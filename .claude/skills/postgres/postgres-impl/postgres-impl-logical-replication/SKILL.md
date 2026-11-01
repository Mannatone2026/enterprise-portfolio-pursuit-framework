---
name: postgres-impl-logical-replication
description: >
  Use when replicating selected tables between databases, setting up zero-downtime upgrades, or debugging a stuck subscription.
  Prevents UPDATE/DELETE silently not replicating (REPLICA IDENTITY missing on PK-less table), unbounded WAL growth from abandoned slots, and broken replication after an un-synced DDL change.
  Covers CREATE PUBLICATION / SUBSCRIPTION, REPLICA IDENTITY (DEFAULT / FULL / INDEX), row filters + column lists (v15+), logical-from-standby (v16+), bidirectional (v16+), pg_createsubscriber (v17+), slot management, initial sync, DDL-not-replicated caveat.
  Keywords: logical replication, CREATE PUBLICATION, CREATE SUBSCRIPTION, REPLICA IDENTITY, replication slot, row filter, column list, pg_createsubscriber, bidirectional replication, subscription stuck, WAL growing, updates not replicating, how to replicate one table, zero downtime upgrade
license: MIT
compatibility: "Designed for Claude Code. Requires PostgreSQL 15, 16, or 17."
metadata:
  author: OpenAEC-Foundation
  version: "1.0"
---

# postgres-impl-logical-replication

## Quick Reference :

Logical replication ships individual row changes (not raw WAL blocks) from a publisher to a subscriber by decoding the WAL. It is selective per table, works across major versions and platforms, and is the basis for zero-downtime upgrades and selective data distribution. The publisher needs `wal_level = logical`. A **publication** (`CREATE PUBLICATION`) names a set of tables and which DML to send. A **subscription** (`CREATE SUBSCRIPTION`) on the other database connects, creates a replication slot on the publisher, and begins applying changes ; by default it also copies the existing rows first (`copy_data = true`).

Two facts cause most production incidents. First : **REPLICA IDENTITY**. To replicate `UPDATE` and `DELETE`, the subscriber must be able to locate the old row. With the default `REPLICA IDENTITY DEFAULT` and NO primary key, `UPDATE`/`DELETE` are silently NOT replicated (and on the publisher, raise an error when the publication includes those operations). A table without a PK needs `REPLICA IDENTITY FULL` or `USING INDEX`. Second : a **replication slot retains WAL on the publisher until its subscriber consumes it**. A subscription that is dropped on the subscriber side without dropping the slot, or a subscriber that stays offline, makes the publisher accumulate WAL without bound until the disk fills.

Two things logical replication does NOT do. **DDL is not replicated** : every schema change (`ALTER TABLE`, new columns, type changes) must be applied by hand on BOTH sides, publisher-side first for additive changes. **Sequences are not replicated** : a `serial`/`identity` column's values arrive as data, but the sequence object on the subscriber stays at its start value, which matters at failover. Version features : row filters, column lists, and `FOR TABLES IN SCHEMA` arrived in v15 ; logical decoding from a standby, bidirectional replication, and parallel apply in v16 ; `pg_createsubscriber` and failover slots in v17.

## When To Use This Skill :

ALWAYS use this skill when :
- Replicating a subset of tables (or columns, or filtered rows) between two databases
- Setting up a major-version upgrade with logical replication for near-zero downtime
- Choosing or fixing a table's `REPLICA IDENTITY`
- Writing `CREATE PUBLICATION` / `CREATE SUBSCRIPTION` statements
- Debugging a subscription that has stopped, or a publisher whose WAL keeps growing
- Using row filters or column lists (v15+) to limit replicated data
- Building bidirectional replication or converting a physical standby with `pg_createsubscriber`

NEVER use this skill for :
- Whole-cluster physical / streaming replication and hot standbys : that is a different mechanism
- Point-in-time recovery, `pg_basebackup`, WAL archiving : see backup/PITR skills
- Connection pooling or read-scaling routing : unrelated to the replication mechanism itself

## Decision Trees :

### Which REPLICA IDENTITY for a published table? :

```
Will the publication replicate UPDATE or DELETE for this table?
├── No (publish = 'insert' only) :
│       REPLICA IDENTITY is irrelevant. DEFAULT is fine.
└── Yes :
    ├── Table has a PRIMARY KEY :
    │       REPLICA IDENTITY DEFAULT (the default). Done.
    ├── No PK but has a UNIQUE index on NOT NULL columns,
    │   non-partial, non-deferrable :
    │       ALTER TABLE t REPLICA IDENTITY USING INDEX idx;
    └── No PK and no suitable unique index :
            ALTER TABLE t REPLICA IDENTITY FULL;
            Logs every column of the old row. Works, but the
            subscriber matches rows by full-row scan : costly on
            wide / large tables. Fails for column types with no
            btree/hash operator class (point, box) unless a PK
            or replica identity exists. Prefer adding a PK.
```

### How to bootstrap the subscriber's initial data? :

