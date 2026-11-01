# Methods : postgres-impl-logical-replication

Full API surface for PostgreSQL logical replication. Syntax verified against the
PostgreSQL 17 documentation. Version-specific features are annotated ; everything
unannotated works in PostgreSQL 15, 16, and 17.

## Publisher Prerequisite

Logical replication requires `wal_level = logical` on the publisher (a change from
the default `replica` ; needs a server restart). The replication role needs the
`REPLICATION` attribute and a `pg_hba.conf` entry permitting it.

```sql
SHOW wal_level;                                  -- must be 'logical'
CREATE ROLE repuser WITH REPLICATION LOGIN PASSWORD '...';
```

## CREATE PUBLICATION

```
CREATE PUBLICATION name
    [ FOR ALL TABLES
      | FOR publication_object [, ... ] ]
    [ WITH ( publication_parameter [= value] [, ... ] ) ]

publication_object is one of:
    TABLE [ ONLY ] table_name [ * ] [ ( column_name [, ...] ) ] [ WHERE ( expression ) ]
    TABLES IN SCHEMA { schema_name | CURRENT_SCHEMA } [, ... ]
```

| Form | Meaning |
|------|---------|
| `FOR ALL TABLES` | Replicate every table in the database, including future ones. |
| `FOR TABLE t` | Replicate named tables. |
| `FOR TABLE t (cols)` | Column list (v15+) : replicate only these columns. |
| `FOR TABLE t WHERE (expr)` | Row filter (v15+) : replicate only matching rows. Parentheses required. |
| `FOR TABLES IN SCHEMA s` | All tables in a schema (v15+). |
| `ONLY t` | Exclude descendant partitions of `t`. |

### Publication parameters

| Parameter | Default | Meaning |
|-----------|---------|---------|
| `publish` | `'insert, update, delete, truncate'` | Which DML to replicate. |
| `publish_via_partition_root` | `false` | `true` = publish partition changes under the root table's identity. |

### Restrictions

- Only persistent base tables and partitioned tables. NOT temporary tables,
  unlogged tables, foreign tables, materialized views, or views.
- Row filter expressions : only the table's own columns and immutable built-in
  functions. NO user-defined functions/operators/types/collations, NO non-immutable
  built-ins, NO system columns.
- For `UPDATE`/`DELETE`, a row filter or column list may reference only columns
  covered by the table's `REPLICA IDENTITY`.
- A column list is not supported when the publication also uses `FOR TABLES IN
  SCHEMA`.
- DDL is never published. `COPY ... FROM` publishes as `INSERT`. `MERGE` publishes
  the per-row `INSERT`/`UPDATE`/`DELETE`.

### ALTER / DROP

```sql
ALTER PUBLICATION p ADD TABLE t;
ALTER PUBLICATION p DROP TABLE t;
ALTER PUBLICATION p SET TABLE t WHERE (region = 'EU');
ALTER PUBLICATION p SET (publish = 'insert, update');
DROP PUBLICATION p;
```

## CREATE SUBSCRIPTION

```
CREATE SUBSCRIPTION subscription_name
    CONNECTION 'conninfo'
    PUBLICATION publication_name [, ...]
    [ WITH ( subscription_parameter [= value] [, ... ] ) ]
```

Run outside a transaction block when it creates a slot (the default).

### Subscription parameters

| Parameter | Default | Meaning |
|-----------|---------|---------|
| `connect` | `true` | Connect to the publisher now. `false` forces `create_slot`, `enabled`, `copy_data` to `false`. |
| `create_slot` | `true` | Create the replication slot on the publisher. |
| `slot_name` | subscription name | Slot to use ; `NONE` detaches (needs `enabled=false`, `create_slot=false`). |
| `enabled` | `true` | Start replicating immediately. |
| `copy_data` | `true` | Initial `COPY` of existing published rows before streaming. |
| `binary` | `false` | Transfer in binary format (faster, less portable). |
| `streaming` | `off` | `off` / `on` / `parallel` (v16+) : how in-progress large transactions are applied. |
| `two_phase` | `false` | Replicate prepared (two-phase) transactions. |
| `synchronous_commit` | `off` | Override `synchronous_commit` for this subscription's apply worker. |
| `disable_on_error` | `false` | Auto-disable the subscription when apply hits an error. |
| `origin` | `any` | `none` = only changes with no origin (loop prevention for bidirectional) ; `any` = all. |
| `failover` | `false` | Sync the slot to standbys so it survives publisher failover (v17+). |
| `password_required` | `true` | Require a password in `conninfo` (ignored for superusers). |
| `run_as_owner` | `false` | `false` = apply as each table's owner (safer) ; `true` = as the subscription owner. |

### ALTER / DROP

