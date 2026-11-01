---
name: postgres-impl-zero-downtime-migrations
description: >
  Use when altering a table on a live production database, adding indexes or constraints without downtime, or backfilling a new column.
  Prevents plain CREATE INDEX locking writes (use CONCURRENTLY), ALTER COLUMN TYPE full-rewrite locks, migrations stalling all traffic behind a long query (set lock_timeout), and single huge backfill UPDATEs.
  Covers ALTER TABLE rewrite rules (metadata-only vs full rewrite), CREATE INDEX / REINDEX CONCURRENTLY, ADD COLUMN with default (v11+), safe NOT NULL + FK via NOT VALID + VALIDATE, lock_timeout, chunked backfill, expand-contract pattern.
  Keywords: zero downtime migration, ALTER TABLE, CREATE INDEX CONCURRENTLY, REINDEX CONCURRENTLY, NOT VALID, VALIDATE CONSTRAINT, lock_timeout, backfill, expand contract, table rewrite, migration locked the table, ALTER blocks writes, how to add column safely, add not null without downtime
license: MIT
compatibility: "Designed for Claude Code. Requires PostgreSQL 15, 16, or 17."
metadata:
  author: OpenAEC-Foundation
  version: "1.0"
---

# postgres-impl-zero-downtime-migrations

## Quick Reference :

A schema change on a live database is safe only when it never holds a
strong lock long enough to block production traffic. Two distinct dangers :

1. **The operation itself is slow** : a full table rewrite (`ALTER COLUMN
   TYPE`, `ADD COLUMN` with a volatile default) or a plain `CREATE INDEX`
   holds a strong lock for the whole duration.
2. **The lock queue** : even a fast, metadata-only `ALTER TABLE` requests
   `ACCESS EXCLUSIVE`. If a long-running query holds the table, the `ALTER`
   waits behind it, and every query arriving after the `ALTER` waits behind
   the `ALTER`. One slow `SELECT` plus one fast `ALTER` can freeze the table.

Core rules :

- ALWAYS `SET lock_timeout` before any migration DDL, so a blocked `ALTER`
  fails fast instead of stalling all traffic.
- Build indexes with `CREATE INDEX CONCURRENTLY`, never plain `CREATE INDEX`,
  on a table that takes writes.
- Add `NOT NULL`, foreign keys, and `CHECK` constraints in two steps :
  `... NOT VALID` (fast), then `VALIDATE CONSTRAINT` (no write block).
- Backfill large tables in committed chunks, never one giant `UPDATE`.
- Change a column type or rename a column with the expand-contract pattern,
  never `ALTER COLUMN TYPE` in place.

## When To Use This Skill :

ALWAYS use this skill when :
- Running `ALTER TABLE`, `CREATE INDEX`, or `ADD CONSTRAINT` against a
  database that is serving live traffic.
- Adding a column, changing a column type, or renaming a column safely.
- Adding a `NOT NULL`, foreign key, or `CHECK` constraint to a large table.
- Backfilling a newly added column across millions of rows.
- A migration "locked the table" or stalled all queries.

NEVER use this skill for :
- Choosing index type or columns : use postgres-impl-indexing-strategy.
- VACUUM, bloat, or autovacuum tuning : use postgres-impl-vacuum-bloat.
- Bulk-loading fresh data into a new/empty table : use postgres-impl-bulk-loading.
- Lock-conflict theory and deadlock analysis : use the locking core skill.

## Decision Trees :

### Is this ALTER TABLE safe to run online? :

