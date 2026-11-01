# Examples : Declarative Partitioning and pg_partman

Working, version-annotated samples. Verified against PostgreSQL 17 docs and
pg_partman 5.x docs. Samples run on PostgreSQL 15, 16, and 17 unless a note
marks otherwise. pg_partman requires PostgreSQL 14+.

## RANGE Partitioning Examples

### Create a time-series partitioned table

```sql
CREATE TABLE measurement (
    city_id   int  NOT NULL,
    logdate   date NOT NULL,
    peaktemp  int,
    unitsales int
) PARTITION BY RANGE (logdate);

CREATE TABLE measurement_2026m01 PARTITION OF measurement
    FOR VALUES FROM ('2026-01-01') TO ('2026-02-01');
CREATE TABLE measurement_2026m02 PARTITION OF measurement
    FOR VALUES FROM ('2026-02-01') TO ('2026-03-01');
CREATE TABLE measurement_2026m03 PARTITION OF measurement
    FOR VALUES FROM ('2026-03-01') TO ('2026-04-01');
```

### Unbounded lower partition with MINVALUE

```sql
-- Everything before 2026 in one historical partition
CREATE TABLE measurement_pre2026 PARTITION OF measurement
    FOR VALUES FROM (MINVALUE) TO ('2026-01-01');
```

### Partition with storage options

```sql
CREATE TABLE measurement_2026m04 PARTITION OF measurement
    FOR VALUES FROM ('2026-04-01') TO ('2026-05-01')
    WITH (parallel_workers = 4)
    TABLESPACE fasttablespace;
```

## LIST and HASH Partitioning Examples

### LIST partitioning by category

```sql
CREATE TABLE sale (
    id     bigint GENERATED ALWAYS AS IDENTITY,
    region text NOT NULL,
    amount numeric(12,2) NOT NULL
) PARTITION BY LIST (region);

CREATE TABLE sale_eu PARTITION OF sale FOR VALUES IN ('NL', 'DE', 'FR', 'BE');
CREATE TABLE sale_us PARTITION OF sale FOR VALUES IN ('US', 'CA', 'MX');
CREATE TABLE sale_other PARTITION OF sale DEFAULT;
```

### HASH partitioning for even distribution

```sql
CREATE TABLE orders (
    order_id bigint NOT NULL,
    total    numeric(12,2)
) PARTITION BY HASH (order_id);

-- All four remainders must exist
CREATE TABLE orders_h0 PARTITION OF orders FOR VALUES WITH (MODULUS 4, REMAINDER 0);
CREATE TABLE orders_h1 PARTITION OF orders FOR VALUES WITH (MODULUS 4, REMAINDER 1);
CREATE TABLE orders_h2 PARTITION OF orders FOR VALUES WITH (MODULUS 4, REMAINDER 2);
CREATE TABLE orders_h3 PARTITION OF orders FOR VALUES WITH (MODULUS 4, REMAINDER 3);
```

## DEFAULT Partition and Sub-partitioning

### DEFAULT catch-all partition

```sql
CREATE TABLE measurement_default PARTITION OF measurement DEFAULT;

-- Without it, this insert fails (SQLSTATE 23514) if no partition matches
INSERT INTO measurement (city_id, logdate, peaktemp)
VALUES (1, '2030-12-25', 5);
```

### Sub-partition a partition

```sql
-- A monthly partition that is itself partitioned by peaktemp range
CREATE TABLE measurement_2026m05 PARTITION OF measurement
    FOR VALUES FROM ('2026-05-01') TO ('2026-06-01')
    PARTITION BY RANGE (peaktemp);

CREATE TABLE measurement_2026m05_cold PARTITION OF measurement_2026m05
    FOR VALUES FROM (MINVALUE) TO (20);
CREATE TABLE measurement_2026m05_warm PARTITION OF measurement_2026m05
    FOR VALUES FROM (20) TO (MAXVALUE);
```

## Partition Pruning Examples

### Pruned query (partition key in WHERE)

