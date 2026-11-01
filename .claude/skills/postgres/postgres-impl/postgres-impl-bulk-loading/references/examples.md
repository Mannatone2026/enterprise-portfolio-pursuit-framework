# Bulk Loading : Examples

Working, version-annotated code samples. Every snippet is verified against
PostgreSQL 17 official documentation. Patterns marked `v15+` work on all
supported versions; `v16+` / `v17+` require that minimum.

## 1. COPY TO : export

```sql
-- v15+ : export a whole table to a server-side file
COPY country TO '/srv/export/country.txt';

-- v15+ : export with a pipe delimiter to the client connection
COPY country TO STDOUT (DELIMITER '|');

-- v15+ : export a query result, not a table
COPY (SELECT id, total FROM orders WHERE total > 1000)
  TO '/srv/export/big_orders.csv' WITH (FORMAT csv, HEADER true);

-- v15+ : export through a compression program
COPY country TO PROGRAM 'gzip > /srv/export/country.csv.gz'
  WITH (FORMAT csv, HEADER true);
```

## 2. COPY FROM : text format (default)

```sql
-- v15+ : tab-separated, \N means NULL, backslash escapes
COPY country FROM '/srv/import/country_data';

-- v15+ : restrict which columns the file supplies; others take their default
COPY country (id, name) FROM '/srv/import/partial.txt';
```

## 3. COPY FROM : CSV format

```sql
-- v15+ : standard CSV with a header line to skip
COPY sales FROM '/srv/import/sales.csv'
  WITH (FORMAT csv, HEADER true);

-- v15+ : European CSV : semicolon delimiter, "N/A" means NULL
COPY sales FROM '/srv/import/sales_eu.csv'
  WITH (FORMAT csv, DELIMITER ';', NULL 'N/A', HEADER true);

-- v15+ : a quoted empty string in column `notes` should read as SQL NULL
COPY sales FROM '/srv/import/sales.csv'
  WITH (FORMAT csv, HEADER true, FORCE_NULL (notes));

-- v17+ : treat every column's quoted-empty-string as NULL
COPY sales FROM '/srv/import/sales.csv'
  WITH (FORMAT csv, HEADER true, FORCE_NULL *);
```

## 4. COPY FROM : binary format

```sql
-- v15+ : binary is fastest and skips text parsing, but is not portable
-- across architectures or major versions. Produce and consume with COPY only.
COPY metrics TO   '/srv/export/metrics.bin' WITH (FORMAT binary);
COPY metrics FROM '/srv/import/metrics.bin' WITH (FORMAT binary);
```

## 5. v16 COPY DEFAULT : use the column default for a marker string

```sql
-- v16+ : input fields equal to '\D' get the column's DEFAULT expression
-- Table : CREATE TABLE t (id int, created timestamptz DEFAULT now(), note text);
COPY t FROM '/srv/import/t.csv'
  WITH (FORMAT csv, HEADER true, DEFAULT '\D');
-- A row "5,\D,hello" inserts id=5, created=now(), note='hello'
```

## 6. v17 HEADER MATCH : verify the header line

```sql
-- v17+ : aborts the COPY if the file header does not match the column list
COPY sales (id, amount, ts) FROM '/srv/import/sales.csv'
  WITH (FORMAT csv, HEADER MATCH);
-- File header line MUST be exactly : id,amount,ts
```

## 7. v17 ON_ERROR ignore : skip malformed rows

```sql
-- v17+ : bad rows are skipped, a NOTICE reports the skipped count
COPY raw_feed FROM '/srv/import/feed.csv'
  WITH (FORMAT csv, HEADER true, ON_ERROR ignore);

-- v17+ : add per-row detail (line number, column) for each skipped row
COPY raw_feed FROM '/srv/import/feed.csv'
  WITH (FORMAT csv, HEADER true, ON_ERROR ignore, LOG_VERBOSITY verbose);
```

## 8. COPY FREEZE : write-once table

```sql
-- v15+ : FREEZE is honored only because TRUNCATE runs in this transaction
BEGIN;
TRUNCATE archive_2025;
COPY archive_2025 FROM '/srv/import/archive_2025.csv'
  WITH (FORMAT csv, HEADER true, FREEZE true);
COMMIT;

-- v15+ : equivalent with CREATE TABLE in the same transaction
BEGIN;
CREATE TABLE archive_2024 (id bigint, ts timestamptz, payload jsonb);
COPY archive_2024 FROM '/srv/import/archive_2024.csv'
  WITH (FORMAT csv, HEADER true, FREEZE true);
COMMIT;
```

## 9. psql \copy : client-side file

```
-- v15+ : run inside psql, the file is on the CLIENT machine
\copy sales FROM '/home/me/sales.csv' WITH (FORMAT csv, HEADER true)

-- v15+ : export a query result to a client-side file
\copy (SELECT * FROM sales WHERE ts > '2026-01-01') TO 'recent.csv' WITH (FORMAT csv, HEADER true)
```

Use `\copy` whenever the file lives on the machine running psql, or when the
connecting role lacks `pg_read_server_files` membership.

## 10. Full bulk-load optimization workflow

