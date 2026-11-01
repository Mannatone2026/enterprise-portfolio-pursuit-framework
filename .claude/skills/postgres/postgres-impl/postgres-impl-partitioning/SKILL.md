---
name: postgres-impl-partitioning
description: >
  Use when splitting a large table by time or category, automating partition creation, or debugging why a query scans every partition.
  Prevents queries that scan all partitions from missing the partition key in WHERE (no pruning), unique constraints rejected for omitting the partition key, and long ATTACH locks without a pre-matching CHECK constraint.
  Covers declarative partitioning RANGE / LIST / HASH, partition pruning (plan-time + runtime v11+), partition-wise join/aggregate, DEFAULT partition, ATTACH / DETACH CONCURRENTLY (v14+), sub-partitioning, pg_partman automation.
  Keywords: partitioning, PARTITION BY RANGE, LIST, HASH, partition pruning, partition-wise join, DEFAULT partition, ATTACH PARTITION, DETACH CONCURRENTLY, pg_partman, sub-partitioning, query scans all partitions, partition is slow, how to split a big table, time-series table
license: MIT
compatibility: "Designed for Claude Code. Requires PostgreSQL 15, 16, or 17."
metadata:
  author: OpenAEC-Foundation
  version: "1.0"
---

# postgres-impl-partitioning

## Quick Reference :

Declarative partitioning splits one logical table into many physical child
tables by a **partition key**. The planner skips children that cannot hold
matching rows (partition pruning), so big-table scans touch only relevant data.

Three strategies :

- **RANGE** : ordered ranges of the key. Time-series, sequential ids. Bounds
  are lower-inclusive, upper-exclusive.
- **LIST** : explicit value sets. Discrete categories (region, tenant, status).
- **HASH** : hash modulus + remainder. Even distribution when no natural range
  or list exists.

Two rules govern every partitioned query :

1. ALWAYS filter by the partition key. A query with no partition-key predicate
   in `WHERE` scans every partition : pruning cannot eliminate anything.
2. ALWAYS include all partition-key columns in any `UNIQUE` or `PRIMARY KEY`
   constraint. PostgreSQL rejects a unique constraint that omits them.

```
Time-series / sequential ids       -> PARTITION BY RANGE
Discrete categories, fixed set     -> PARTITION BY LIST
Even spread, no natural key range  -> PARTITION BY HASH
Catch unmatched values             -> add a DEFAULT partition
Automate creation + retention      -> pg_partman
```

## When To Use This Skill :

ALWAYS use this skill when :
- A table is large enough that full scans or VACUUM are too slow
- Data has a natural time, sequence, or category dimension to split on
- Old data must be dropped or archived in bulk (detach a whole partition)
- A query unexpectedly scans every partition and you need pruning
- Automating rolling time-window partitions with retention

NEVER use this skill for :
- Sharding across multiple servers : partitioning is single-instance only
- Speeding up a small table : partitioning adds planning overhead, not speed
- Replacing an index : partitions and indexes solve different problems
- Splitting by a column that queries rarely filter on : pruning will not fire

## Decision Trees :

### Which partition strategy :

```
Does the key have a meaningful order (date, timestamp, serial id)?
├─ YES -> PARTITION BY RANGE
└─ NO  -> Is the key a small fixed set of discrete values?
          ├─ YES -> PARTITION BY LIST
          └─ NO  -> PARTITION BY HASH (modulus + remainder)
```

### Will a query be pruned :

```
Does the WHERE clause filter on the partition key (or a key column)?
├─ NO  -> every partition is scanned ; rewrite the query or repartition
└─ YES -> Is the predicate a constant or a bound parameter?
          ├─ constant        -> plan-time pruning (children absent in EXPLAIN)
          └─ parameter / join -> runtime pruning v11+ (loops=0 in EXPLAIN ANALYZE)
```

### Adding a partition : direct vs attach :

```
Is the partition empty (future time window)?
├─ YES -> CREATE TABLE ... PARTITION OF parent FOR VALUES ...   (simple)
└─ NO (loading existing data first)
   └─ Create standalone table + matching CHECK constraint, load, then
      ATTACH PARTITION. The CHECK lets ATTACH skip the validation scan.
```

### Manual partitions vs pg_partman :

```
Rolling time window needing new partitions + old-data retention forever?
├─ YES -> pg_partman : create_parent + run_maintenance_proc (background worker)
└─ NO (fixed, known set of partitions) -> create them manually
```

## Patterns :

### Pattern 1 : RANGE-partitioned table for time-series

ALWAYS : partition time-series tables by the timestamp/date column.
NEVER : leave gaps between range partitions unless a DEFAULT partition exists.