```
How large is the dataset and what infrastructure exists?
├── Small to moderate, no existing physical standby :
│       CREATE SUBSCRIPTION ... WITH (copy_data = true)  [default].
│       The subscriber COPYs every published table, then streams.
├── Very large dataset, the copy phase would take too long :
│   ├── PostgreSQL 17+ AND a physical standby already exists :
│   │       pg_createsubscriber : converts the standby into a
│   │       logical subscriber, skipping the initial copy entirely.
│   └── Otherwise : copy_data = true and accept the copy time, or
│           pre-load with pg_dump and CREATE SUBSCRIPTION WITH
│           (copy_data = false), but then YOU guarantee the data
│           matches the slot's start LSN.
```

### A subscription has stopped applying : how to resolve? :

```
Check the error in the subscriber log / pg_stat_subscription.
├── Constraint violation (duplicate key, FK, RLS, permission) :
│   ├── The conflicting row is genuinely wrong on the subscriber :
│   │       fix the subscriber data / permissions, replication resumes.
│   └── The transaction must be discarded :
│           ALTER SUBSCRIPTION s SKIP (LSN '<finish_lsn from log>');
│           WARNING : skips the WHOLE transaction, including
│           non-conflicting changes. Can leave the subscriber
│           inconsistent. Verify before skipping.
├── Column / type mismatch -> a DDL change was not synced :
│       apply the missing DDL on the subscriber, replication resumes.
└── Recurring errors : create the subscription WITH
        (disable_on_error = true) so it disables instead of
        looping, then resolve and re-enable.
```

## Patterns :

### Pattern 1 : Set REPLICA IDENTITY before publishing UPDATE/DELETE

ALWAYS ensure every table in an update/delete-replicating publication has a PK or an explicit `REPLICA IDENTITY`.
NEVER leave a PK-less table at `REPLICA IDENTITY DEFAULT` in such a publication : its `UPDATE`/`DELETE` changes are not replicated and the publisher errors on those operations.

```sql
-- Publisher : wal_level = logical must be set (needs a restart).
SHOW wal_level;   -- must be 'logical'

-- Table with a PK : nothing to do, DEFAULT already works.
-- Table WITHOUT a PK that needs UPDATE/DELETE replication :
ALTER TABLE events REPLICA IDENTITY FULL;
-- ... or, better, point at a usable unique index :
ALTER TABLE events REPLICA IDENTITY USING INDEX events_uniq_idx;
```

WHY : `REPLICA IDENTITY` controls which old-row columns are written to WAL so the subscriber can find the target row. `DEFAULT` uses the primary key ; with no PK it identifies nothing, so `UPDATE`/`DELETE` cannot be applied. The clause has no effect at all unless logical replication is in use. Source : postgresql.org/docs/17/sql-altertable.html (REPLICA IDENTITY).

### Pattern 2 : CREATE PUBLICATION with row filter and column list

ALWAYS scope a publication to exactly the tables, columns, and rows the subscriber needs ; row filters and column lists exist for this (v15+).
NEVER put a non-`REPLICA IDENTITY` column in a row filter or column list when the publication replicates `UPDATE`/`DELETE` : the publisher rejects it.

```sql
-- Whole tables :
CREATE PUBLICATION sales_pub FOR TABLE orders, customers;

-- Row filter (v15+) : only EU rows. Parentheses are required.
CREATE PUBLICATION eu_pub FOR TABLE orders WHERE (region = 'EU');

-- Column list (v15+) : ship only these columns.
CREATE PUBLICATION lean_pub FOR TABLE customers (id, name, region);

-- Whole schema (v15+) :
CREATE PUBLICATION app_pub FOR TABLES IN SCHEMA app;

-- Insert-only feed : REPLICA IDENTITY then does not matter.
CREATE PUBLICATION audit_pub FOR TABLE audit_log WITH (publish = 'insert');
```

WHY : a publication defines what to send ; `publish` defaults to `'insert, update, delete, truncate'`. Row filter expressions may use only the table's own columns and immutable built-in functions (no user-defined functions, no system columns). A column list is not allowed alongside `FOR TABLES IN SCHEMA`. Only persistent base and partitioned tables can be published. Source : postgresql.org/docs/17/sql-createpublication.html.

### Pattern 3 : CREATE SUBSCRIPTION and the initial sync

ALWAYS run `CREATE SUBSCRIPTION` outside a transaction block when it creates a slot, and let `copy_data = true` (the default) seed the existing rows.
NEVER assume `copy_data = false` is safe : it skips the initial copy, so the subscriber must already hold data consistent with the slot's start LSN.

```sql
-- On the SUBSCRIBER database. Creates a slot on the publisher,
-- copies existing rows, then streams ongoing changes.
CREATE SUBSCRIPTION sales_sub
  CONNECTION 'host=pub.db port=5432 user=repuser dbname=salesdb'
  PUBLICATION sales_pub
  WITH (copy_data = true, streaming = parallel, disable_on_error = true);

-- Deferred setup : define now, start later.
CREATE SUBSCRIPTION sales_sub
  CONNECTION 'host=pub.db ...' PUBLICATION sales_pub
  WITH (enabled = false);
ALTER SUBSCRIPTION sales_sub ENABLE;
```