```sql
EXPLAIN
SELECT count(*) FROM measurement
WHERE logdate >= DATE '2026-02-01' AND logdate < DATE '2026-03-01';
-- Aggregate
--   ->  Seq Scan on measurement_2026m02 measurement
-- Only the February partition appears : others were pruned at plan time.
```

### Non-pruned query (no partition key filter)

```sql
EXPLAIN
SELECT count(*) FROM measurement WHERE peaktemp > 30;
-- Append
--   ->  Seq Scan on measurement_2026m01
--   ->  Seq Scan on measurement_2026m02
--   ->  Seq Scan on measurement_2026m03
--   ->  Seq Scan on measurement_default
-- Every partition scanned : peaktemp is not the partition key.
```

### Partition-wise join (v11+)

```sql
SET enable_partitionwise_join = on;
EXPLAIN
SELECT *
FROM measurement m JOIN measurement_archive a USING (logdate, city_id);
-- Matching partitions join pair by pair instead of as one big join.
```

## ATTACH and DETACH Examples

### Attach a pre-loaded table without a validation scan

```sql
-- 1. Standalone table mirroring the parent
CREATE TABLE measurement_2026m06
    (LIKE measurement INCLUDING DEFAULTS INCLUDING CONSTRAINTS);

-- 2. CHECK constraint matching the future bounds
ALTER TABLE measurement_2026m06 ADD CONSTRAINT m2026m06
    CHECK (logdate >= DATE '2026-06-01' AND logdate < DATE '2026-07-01');

-- 3. Bulk-load data into measurement_2026m06 here

-- 4. ATTACH : the CHECK proves all rows fit, ATTACH skips the scan
ALTER TABLE measurement ATTACH PARTITION measurement_2026m06
    FOR VALUES FROM ('2026-06-01') TO ('2026-07-01');

-- 5. Drop the redundant CHECK
ALTER TABLE measurement_2026m06 DROP CONSTRAINT m2026m06;
```

### Detach old data with a weak lock (v14+)

```sql
-- Reads against measurement continue during the detach
ALTER TABLE measurement DETACH PARTITION measurement_2026m01 CONCURRENTLY;

-- The detached table is now a standalone table : archive or drop it
DROP TABLE measurement_2026m01;
```

## Index and Constraint Examples

### Index on the parent propagates to all partitions

```sql
CREATE INDEX measurement_logdate_idx ON measurement (logdate);
CREATE INDEX measurement_city_idx    ON measurement (city_id);
```

### Unique constraint must include the partition key

```sql
-- VALID : includes logdate, the partition key
ALTER TABLE measurement ADD UNIQUE (city_id, logdate);

-- INVALID : omits logdate -> ERROR, SQLSTATE 0A000
-- ALTER TABLE measurement ADD UNIQUE (city_id);
```

### Concurrent index build on a partitioned table

```sql
-- CONCURRENTLY is NOT allowed on the parent ; do it per partition
CREATE INDEX measurement_us_idx ON ONLY measurement (unitsales);

CREATE INDEX CONCURRENTLY measurement_2026m02_us
    ON measurement_2026m02 (unitsales);

ALTER INDEX measurement_us_idx
    ATTACH PARTITION measurement_2026m02_us;
```

## pg_partman Examples (PostgreSQL 14+)

### Install and create an automated partition set

```sql
CREATE SCHEMA partman;
CREATE EXTENSION pg_partman SCHEMA partman;

-- 5.x : interval is a plain string ; p_type defaults to 'range'
SELECT partman.create_parent(
    p_parent_table := 'public.measurement',
    p_control      := 'logdate',
    p_interval     := '1 month'
);
```

### Configure retention

```sql
-- Drop partitions whose data is older than 6 months
UPDATE partman.part_config
SET retention            = '6 months',
    retention_keep_table = false
WHERE parent_table = 'public.measurement';
```

### Run maintenance manually

```sql
-- Creates upcoming partitions and applies retention
CALL partman.run_maintenance_proc();

-- Maintain a single parent only
SELECT partman.run_maintenance('public.measurement');
```