```
What does the ALTER do?
├── ADD COLUMN, no DEFAULT or non-volatile DEFAULT  -> metadata only, no rewrite
├── DROP COLUMN                                     -> metadata only, no rewrite
├── RENAME COLUMN / TABLE / CONSTRAINT              -> metadata only, no rewrite
├── SET DEFAULT / DROP DEFAULT                       -> metadata only, no rewrite
│      ALL of the above still take ACCESS EXCLUSIVE briefly
│      -> SET lock_timeout first, retry on 55P03
├── ADD COLUMN with a volatile DEFAULT (now(), random()) -> FULL REWRITE
│      -> expand-contract: add nullable column, backfill in chunks
├── ALTER COLUMN TYPE                                -> FULL REWRITE (almost always)
│      exception: binary-coercible old->new type AND no USING change
│      -> expand-contract: new column, dual-write, backfill, swap
└── SET NOT NULL                                     -> scans the whole table
       -> add CHECK (col IS NOT NULL) NOT VALID, VALIDATE it, then SET NOT NULL
```

### Adding a constraint without blocking writes :

```
Constraint type?
├── NOT NULL
│   1. ALTER TABLE t ADD CONSTRAINT c CHECK (col IS NOT NULL) NOT VALID;
│   2. ALTER TABLE t VALIDATE CONSTRAINT c;        -- SHARE UPDATE EXCLUSIVE
│   3. ALTER TABLE t ALTER COLUMN col SET NOT NULL;-- scan skipped, CHECK proves it
│   4. (optional) ALTER TABLE t DROP CONSTRAINT c; -- redundant once NOT NULL set
├── FOREIGN KEY
│   1. ALTER TABLE t ADD CONSTRAINT fk FOREIGN KEY (x) REFERENCES o(id) NOT VALID;
│   2. ALTER TABLE t VALIDATE CONSTRAINT fk;        -- SHARE UPDATE EXCLUSIVE
└── CHECK
    1. ALTER TABLE t ADD CONSTRAINT c CHECK (...) NOT VALID;
    2. ALTER TABLE t VALIDATE CONSTRAINT c;         -- SHARE UPDATE EXCLUSIVE
```

### A concurrent index build failed :

```
CREATE INDEX CONCURRENTLY raised an error (deadlock, unique violation, ...)
├── An INVALID index is left behind (\d shows "INVALID";
│   pg_index.indisvalid = false). It is ignored by the planner but still
│   carries write overhead.
├── Preferred fix:  DROP INDEX CONCURRENTLY idx;  then retry CREATE INDEX CONCURRENTLY.
└── Alternative:    REINDEX INDEX CONCURRENTLY idx;   (v12+)
```

## Patterns :

### Pattern 1 : Set lock_timeout before every migration DDL

ALWAYS : `SET lock_timeout` to a few seconds before any `ALTER TABLE` or
`DROP`, so a blocked request fails instead of freezing the table.
NEVER : run migration DDL with the default `lock_timeout = 0` (wait forever).

```sql
SET lock_timeout = '3s';
ALTER TABLE orders ADD COLUMN tracking_code text;   -- metadata only
RESET lock_timeout;
-- On failure the statement raises SQLSTATE 55P03 (lock_not_available);
-- the migration runner catches it and retries after a short pause.
```

WHY : `ALTER TABLE` requests `ACCESS EXCLUSIVE`. With `lock_timeout = 0` the
request queues behind any long-running query, and because lock requests are
ordered, every new query queues behind the `ALTER`. A 3-second timeout caps
the blast radius : the `ALTER` gives up and is retried later. Use
`statement_timeout` as well to bound the operation once it does start.

### Pattern 2 : Add a column with a default, no rewrite

ALWAYS : give a new column a constant or non-volatile default; this is
stored in catalog metadata and applied lazily, with no table rewrite.
NEVER : add a column with a volatile default such as `now()` or `random()`
on a large live table : that forces a full rewrite.

```sql
SET lock_timeout = '3s';
-- No rewrite: the default is evaluated once and stored in metadata.
ALTER TABLE orders ADD COLUMN status text NOT NULL DEFAULT 'pending';
RESET lock_timeout;
```

