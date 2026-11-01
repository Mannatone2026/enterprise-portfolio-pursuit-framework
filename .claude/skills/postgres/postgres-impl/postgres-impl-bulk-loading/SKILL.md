---
name: postgres-impl-bulk-loading
description: >
  Use when importing large datasets, choosing COPY vs INSERT, querying CSV files as tables, or setting up cross-database queries.
  Prevents row-by-row INSERT loops that are orders of magnitude slower than COPY, stale planner stats from skipping ANALYZE after load, and "permission denied" from server-side COPY on a client file (use \copy).
  Covers COPY FROM/TO/STDIN, CSV/TEXT/BINARY formats, COPY FREEZE, v17 ON_ERROR ignore + HEADER MATCH, v16 COPY DEFAULT, file_fdw for CSV-as-table, postgres_fdw cross-database queries, load optimization (drop indexes, UNLOGGED staging), prepared statements.
  Keywords: COPY, bulk load, COPY FROM STDIN, \copy, CSV import, COPY FREEZE, ON_ERROR, file_fdw, postgres_fdw, foreign data wrapper, IMPORT FOREIGN SCHEMA, import is slow, fastest way to load data, query csv as table, cross database query, insert loop slow
license: MIT
compatibility: "Designed for Claude Code. Requires PostgreSQL 15, 16, or 17."
metadata:
  author: OpenAEC-Foundation
  version: "1.0"
---

# postgres-impl-bulk-loading

## Quick Reference :

`COPY` is the fast path for bulk data : an order of magnitude faster than `INSERT`
because it bypasses per-row statement parsing and planning. Pick the transport by
where the file lives :

```
File on the DB server machine    -> COPY ... FROM '/abs/path'   (server-side)
File on the client machine       -> \copy ... FROM 'path'       (psql, client-side)
Data streamed by an app/driver   -> COPY ... FROM STDIN         (psycopg, JDBC, etc.)
```

Performance ladder, fastest first : `COPY` >> multi-row `INSERT` (one statement,
many `VALUES` rows) >> single-row `INSERT` in a loop. NEVER write a row-by-row
`INSERT` loop for bulk data.

Three load-time decisions that change throughput by 10x or more :
1. Drop secondary indexes and FK constraints before load, recreate after.
2. Load into an `UNLOGGED` staging table when the data is reproducible.
3. ALWAYS run `ANALYZE` after the load so the planner has real statistics.

For files-as-tables use `file_fdw` (read-only CSV foreign table). For querying or
writing another PostgreSQL database use `postgres_fdw`.

## When To Use This Skill :

ALWAYS use this skill when :
- Importing a CSV, TSV, or dump file into a table
- An import is slow and a row-by-row `INSERT` loop is the cause
- Choosing between `COPY`, multi-row `INSERT`, and single-row `INSERT`
- Loading dirty data that must skip bad rows (`ON_ERROR ignore`, v17+)
- Querying a CSV file directly as if it were a table (`file_fdw`)
- Running cross-database queries against another PostgreSQL instance (`postgres_fdw`)
- Tuning a one-time large load (index drop, `UNLOGGED`, `maintenance_work_mem`)

NEVER use this skill for :
- Logical backup and restore of whole databases (use postgres-impl-backup-restore)
- Online schema changes during a load (use postgres-impl-zero-downtime-migration)
- Streaming changes between live databases (use postgres-impl-logical-replication)

## Decision Trees :

### COPY vs INSERT :

```
Loading > ~1000 rows from a file or stream?
├── YES -> COPY (server-side, \copy, or FROM STDIN). Done.
└── NO  -> INSERT
          ├── Many rows, one statement     -> INSERT ... VALUES (..),(..),(..)
          └── Repeated parameterized inserts from app
                                           -> prepared statement + batch
                                              (psycopg execute_batch / executemany)
```

### Server-side COPY vs client-side \copy :

```
Where is the data file?
├── On the database server's filesystem, readable by the postgres OS user
│      -> COPY t FROM '/abs/path' ...
│         Requires superuser OR membership of pg_read_server_files.
│         "permission denied" / SQLSTATE 42501 means you do NOT have it.
├── On the client / app machine running psql
│      -> \copy t FROM 'path' ...   (psql meta-command, no server file access)
└── Produced by an application or driver
       -> COPY t FROM STDIN ...     (driver streams the bytes)
```