```sql
ALTER SUBSCRIPTION s ENABLE;
ALTER SUBSCRIPTION s DISABLE;
ALTER SUBSCRIPTION s REFRESH PUBLICATION;          -- pick up table-set changes
ALTER SUBSCRIPTION s SET (streaming = on);
ALTER SUBSCRIPTION s SKIP (LSN '0/14C0378');       -- skip one transaction
ALTER SUBSCRIPTION s SET (slot_name = NONE);       -- detach before drop
DROP SUBSCRIPTION s;                               -- also drops the publisher slot
```

## REPLICA IDENTITY (ALTER TABLE)

```
ALTER TABLE t REPLICA IDENTITY { DEFAULT | USING INDEX index_name | FULL | NOTHING }
```

Controls which old-row columns are written to WAL to identify updated/deleted rows.
Has NO effect unless logical replication is in use.

| Form | Old-row columns logged | Use when |
|------|------------------------|----------|
| `DEFAULT` | primary key columns | Table has a primary key (the normal case). |
| `USING INDEX idx` | columns of `idx` | No PK, but a unique, non-partial, non-deferrable index on `NOT NULL` columns exists. If the index is dropped, behaves as `NOTHING`. |
| `FULL` | every column | No PK and no usable unique index. Subscriber matches by full-row scan. Fails for column types with no btree/hash operator class (`point`, `box`) unless a PK / replica identity exists. |
| `NOTHING` | none | INSERT-only table. `UPDATE`/`DELETE` cannot be replicated. |

## Conflict Handling

A conflict (unique violation, FK violation, RLS rejection, permission failure) makes
the apply worker stop with an error ; it must be resolved manually. Missing data on
a replicated `UPDATE`/`DELETE` is NOT a conflict, just silently skipped.

Resolution :

1. Fix the subscriber data/permissions so the change applies, OR
2. Skip the offending transaction :
   ```sql
   ALTER SUBSCRIPTION s SKIP (LSN '<finish_lsn>');
   ```
   or advance the origin while disabled :
   ```sql
   SELECT pg_replication_origin_advance('pg_16395', '<next_lsn>');
   ```

Skipping discards the WHOLE transaction including non-conflicting rows : it can make
the subscriber inconsistent. With `streaming = parallel`, the finish LSN may not be
logged ; set `streaming = on` and reproduce to obtain it.

## pg_createsubscriber (v17+)

Command-line tool that converts a running physical standby into a logical
subscriber, skipping the initial data copy.

```
pg_createsubscriber { -d dbname } { -D datadir } { -P 'publisher_connstr' } [options]
```

| Option | Meaning |
|--------|---------|
| `-d, --database` | Database to create a subscription in (repeatable). |
| `-D, --pgdata` | Data directory of the physical standby being converted. |
| `-P, --publisher-server` | Connection string to the publisher. |
| `-n, --dry-run` | Validate without modifying anything. |
| `--publication` / `--subscription` / `--replication-slot` | Custom object names. |

Prerequisites : the publisher has `wal_level = logical` and is not in recovery ;
the standby is running and accepts local connections ; both servers share the same
major version and system identifier.

## Monitoring Catalog Views

| View / function | Shows |
|-----------------|-------|
| `pg_publication`, `pg_publication_tables` | Defined publications and their tables. |
| `pg_subscription`, `pg_subscription_rel` | Defined subscriptions and per-table sync state. |
| `pg_stat_subscription` | Apply-worker status, last received/applied LSN. |
| `pg_replication_slots` | Slots on the publisher : `active`, `confirmed_flush_lsn`. |
| `pg_stat_replication` | WAL sender activity per connected subscriber. |
| `pg_replication_origin_status` | Current apply position per origin. |

```sql
-- WAL retained by each slot on the publisher :
SELECT slot_name, active,
       pg_size_pretty(pg_wal_lsn_diff(pg_current_wal_lsn(),
                                      confirmed_flush_lsn)) AS retained
FROM pg_replication_slots;
```

## Restrictions Summary

- DDL is not replicated : sync schema by hand (`pg_dump --schema-only` for the
  initial copy).
- Sequence data is not replicated : the sequence object on the subscriber stays at
  its start value.
- Large objects are not replicated.
- Truncating tables whose subscriber-side foreign keys point outside the
  subscription fails.
- Only tables and partitioned tables ; views / matviews / foreign tables error.
- Partitioned tables : leaf partitions on the publisher must exist as valid targets
  on the subscriber.

## Sources

- https://www.postgresql.org/docs/17/logical-replication.html
- https://www.postgresql.org/docs/17/sql-createpublication.html
- https://www.postgresql.org/docs/17/sql-createsubscription.html
- https://www.postgresql.org/docs/17/sql-altertable.html
- https://www.postgresql.org/docs/17/logical-replication-restrictions.html
- https://www.postgresql.org/docs/17/logical-replication-conflicts.html
- https://www.postgresql.org/docs/17/app-pgcreatesubscriber.html