WHY : when `ADD COLUMN` has a non-volatile `DEFAULT`, the value is evaluated
at statement time and stored in the table's metadata; existing rows read
that stored value, so no rewrite happens. A volatile default must be
computed per row, which requires rewriting every page. For a per-row
timestamp, add the column without a default and backfill it (Pattern 6).

### Pattern 3 : Build indexes with CONCURRENTLY

ALWAYS : use `CREATE INDEX CONCURRENTLY` on a table that takes writes.
NEVER : run a plain `CREATE INDEX` on a busy table, and NEVER wrap
`CONCURRENTLY` in a transaction block.

```sql
-- Not inside BEGIN/COMMIT: CONCURRENTLY cannot run in a transaction block.
CREATE INDEX CONCURRENTLY idx_orders_customer ON orders (customer_id);

-- Detect a failed build:
SELECT indexrelid::regclass AS index_name
FROM pg_index
WHERE NOT indisvalid;

-- Recover from a failed build:
DROP INDEX CONCURRENTLY idx_orders_customer;   -- then retry the CREATE
```

WHY : a plain `CREATE INDEX` locks the table against writes for the entire
build. `CONCURRENTLY` takes only `SHARE UPDATE EXCLUSIVE`, so inserts,
updates, and deletes continue; the cost is two table scans and a longer wall
time. A failed concurrent build leaves an `INVALID` index that the planner
ignores but that still adds write overhead, so it must be dropped or rebuilt
with `REINDEX INDEX CONCURRENTLY` (v12+). Only one concurrent build per
table runs at a time. For a partitioned table, build each partition's index
concurrently, then create the partitioned index non-concurrently.

### Pattern 4 : Add NOT NULL without a blocking scan

ALWAYS : prove non-null with a validated `CHECK` constraint first, then
`SET NOT NULL` skips its table scan.
NEVER : run `ALTER COLUMN ... SET NOT NULL` directly on a large table : it
scans every row while holding `ACCESS EXCLUSIVE`.

```sql
-- Step 1: add the CHECK as NOT VALID (no scan, brief ACCESS EXCLUSIVE).
ALTER TABLE orders ADD CONSTRAINT orders_status_nn
  CHECK (status IS NOT NULL) NOT VALID;

-- Step 2: validate it (SHARE UPDATE EXCLUSIVE, does not block writes).
ALTER TABLE orders VALIDATE CONSTRAINT orders_status_nn;

-- Step 3: SET NOT NULL skips its scan because the valid CHECK proves it.
ALTER TABLE orders ALTER COLUMN status SET NOT NULL;

-- Step 4 (optional): drop the now-redundant CHECK.
ALTER TABLE orders DROP CONSTRAINT orders_status_nn;
```

WHY : `SET NOT NULL` normally scans the whole table under `ACCESS
EXCLUSIVE`. If a valid `CHECK (col IS NOT NULL)` exists and is not dropped
in the same command, the scan is skipped. The `NOT VALID` + `VALIDATE` split
moves the expensive scan to a `SHARE UPDATE EXCLUSIVE` step that lets writes
continue.

### Pattern 5 : Add a foreign key without a blocking scan

ALWAYS : add the FK as `NOT VALID`, then `VALIDATE` it as a separate step.
NEVER : add a plain foreign key to a large table in one statement : the
verification scan runs while holding a strong lock.

```sql
-- Step 1: NOT VALID -- no verification scan, enforced for new rows.
ALTER TABLE orders ADD CONSTRAINT orders_customer_fk
  FOREIGN KEY (customer_id) REFERENCES customers (id) NOT VALID;

-- Step 2: VALIDATE -- SHARE UPDATE EXCLUSIVE, plus ROW SHARE on customers.
ALTER TABLE orders VALIDATE CONSTRAINT orders_customer_fk;
```

WHY : a `NOT VALID` foreign key is enforced for every subsequent insert and
update immediately, only existing rows are not yet checked. `VALIDATE
CONSTRAINT` then scans existing rows under `SHARE UPDATE EXCLUSIVE`, so
production writes are not blocked.