```sql
-- v15+ : load tens of millions of rows fast
BEGIN;

-- 1. stop autovacuum churning the table mid-load
ALTER TABLE big_facts SET (autovacuum_enabled = false);

-- 2. session memory + durability tradeoffs, auto-revert on COMMIT
SET LOCAL maintenance_work_mem = '2GB';
SET LOCAL synchronous_commit = off;

-- 3. drop secondary indexes (keep the primary key for dedup if needed)
DROP INDEX big_facts_region_idx;
DROP INDEX big_facts_ts_idx;

-- 4. the load itself
COPY big_facts FROM '/srv/import/big_facts.csv'
  WITH (FORMAT csv, HEADER true);

COMMIT;

-- 5. rebuild indexes after the load : one sorted pass each, no live blocking
CREATE INDEX CONCURRENTLY big_facts_region_idx ON big_facts (region);
CREATE INDEX CONCURRENTLY big_facts_ts_idx     ON big_facts (ts);

-- 6. re-enable autovacuum and give the planner real statistics
ALTER TABLE big_facts SET (autovacuum_enabled = true);
VACUUM ANALYZE big_facts;
```

## 11. UNLOGGED staging table for reproducible ETL

```sql
-- v15+ : UNLOGGED skips WAL entirely; safe because the source file can be replayed
CREATE UNLOGGED TABLE staging_sales (LIKE sales INCLUDING DEFAULTS);

COPY staging_sales FROM '/srv/import/sales.csv' WITH (FORMAT csv, HEADER true);

-- transform / dedup while moving into the real (logged) table
INSERT INTO sales
SELECT DISTINCT ON (id) * FROM staging_sales ORDER BY id, ts DESC;

DROP TABLE staging_sales;
ANALYZE sales;
```

## 12. Load into a standalone table then ATTACH as a partition

```sql
-- v15+ : avoids loading directly into the live partitioned parent
CREATE TABLE sales_2026_01 (LIKE sales INCLUDING ALL);

COPY sales_2026_01 FROM '/srv/import/sales_2026_01.csv'
  WITH (FORMAT csv, HEADER true);

-- a CHECK matching the bound lets ATTACH skip its validation scan
ALTER TABLE sales_2026_01
  ADD CONSTRAINT sales_2026_01_ck
  CHECK (ts >= '2026-01-01' AND ts < '2026-02-01');

ALTER TABLE sales ATTACH PARTITION sales_2026_01
  FOR VALUES FROM ('2026-01-01') TO ('2026-02-01');
```

## 13. file_fdw : query a CSV file as a table

```sql
-- v15+ : read-only foreign table over a server-side CSV
CREATE EXTENSION IF NOT EXISTS file_fdw;
CREATE SERVER files FOREIGN DATA WRAPPER file_fdw;

CREATE FOREIGN TABLE ext_sales (
  id      integer,
  amount  numeric,
  ts      timestamptz,
  notes   text
)
  SERVER files
  OPTIONS (filename '/srv/import/sales.csv', format 'csv', header 'true');

-- query the file directly, no import needed
SELECT date_trunc('month', ts) AS m, sum(amount)
FROM ext_sales
GROUP BY 1 ORDER BY 1;

-- column-level option : quoted "" in `notes` reads as NULL
CREATE FOREIGN TABLE ext_films (
  code   char(5),
  title  text,
  rating text OPTIONS (force_null 'true')
)
  SERVER files
  OPTIONS (filename '/srv/import/films.csv', format 'csv', header 'true');
```

## 14. postgres_fdw : cross-database query and bulk insert

```sql
-- v15+ : set up the wrapper, server, and credentials
CREATE EXTENSION IF NOT EXISTS postgres_fdw;

CREATE SERVER analytics_db FOREIGN DATA WRAPPER postgres_fdw
  OPTIONS (host 'db2.internal', port '5432', dbname 'analytics',
           use_remote_estimate 'true', batch_size '1000');

CREATE USER MAPPING FOR current_user SERVER analytics_db
  OPTIONS (user 'reporter', password 'secret');

-- import remote tables instead of declaring each foreign table by hand
IMPORT FOREIGN SCHEMA public LIMIT TO (orders, customers)
  FROM SERVER analytics_db INTO remote;

-- confirm the WHERE clause and join are sent to the remote server
EXPLAIN VERBOSE
SELECT o.id, c.name
FROM remote.orders o JOIN remote.customers c ON c.id = o.customer_id
WHERE o.total > 1000;
-- Look for "Remote SQL:" lines containing the WHERE and JOIN.

-- bulk INSERT through a foreign table : batch_size '1000' on the server
-- makes postgres_fdw send 1000 rows per round trip instead of one.
INSERT INTO remote.orders SELECT * FROM local_orders_staging;
```

## 15. Driver-side COPY FROM STDIN (psycopg 3)

```python
# psycopg 3 : stream rows straight into COPY, never one INSERT per row
import psycopg

with psycopg.connect("dbname=app") as conn, conn.cursor() as cur:
    with cur.copy("COPY events (id, ts, payload) FROM STDIN") as copy:
        for row in source_rows:          # row = (id, ts, payload)
            copy.write_row(row)
    conn.commit()

# When COPY does not fit (e.g. ON CONFLICT upserts), batch with executemany
# so the statement is parsed and planned once, not per row.
with psycopg.connect("dbname=app") as conn, conn.cursor() as cur:
    cur.executemany(
        "INSERT INTO events (id, ts, payload) VALUES (%s, %s, %s)"
        " ON CONFLICT (id) DO NOTHING",
        source_rows,
    )
    conn.commit()
```