```sql
CREATE TABLE measurement (
    city_id   int  NOT NULL,
    logdate   date NOT NULL,
    peaktemp  int,
    unitsales int
) PARTITION BY RANGE (logdate);

-- Bounds : lower inclusive, upper exclusive (no overlap, no gap)
CREATE TABLE measurement_2026m01 PARTITION OF measurement
    FOR VALUES FROM ('2026-01-01') TO ('2026-02-01');
CREATE TABLE measurement_2026m02 PARTITION OF measurement
    FOR VALUES FROM ('2026-02-01') TO ('2026-03-01');
```

WHY : `FROM ('2026-01-01') TO ('2026-02-01')` holds Jan only. The exclusive
upper bound means `2026-02-01` lands in the next partition, never both. A row
whose `logdate` matches no partition raises SQLSTATE `23514` unless a DEFAULT
partition catches it.

### Pattern 2 : LIST and HASH partitioning

ALWAYS : use LIST for a known discrete set, HASH for even spread.
NEVER : use HASH when you also need to drop data by range : hash partitions
have no time meaning.

```sql
-- LIST : one partition per region group
CREATE TABLE sale (region text NOT NULL, amount numeric)
    PARTITION BY LIST (region);
CREATE TABLE sale_eu PARTITION OF sale FOR VALUES IN ('NL', 'DE', 'FR');
CREATE TABLE sale_us PARTITION OF sale FOR VALUES IN ('US', 'CA');

-- HASH : four partitions, even distribution by order_id
CREATE TABLE orders (order_id bigint NOT NULL, total numeric)
    PARTITION BY HASH (order_id);
CREATE TABLE orders_h0 PARTITION OF orders FOR VALUES WITH (MODULUS 4, REMAINDER 0);
CREATE TABLE orders_h1 PARTITION OF orders FOR VALUES WITH (MODULUS 4, REMAINDER 1);
CREATE TABLE orders_h2 PARTITION OF orders FOR VALUES WITH (MODULUS 4, REMAINDER 2);
CREATE TABLE orders_h3 PARTITION OF orders FOR VALUES WITH (MODULUS 4, REMAINDER 3);
```

WHY : LIST routing is exact-match on the listed values. HASH requires every
remainder `0 .. modulus-1` to have a partition, or some rows have nowhere to
go. HASH gives balance but no range semantics, so you cannot drop "old" data.

### Pattern 3 : DEFAULT partition as a catch-all

ALWAYS : add a DEFAULT partition when the full key domain is not pre-covered.
NEVER : rely on the DEFAULT partition for hot data : it cannot be pruned by
range and grows unbounded.

```sql
CREATE TABLE measurement_default PARTITION OF measurement DEFAULT;
```

WHY : without a DEFAULT partition, inserting a row that matches no partition
fails with `23514` (`check_violation`). The DEFAULT partition absorbs it. But
adding a new normal partition later must scan the DEFAULT partition to confirm
no row belongs in the new range : keep the DEFAULT partition small.

### Pattern 4 : Partition pruning requires the key in WHERE

ALWAYS : put a partition-key predicate in `WHERE` so pruning eliminates children.
NEVER : assume partitioning speeds up a query that does not filter on the key.

```sql
-- Pruned : only Jan + Feb partitions appear in the plan
EXPLAIN SELECT count(*) FROM measurement
WHERE logdate >= DATE '2026-01-01' AND logdate < DATE '2026-03-01';

-- NOT pruned : no logdate filter, every partition is scanned
EXPLAIN SELECT count(*) FROM measurement WHERE peaktemp > 30;
```

WHY : `enable_partition_pruning` is `on` by default. The planner removes
children whose bounds cannot satisfy a partition-key predicate. A predicate on
a non-key column (`peaktemp`) prunes nothing. For parameterised plans and
nested-loop joins, runtime pruning (v11+) prunes during execution : a pruned
child shows `(never executed)` or `loops=0` in `EXPLAIN ANALYZE`.

### Pattern 5 : ATTACH with a matching CHECK to skip the validation scan

ALWAYS : add a CHECK constraint matching the future bounds before ATTACH.
NEVER : ATTACH a populated table without that CHECK : ATTACH then full-scans
the whole table under a strong lock to verify every row.

