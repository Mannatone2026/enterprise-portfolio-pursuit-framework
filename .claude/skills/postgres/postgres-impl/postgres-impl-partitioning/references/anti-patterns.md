# Anti-Patterns : Declarative Partitioning and pg_partman

Each entry : cause, symptom, fix-pattern, and SQLSTATE where a specific error
code applies. Verified against PostgreSQL 17 documentation.

## AP-1 : Querying without a partition-key filter

**Cause** : a partitioned table is queried with a `WHERE` clause that filters
only on non-key columns.

**Symptom** : `EXPLAIN` shows an `Append` over every partition ; the query is
no faster than before partitioning, and often slower because of the per-child
planning overhead.

**Fix** : add a partition-key predicate, or, if queries genuinely never filter
on the chosen key, repartition on a column the workload actually filters on.

```sql
-- WRONG : peaktemp is not the partition key, nothing is pruned
SELECT count(*) FROM measurement WHERE peaktemp > 30;

-- RIGHT : logdate is the partition key, irrelevant partitions are pruned
SELECT count(*) FROM measurement
WHERE logdate >= DATE '2026-02-01' AND logdate < DATE '2026-03-01'
  AND peaktemp > 30;
```

**SQLSTATE** : none ; correct results, no pruning benefit.

## AP-2 : Too many tiny partitions

**Cause** : the partition interval is far smaller than the data volume needs,
for example hourly partitions for a low-traffic table, producing thousands of
partitions.

**Symptom** : every query is slow to plan because the planner inspects each
partition's bounds ; `pg_class` bloats ; autovacuum has thousands of tiny
tables to track. The overhead grows even for queries that prune well.

**Fix** : pick an interval that yields a manageable partition count. Aim for
partitions large enough to matter but few enough that planning stays cheap.
Sub-partition only when one dimension is not enough.

```sql
-- WRONG : hourly partitions for a table with a few thousand rows per day
-- RIGHT : monthly (or daily for high volume) keeps the count reasonable
SELECT partman.create_parent('public.measurement', 'logdate', '1 month');
```

**SQLSTATE** : none ; a planning-performance problem.

## AP-3 : Unique or primary key omitting the partition key

**Cause** : a `UNIQUE` or `PRIMARY KEY` constraint is declared on a partitioned
table without all partition-key columns.

**Symptom** : `CREATE TABLE` or `ALTER TABLE` fails immediately with
`unique constraint on partitioned table must include all partitioning columns`.

**Fix** : add every partition-key column to the constraint. Uniqueness is
enforced per partition, so the key must let PostgreSQL route a row to exactly
one partition.

```sql
-- WRONG : logdate is the partition key but missing from the constraint
ALTER TABLE measurement ADD PRIMARY KEY (city_id);

-- RIGHT : include the partition key
ALTER TABLE measurement ADD PRIMARY KEY (city_id, logdate);
```

**SQLSTATE** : `0A000` (`feature_not_supported`).

## AP-4 : ATTACH of a populated table without a matching CHECK

**Cause** : a table already holding rows is attached with
`ALTER TABLE ... ATTACH PARTITION` and no CHECK constraint that implies the
partition bounds.

**Symptom** : the `ATTACH` statement holds a strong lock and reads the entire
table to verify every row fits the bounds. On a large table this blocks writes
for a long time.

**Fix** : add a CHECK constraint matching the future bounds before attaching ;
ATTACH then proves validity from the constraint and skips the scan. Drop the
CHECK afterwards.

```sql
ALTER TABLE measurement_2026m06 ADD CONSTRAINT m2026m06
    CHECK (logdate >= DATE '2026-06-01' AND logdate < DATE '2026-07-01');
ALTER TABLE measurement ATTACH PARTITION measurement_2026m06
    FOR VALUES FROM ('2026-06-01') TO ('2026-07-01');
ALTER TABLE measurement_2026m06 DROP CONSTRAINT m2026m06;
```

**SQLSTATE** : none if data is valid ; `23514` (`check_violation`) if any row
falls outside the declared bounds.

## AP-5 : Insert matching no partition and no DEFAULT partition

**Cause** : a row is inserted whose partition-key value falls outside every
defined partition, and the table has no DEFAULT partition.

**Symptom** : the insert fails with
`no partition of relation "..." found for row`.

**Fix** : either pre-create the partition covering that value, or add a DEFAULT
partition as a catch-all. Keep the DEFAULT partition small : it cannot be
pruned by range.

```sql
-- Add a catch-all so unmatched rows have a home
CREATE TABLE measurement_default PARTITION OF measurement DEFAULT;
```

**SQLSTATE** : `23514` (`check_violation`).

## AP-6 : HASH partitioning when bulk range-drop is needed

**Cause** : a time-series table is HASH-partitioned because hashing "spreads
the load evenly".

**Symptom** : old data cannot be removed by dropping a partition : each hash
partition contains rows from every time period. Retention becomes a slow
`DELETE` instead of an instant `DROP TABLE`.

**Fix** : partition time-series data by RANGE on the time column. Dropping a
month is then one `DROP TABLE`. Use HASH only when there is no range or list
dimension and even spread is the sole goal.

```sql
-- RIGHT for time-series : RANGE allows instant retention
CREATE TABLE measurement (...) PARTITION BY RANGE (logdate);
DROP TABLE measurement_2026m01;   -- drop a whole month instantly
```

**SQLSTATE** : none ; a strategy mismatch.

## AP-7 : CREATE INDEX CONCURRENTLY on a partitioned parent

**Cause** : a concurrent index build is attempted directly on the partitioned
parent : `CREATE INDEX CONCURRENTLY ON measurement (col)`.

**Symptom** : the statement fails with
`cannot create index on partitioned table "measurement" concurrently`.

**Fix** : create a parent-only template index with `ON ONLY`, build each
partition index `CONCURRENTLY`, then `ATTACH` them to the template.

```sql
CREATE INDEX measurement_col_idx ON ONLY measurement (col);
CREATE INDEX CONCURRENTLY measurement_2026m02_col ON measurement_2026m02 (col);
ALTER INDEX measurement_col_idx ATTACH PARTITION measurement_2026m02_col;
```

**SQLSTATE** : `0A000` (`feature_not_supported`).

## AP-8 : Forgetting to pre-create future partitions

**Cause** : partitions are created by hand for known dates, and nobody creates
the partition for the upcoming period.

**Symptom** : at the period boundary every insert into the new period fails
with `no partition of relation found for row` until someone notices.

**Fix** : automate creation with pg_partman. `create_parent` registers the
table and `run_maintenance_proc` pre-creates upcoming partitions ahead of the
boundary (`p_premake` controls how far ahead).

```sql
SELECT partman.create_parent('public.measurement', 'logdate', '1 month');
-- The pg_partman_bgw worker calls run_maintenance_proc on a schedule
```

**SQLSTATE** : `23514` (`check_violation`) on the failing insert.
