# Methods : Declarative Partitioning and pg_partman

Full API surface. Verified against PostgreSQL 17 documentation
(ddl-partitioning.html) and the pg_partman 5.x documentation
(doc/pg_partman.md). Baseline is PostgreSQL 15+ ; version-gated features noted.

## Partitioned Table DDL

| Statement | Purpose |
|-----------|---------|
| `CREATE TABLE t (...) PARTITION BY RANGE (col)` | Partition by ordered key ranges |
| `CREATE TABLE t (...) PARTITION BY LIST (col)` | Partition by enumerated value sets |
| `CREATE TABLE t (...) PARTITION BY HASH (col)` | Partition by hash modulus / remainder |
| `PARTITION BY RANGE (col_a, col_b)` | Multi-column partition key (composite) |

A partitioned parent stores no rows itself : all rows live in partitions.

## Partition Creation

| Statement | Strategy |
|-----------|----------|
| `CREATE TABLE c PARTITION OF p FOR VALUES FROM (lo) TO (hi)` | RANGE : lo inclusive, hi exclusive |
| `CREATE TABLE c PARTITION OF p FOR VALUES IN (v1, v2, ...)` | LIST : exact-match value set |
| `CREATE TABLE c PARTITION OF p FOR VALUES WITH (MODULUS m, REMAINDER r)` | HASH : one slice of the hash space |
| `CREATE TABLE c PARTITION OF p DEFAULT` | DEFAULT : catch-all for unmatched values |
| `... PARTITION OF p ... PARTITION BY RANGE (col2)` | Sub-partition : the child is itself partitioned |

RANGE bound keywords : `MINVALUE` and `MAXVALUE` represent unbounded ends, e.g.
`FOR VALUES FROM (MINVALUE) TO ('2026-01-01')`.

For HASH, every remainder `0 .. modulus-1` must have a partition. For LIST, a
value not in any `IN` set and with no DEFAULT partition causes an insert error.

## ATTACH and DETACH

| Statement | Lock | Notes |
|-----------|------|-------|
| `ALTER TABLE p ATTACH PARTITION c FOR VALUES ...` | scans `c` unless a matching CHECK exists | A CHECK implying the bounds skips the scan |
| `ALTER TABLE p DETACH PARTITION c` | `ACCESS EXCLUSIVE` | Blocks all access to the parent briefly |
| `ALTER TABLE p DETACH PARTITION c CONCURRENTLY` | `SHARE UPDATE EXCLUSIVE` | v14+ ; reads continue ; cannot run in a transaction block |
| `ALTER TABLE p DETACH PARTITION c FINALIZE` | weak | Completes an interrupted concurrent detach |
| `DROP TABLE c` | `ACCESS EXCLUSIVE` on `c` | Removes a partition and its data |

Recommended ATTACH workflow : create standalone table, add a CHECK constraint
matching the target bounds, load data, ATTACH, then drop the redundant CHECK.

## Indexes on Partitioned Tables

| Statement | Behaviour |
|-----------|-----------|
| `CREATE INDEX ON p (col)` | Creates the index on all current and future partitions |
| `CREATE INDEX ON ONLY p (col)` | Parent-only "template" index, marked invalid until children attach |
| `CREATE INDEX CONCURRENTLY ON c (col)` | Per-partition concurrent build (NOT allowed on the parent) |
| `ALTER INDEX parent_idx ATTACH PARTITION child_idx` | Links a child index under the parent template |

`CREATE INDEX CONCURRENTLY` and `REINDEX CONCURRENTLY` are NOT supported on a
partitioned parent. Build per-partition, then attach.

## Constraints on Partitioned Tables

| Constraint | Rule |
|------------|------|
| `PRIMARY KEY` / `UNIQUE` | MUST include every partition-key column |
| `FOREIGN KEY` | Supported ; verified per partition |
| `CHECK` | Always inherited by partitions ; cannot be `NO INHERIT` |
| `NOT NULL` | Inherited and enforced on every partition |
| `EXCLUDE` (exclusion constraint) | Supported on partitioned tables in v17+ |
| Identity columns | Supported on partitioned tables in v17+ |