### file_fdw vs postgres_fdw vs COPY :

```
What is the source?
├── A CSV/text file you want to QUERY repeatedly without importing
│      -> file_fdw : CREATE FOREIGN TABLE over the file (READ-ONLY)
├── A CSV/text file you want to LOAD into a real table
│      -> COPY (or file_fdw foreign table + INSERT ... SELECT for transform)
└── Another PostgreSQL database (query or write)
       -> postgres_fdw : CREATE SERVER + USER MAPPING + IMPORT FOREIGN SCHEMA
```

## Patterns :

### Pattern 1 : COPY FROM STDIN as the application import path

ALWAYS stream bulk data from an application via `COPY ... FROM STDIN`.
NEVER issue one `INSERT` per row from application code.

```sql
-- Server-side from a file readable by the postgres process :
COPY events (id, ts, payload) FROM '/var/lib/pg/import/events.csv'
  WITH (FORMAT csv, HEADER true);

-- From a driver (psycopg, JDBC) the statement is :
COPY events (id, ts, payload) FROM STDIN WITH (FORMAT csv);
```

WHY : `COPY` parses the table descriptor once and appends tuples in tight loops.
A row-by-row `INSERT` re-parses and re-plans every statement and pays a network
round trip per row. The gap is one to two orders of magnitude.

### Pattern 2 : CSV import with explicit format options

ALWAYS set `FORMAT csv` and the matching `DELIMITER`, `QUOTE`, `NULL`, `HEADER`
options explicitly. NEVER rely on `text` format defaults for a comma-separated file.

```sql
COPY sales FROM '/import/sales.csv'
  WITH (FORMAT csv, DELIMITER ',', QUOTE '"', NULL '', HEADER true,
        FORCE_NULL (notes), ENCODING 'UTF-8');
```

WHY : the default `text` format is tab-separated and uses `\N` for NULL, not the
empty string a CSV uses. `FORCE_NULL (col)` makes a quoted empty string `""` read
as SQL `NULL` instead of an empty string. See references/methods.md for every
option and its format restriction.

### Pattern 3 : COPY FREEZE for write-once tables

ALWAYS create or `TRUNCATE` the target table in the SAME transaction as a
`COPY ... FREEZE`. NEVER run `COPY FREEZE` on a pre-existing populated table.

```sql
BEGIN;
TRUNCATE archive_2025;          -- or CREATE TABLE, in this same transaction
COPY archive_2025 FROM '/import/archive.csv' WITH (FORMAT csv, FREEZE true);
COMMIT;
```

WHY : `FREEZE` marks rows frozen immediately so no later `VACUUM` must rewrite
them. PostgreSQL only allows it when the table was created or truncated in the
current subtransaction, there are no open cursors, and no older snapshots. The
frozen rows become visible to all sessions on commit, violating normal MVCC
visibility rules. `COPY FREEZE` is not allowed on a partitioned table.

### Pattern 4 : v17 ON_ERROR ignore for dirty data

ALWAYS use `ON_ERROR ignore` (v17+) when loading data that may contain malformed
rows you want skipped instead of aborting the whole load.

```sql
-- v17+ : skip rows that fail type conversion, report how many were skipped
COPY raw_feed FROM '/import/feed.csv'
  WITH (FORMAT csv, HEADER true, ON_ERROR ignore, LOG_VERBOSITY verbose);
```

WHY : without `ON_ERROR ignore` a single bad row aborts the entire `COPY` and
rolls back every row already loaded. `LOG_VERBOSITY verbose` emits a per-row
NOTICE with line number and column for each skipped row. `ON_ERROR` and
`LOG_VERBOSITY` only apply to `text` and `csv` formats and to `COPY FROM`.

### Pattern 5 : Bulk-load optimization workflow

ALWAYS drop secondary indexes and FK constraints before a large load and recreate
them after. NEVER load millions of rows into a fully-indexed table.

```sql
BEGIN;
ALTER TABLE big SET (autovacuum_enabled = false);
SET LOCAL maintenance_work_mem = '2GB';
DROP INDEX big_secondary_idx;
COPY big FROM '/import/big.csv' WITH (FORMAT csv, HEADER true);
COMMIT;

CREATE INDEX CONCURRENTLY big_secondary_idx ON big (col);
ALTER TABLE big SET (autovacuum_enabled = true);
VACUUM ANALYZE big;             -- planner has zero stats until this runs
```