```sql
-- 1. Standalone table, same shape as the parent
CREATE TABLE measurement_2026m03
    (LIKE measurement INCLUDING DEFAULTS INCLUDING CONSTRAINTS);

-- 2. CHECK constraint matching the target bounds
ALTER TABLE measurement_2026m03 ADD CONSTRAINT m2026m03
    CHECK (logdate >= DATE '2026-03-01' AND logdate < DATE '2026-04-01');

-- 3. Load and verify data into the standalone table here

-- 4. ATTACH : the CHECK proves all rows fit, so no scan is needed
ALTER TABLE measurement ATTACH PARTITION measurement_2026m03
    FOR VALUES FROM ('2026-03-01') TO ('2026-04-01');

-- 5. Drop the now-redundant CHECK
ALTER TABLE measurement_2026m03 DROP CONSTRAINT m2026m03;
```

WHY : ATTACH must guarantee every row of the new partition fits its bounds.
Without a matching CHECK it reads the entire table to verify. A pre-existing
CHECK that logically implies the bounds lets ATTACH take only a
`SHARE UPDATE EXCLUSIVE` lock and skip the scan.

### Pattern 6 : Indexes, constraints, and DETACH CONCURRENTLY

ALWAYS : create indexes on the parent ; include the partition key in unique keys.
NEVER : run `CREATE INDEX CONCURRENTLY` directly on a partitioned parent : it
is not supported.

```sql
-- Index on parent propagates to all current and future partitions
CREATE INDEX measurement_logdate_idx ON measurement (logdate);

-- Unique constraint MUST contain the partition key (logdate)
ALTER TABLE measurement ADD UNIQUE (city_id, logdate);

-- CONCURRENTLY index : per-partition, then attach to a parent-only index
CREATE INDEX ONLY_idx ON ONLY measurement (unitsales);
CREATE INDEX CONCURRENTLY m2026m01_us ON measurement_2026m01 (unitsales);
ALTER INDEX ONLY_idx ATTACH PARTITION m2026m01_us;

-- Detach old data without a long ACCESS EXCLUSIVE lock (v14+)
ALTER TABLE measurement DETACH PARTITION measurement_2026m01 CONCURRENTLY;
```

WHY : a unique constraint can only be enforced per partition, so it must
include the partition key (`UNIQUE (city_id)` alone is rejected with `0A000`).
`CREATE INDEX` on the parent cannot run `CONCURRENTLY` : build per-partition
indexes concurrently and `ATTACH` them. `DETACH ... CONCURRENTLY` (v14+) frees
a partition with a weak lock so reads continue.

### Pattern 7 : Automate partitions and retention with pg_partman

ALWAYS : use pg_partman for rolling time windows that need ongoing maintenance.
NEVER : hand-create daily partitions forever : you will miss one and inserts
will fail.

```sql
CREATE SCHEMA partman;
CREATE EXTENSION pg_partman SCHEMA partman;

-- 5.x : interval is a plain string ; p_type defaults to 'range'
SELECT partman.create_parent(
    p_parent_table := 'public.measurement',
    p_control      := 'logdate',
    p_interval     := '1 month'
);

-- Configure retention : drop partitions with data older than 6 months
UPDATE partman.part_config
SET retention = '6 months', retention_keep_table = false
WHERE parent_table = 'public.measurement';

-- The background worker calls this on a schedule ; run manually to test
CALL partman.run_maintenance_proc();
```

WHY : `create_parent` registers the table in `partman.part_config` and
pre-creates upcoming partitions. `run_maintenance_proc` (driven by the
`pg_partman_bgw` background worker) creates new partitions ahead of time and
applies retention. pg_partman 5.x supports only declarative partitioning and
requires PostgreSQL 14 or newer.

## Anti-Patterns :

(Short list, full detail in references/anti-patterns.md)

- Query with no partition-key filter : every partition scanned, pruning dead
- Thousands of tiny partitions : planning overhead per query dominates
- `UNIQUE` / `PRIMARY KEY` omitting the partition key : SQLSTATE `0A000`
- ATTACH of a populated table without a matching CHECK : full validation scan
- Row matching no partition and no DEFAULT partition : SQLSTATE `23514`
- HASH partitioning when bulk range-drop of old data is needed : impossible
- `CREATE INDEX CONCURRENTLY` on a partitioned parent : not supported

## Reference Links :

- [references/methods.md](references/methods.md) : Full DDL and pg_partman API surface
- [references/examples.md](references/examples.md) : Working version-annotated code samples
- [references/anti-patterns.md](references/anti-patterns.md) : Anti-patterns with cause, symptom, fix

## See Also :

- postgres-impl-indexing : indexing strategy for partitioned tables
- postgres-impl-vacuum-autovacuum : VACUUM scales per partition, not per parent
- postgres-syntax-arrays-ranges : range bound semantics shared with RANGE partitions
- postgres-core-extensions : installing and managing pg_partman
- Vooronderzoek section : §8 (Partitioning)
- Official docs : https://www.postgresql.org/docs/17/ddl-partitioning.html