A `UNIQUE` constraint that omits the partition key is rejected with SQLSTATE
`0A000` (`feature_not_supported`).

## Partition Pruning and Planner Settings

| Setting | Default | Effect |
|---------|---------|--------|
| `enable_partition_pruning` | `on` | Plan-time and runtime elimination of partitions |
| `constraint_exclusion` | `partition` | Legacy inheritance-based exclusion ; leave at `partition` |
| `enable_partitionwise_join` | `off` | v11+ : join matching partitions pair by pair |
| `enable_partitionwise_aggregate` | `off` | v11+ : aggregate per partition before combining |

Plan-time pruning : pruned children are absent from `EXPLAIN`. Runtime pruning
(v11+) : pruned children show `(never executed)` / `loops=0` in
`EXPLAIN ANALYZE`. Partition-wise join/aggregate raise memory use per partition
pair, so they are off by default ; enable per session and test.

## pg_partman 5.x : Setup

| Statement | Purpose |
|-----------|---------|
| `CREATE SCHEMA partman;` | Dedicated schema for the extension |
| `CREATE EXTENSION pg_partman SCHEMA partman;` | Install pg_partman (requires PostgreSQL 14+) |

`postgresql.conf` for the background worker :

```
shared_preload_libraries = 'pg_partman_bgw'
pg_partman_bgw.interval  = 3600
pg_partman_bgw.dbname    = 'mydb'
pg_partman_bgw.role      = 'partman_role'
```

`pg_partman_bgw.dbname` is required for maintenance to run automatically.

## pg_partman 5.x : create_parent()

```sql
create_parent(
    p_parent_table        text,
    p_control             text,
    p_interval            text,
    p_type                text    DEFAULT 'range',
    p_epoch               text    DEFAULT 'none',
    p_premake             int     DEFAULT 4,
    p_start_partition     text    DEFAULT NULL,
    p_default_table       boolean DEFAULT true,
    p_automatic_maintenance text  DEFAULT 'on',
    p_constraint_cols     text[]  DEFAULT NULL,
    p_template_table      text    DEFAULT NULL,
    p_jobmon              boolean DEFAULT true,
    p_date_trunc_interval text    DEFAULT NULL
) RETURNS boolean
```

- `p_parent_table` : schema-qualified table name.
- `p_control` : the partition-key column.
- `p_interval` : time interval string (`'1 day'`, `'1 month'`, `'1 year'`) or
  an integer-as-text for id-based partitioning.
- `p_type` : `'range'` for time and for id intervals >= 2 ; `'list'` for an id
  interval of 1.
- `p_premake` : how many future partitions to keep pre-created.

## pg_partman 5.x : Maintenance

```sql
run_maintenance(
    p_parent_table text    DEFAULT NULL,
    p_analyze      boolean DEFAULT false,
    p_jobmon       boolean DEFAULT true
) RETURNS void

run_maintenance_proc(
    p_wait    int     DEFAULT 0,
    p_analyze boolean DEFAULT NULL,
    p_jobmon  boolean DEFAULT true
)
```

`run_maintenance_proc` is a procedure (call with `CALL`). It creates upcoming
partitions and applies retention. The `pg_partman_bgw` worker calls it on the
configured interval.

## pg_partman 5.x : part_config Retention Columns

| Column | Type | Purpose |
|--------|------|---------|
| `parent_table` | text | The partitioned table this config row controls |
| `retention` | text | Age threshold : an interval (time) or integer (id) |
| `retention_keep_table` | boolean | DEFAULT true : keep detached ; false : drop the child |
| `retention_keep_index` | boolean | DEFAULT true : keep indexes when a table is detached |
| `retention_schema` | text | Optional : move expired partitions here instead of dropping |
| `retention_keep_publication` | boolean | DEFAULT false : keep detached tables in publications |

Configure retention with an `UPDATE` on `partman.part_config`. During
`run_maintenance`, pg_partman removes child tables older than `retention`
(time) or below `max id - retention` (id).