WHY : the subscription owns a replication slot on the publisher and an apply worker locally. `copy_data = true` performs an initial `COPY` of each published table before streaming, so the subscriber starts consistent. `streaming = parallel` (v16+) applies large in-progress transactions via parallel workers. `disable_on_error = true` stops a failing subscription instead of looping forever. Source : postgresql.org/docs/17/sql-createsubscription.html.

### Pattern 4 : Keep DDL in sync by hand

ALWAYS apply every schema change manually on both publisher and subscriber, and order it so the subscriber can accept the publisher's rows at all times.
NEVER `ALTER TABLE` on only one side : logical replication does not replicate DDL, and a column mismatch stops the subscription.

```sql
-- Adding a NULLable / defaulted column : SUBSCRIBER FIRST, then publisher.
--   1) on subscriber :  ALTER TABLE orders ADD COLUMN note text;
--   2) on publisher  :  ALTER TABLE orders ADD COLUMN note text;
-- Dropping a column : PUBLISHER FIRST, then subscriber.
-- After changing the set of columns, refresh the subscription :
ALTER SUBSCRIPTION sales_sub REFRESH PUBLICATION;
```

WHY : only DML row changes are decoded from WAL ; `CREATE`/`ALTER`/`DROP` are not. The initial schema is copied once with `pg_dump --schema-only` ; every later change is the operator's responsibility. Applying an additive change on the subscriber first guarantees it can always accept incoming rows. Sequences are likewise not replicated : their values on the subscriber stay at the start value. Source : postgresql.org/docs/17/logical-replication-restrictions.html.

### Pattern 5 : Monitor slots and drop them cleanly

ALWAYS drop a subscription with `DROP SUBSCRIPTION` so it also removes its slot on the publisher, and monitor slot lag.
NEVER abandon a replication slot : an unconsumed slot pins WAL on the publisher forever and will fill the disk.

```sql
-- On the PUBLISHER : watch slot lag.
SELECT slot_name, active,
       pg_size_pretty(
         pg_wal_lsn_diff(pg_current_wal_lsn(), confirmed_flush_lsn)) AS retained_wal
FROM pg_replication_slots;

-- Clean teardown on the SUBSCRIBER : drops the slot on the publisher too.
DROP SUBSCRIPTION sales_sub;

-- If the publisher is unreachable, detach the slot first, then drop :
ALTER SUBSCRIPTION sales_sub SET (slot_name = NONE);
DROP SUBSCRIPTION sales_sub;
-- then on the publisher : SELECT pg_drop_replication_slot('sales_sub');
```

WHY : a logical slot guarantees the publisher keeps every WAL segment until the subscriber confirms it. That is the feature that makes replication reliable across a subscriber outage, and the failure mode if the subscriber never returns. `DROP SUBSCRIPTION` normally drops the publisher slot in the same step ; only when the publisher is unreachable do you detach with `slot_name = NONE` and drop the slot manually. Source : postgresql.org/docs/17/logical-replication-subscription.html.

## Anti-Patterns :

(Short list ; full cause / symptom / fix in references/anti-patterns.md)

- `REPLICA IDENTITY DEFAULT` on a PK-less table in an update/delete publication : changes silently not replicated
- Abandoned replication slot : unbounded WAL growth on the publisher, disk fills
- DDL changed on one side only : column mismatch stops the subscription
- `REPLICA IDENTITY FULL` on every table : full-row-scan apply cost ; fails on point/box columns
- Treating sequences as replicated : subscriber sequence stuck at start value, ID collisions after failover
- Row filter / column list using a non-`REPLICA IDENTITY` column for `UPDATE`/`DELETE` : publication rejected
- Blind `ALTER SUBSCRIPTION ... SKIP` : skips non-conflicting changes too, subscriber goes inconsistent
- `CREATE SUBSCRIPTION` inside a transaction block while creating a slot : not allowed

## Reference Links :

- [references/methods.md](references/methods.md) : Full API surface, CREATE PUBLICATION / SUBSCRIPTION / REPLICA IDENTITY syntax, all options, catalog views
- [references/examples.md](references/examples.md) : Working code samples, version-annotated, end-to-end setup and upgrade flow
- [references/anti-patterns.md](references/anti-patterns.md) : Anti-patterns with cause, symptom, SQLSTATE, and fix

## See Also :

- `postgres-impl-partitioning` : partitioned-table publication needs matching leaf partitions on the subscriber
- `postgres-core-architecture` : `wal_level`, WAL decoding, the substrate logical replication runs on
- Vooronderzoek section : §9 (Replication : streaming and logical)
- Official docs : https://www.postgresql.org/docs/17/logical-replication.html