### Pattern 6 : Backfill large tables in committed chunks

ALWAYS : backfill a new or changed column in bounded batches, committing
after each batch.
NEVER : run one `UPDATE` across the whole table : it holds row locks for the
entire run, bloats the table with dead tuples, and lags replication.

```sql
-- Driven by the migration runner (or a DO loop). One committed batch:
UPDATE orders
SET region = derive_region(country_code)
WHERE id BETWEEN :lo AND :hi
  AND region IS NULL;
-- Advance :lo/:hi by the batch size (e.g. 10000) and loop until done.
```

WHY : a single huge `UPDATE` is one transaction, so every touched row stays
locked until it finishes, dead tuples accumulate until it commits, and a
replica must apply the whole change at once. Chunks of 1000-10000 rows, each
committed, keep locks short, let autovacuum reclaim space between batches,
and keep replication lag bounded. Add a small `statement_timeout` per batch.

### Pattern 7 : Change a column type or rename with expand-contract

ALWAYS : change a column type or rename a column by adding a new column,
dual-writing, backfilling, switching reads, then dropping the old one.
NEVER : run `ALTER COLUMN TYPE` in place on a large live table.

```sql
-- Expand: new nullable column (metadata only, no rewrite).
ALTER TABLE orders ADD COLUMN amount_cents bigint;
-- Dual-write: application writes both columns, or a trigger mirrors them.
-- Backfill: chunked UPDATE amount_cents from the old column (Pattern 6).
-- Constrain: add NOT NULL via Pattern 4 once backfill completes.
-- Switch: deploy application reads to amount_cents.
-- Contract: ALTER TABLE orders DROP COLUMN amount;
```

WHY : `ALTER COLUMN TYPE` rewrites the entire table and all its indexes
under `ACCESS EXCLUSIVE`. A plain `RENAME` is fast but not atomic with the
application deploy, so the code and schema disagree for an instant.
Expand-contract decouples the schema change from the code deploy : each step
is individually safe and reversible. See references/examples.md.

## Anti-Patterns :

(Full cause + symptom + fix in references/anti-patterns.md)

- Plain `CREATE INDEX` on a busy table : blocks all writes for the whole build.
- `ALTER COLUMN TYPE` in place : full rewrite under `ACCESS EXCLUSIVE`.
- `ADD COLUMN ... DEFAULT now()` (volatile default) : forces a full rewrite.
- Migration DDL without `lock_timeout` : queues behind a long query and
  stalls all traffic : SQLSTATE 55P03 when a timeout is finally set.
- One giant `UPDATE` backfill : long locks, table bloat, replication lag.
- `CREATE INDEX CONCURRENTLY` inside a transaction block : raises an error.
- Ignoring an `INVALID` index after a failed concurrent build : planner
  ignores it, writes still pay its cost.
- `SET NOT NULL` directly on a large table : full scan under `ACCESS EXCLUSIVE`.

## Reference Links :

- [references/methods.md](references/methods.md) : ALTER TABLE rewrite + lock
  matrix, CONCURRENTLY semantics, lock_timeout / statement_timeout, INVALID
  index detection.
- [references/examples.md](references/examples.md) : Working migration code
  samples, version-annotated, including the full expand-contract sequence.
- [references/anti-patterns.md](references/anti-patterns.md) : Anti-patterns
  with cause, symptom, fix-pattern, and SQLSTATE where applicable.

## See Also :

- postgres-impl-indexing-strategy : choosing index type and columns before
  building one concurrently.
- postgres-impl-vacuum-bloat : reclaiming the bloat a backfill leaves behind.
- postgres-impl-partitioning : ATTACH / DETACH PARTITION CONCURRENTLY.
- Vooronderzoek section : §16 (Zero-Downtime Migrations).
- Official docs : https://www.postgresql.org/docs/17/sql-altertable.html ,
  https://www.postgresql.org/docs/17/sql-createindex.html