WHY : maintaining each index during the load forces a B-tree insert and possible
page split per row. Building the index once after the load is a single sorted
pass and uses `maintenance_work_mem`. For reproducible ETL data, load into a
`CREATE UNLOGGED TABLE staging` to skip WAL entirely, then `INSERT ... SELECT`
into the real table. See references/examples.md for the full workflow.

### Pattern 6 : file_fdw to query a CSV as a table

ALWAYS use `file_fdw` to query a CSV file in place when you do not need to import
it. NEVER attempt to write through a `file_fdw` foreign table : it is read-only.

```sql
CREATE EXTENSION IF NOT EXISTS file_fdw;
CREATE SERVER files FOREIGN DATA WRAPPER file_fdw;

CREATE FOREIGN TABLE ext_sales (id int, amount numeric, ts timestamptz)
  SERVER files
  OPTIONS (filename '/import/sales.csv', format 'csv', header 'true');

SELECT sum(amount) FROM ext_sales WHERE ts >= '2026-01-01';
```

WHY : `file_fdw` reads the file on every query (no copy, no storage) which is
ideal for ad-hoc inspection or `INSERT ... SELECT` transforms. The file is read by
the server process, so the path rules match server-side `COPY`.

### Pattern 7 : postgres_fdw for cross-database queries

ALWAYS create `SERVER`, `USER MAPPING`, then `IMPORT FOREIGN SCHEMA` to expose a
remote PostgreSQL database. ALWAYS verify pushdown with `EXPLAIN VERBOSE`.

```sql
CREATE EXTENSION IF NOT EXISTS postgres_fdw;

CREATE SERVER remote_db FOREIGN DATA WRAPPER postgres_fdw
  OPTIONS (host 'db2.internal', port '5432', dbname 'analytics',
           use_remote_estimate 'true');

CREATE USER MAPPING FOR current_user SERVER remote_db
  OPTIONS (user 'reporter', password 'secret');

IMPORT FOREIGN SCHEMA public LIMIT TO (orders, customers)
  FROM SERVER remote_db INTO remote;

EXPLAIN VERBOSE SELECT * FROM remote.orders WHERE total > 1000;
```

WHY : `postgres_fdw` pushes `WHERE`, `JOIN`, aggregate, sort, and `LIMIT` to the
remote server when the operators are `IMMUTABLE` and built-in. `use_remote_estimate
'true'` makes the planner ask the remote for cost estimates so it chooses pushdown
correctly. For bulk `INSERT` through a foreign table, set `batch_size` on the
server (default `1`) so rows are sent in batches. See references/methods.md.

## Anti-Patterns :

(Full cause + symptom + fix in references/anti-patterns.md)

- Row-by-row `INSERT` loop for bulk data : orders of magnitude slower than `COPY`
- Server-side `COPY` on a client file : SQLSTATE 42501 `insufficient_privilege`
- `COPY` into a fully-indexed table : slow load plus index bloat
- Skipping `ANALYZE` after a bulk load : planner picks Seq Scan on stale stats
- `COPY FREEZE` on a pre-existing table : `FREEZE` is silently ignored
- `text` format defaults used on a CSV file : SQLSTATE 22P04 `bad_copy_file_format`
- `postgres_fdw` without `use_remote_estimate` : full table fetched, no pushdown

## Reference Links :

- [references/methods.md](references/methods.md) : Full COPY grammar, every option, file_fdw and postgres_fdw API, load-tuning GUCs
- [references/examples.md](references/examples.md) : Working code samples, version-annotated, including the full optimization workflow and driver-side COPY
- [references/anti-patterns.md](references/anti-patterns.md) : Anti-patterns with cause, symptom, SQLSTATE, and fix

## See Also :

- postgres-impl-indexing-strategy : index drop/recreate strategy around loads
- postgres-impl-partitioning : load into a standalone table then `ATTACH PARTITION`
- postgres-core-vacuum-bloat : why `ANALYZE` and `VACUUM` matter after a load
- Vooronderzoek section : §15 (Bulk Loading), §18 (Extensions Ecosystem)
- Official docs : https://www.postgresql.org/docs/17/sql-copy.html
- Official docs : https://www.postgresql.org/docs/17/file-fdw.html
- Official docs : https://www.postgresql.org/docs/17/postgres-fdw.html
