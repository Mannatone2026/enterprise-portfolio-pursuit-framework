# Bulk Loading : Anti-Patterns

Each entry : cause, symptom, SQLSTATE where applicable, and the fix pattern.
Verified against PostgreSQL 17 official documentation.

## AP-1 : Row-by-row INSERT loop for bulk data

CAUSE : application code issues one `INSERT` statement per row.

SYMPTOM : a load that should take seconds takes minutes or hours. Each statement
is parsed, planned, and executed separately, with a network round trip per row.

FIX : use `COPY ... FROM STDIN` from the driver, or `COPY ... FROM 'file'` for a
server-side file. `COPY` parses the table descriptor once and appends tuples in a
tight loop. The speedup is one to two orders of magnitude.

```sql
-- WRONG : N statements, N round trips
INSERT INTO events VALUES (1, ...);
INSERT INTO events VALUES (2, ...);
-- ... thousands more

-- RIGHT : one COPY
COPY events FROM STDIN WITH (FORMAT csv);
```

If `COPY` truly cannot be used (e.g. per-row `ON CONFLICT` logic), fall back to a
multi-row `INSERT ... VALUES (..),(..),(..)` or a prepared statement with
`executemany` / `execute_batch`. NEVER a single-row loop.

## AP-2 : Server-side COPY on a client file

CAUSE : `COPY t FROM '/path'` is run, but the path points at the client machine,
or the connecting role lacks `pg_read_server_files`.

SYMPTOM : `ERROR: must be superuser or have privileges of the pg_read_server_files
role to COPY from a file` (SQLSTATE 42501 `insufficient_privilege`), or
`ERROR: could not open file "/path" for reading: No such file or directory`
(SQLSTATE 58P01 `undefined_file`) because the file exists only on the client.

FIX : use the psql meta-command `\copy`, which wraps `COPY FROM STDIN` and opens
the file on the client side with the client user's permissions. No server role
membership is needed.

```
-- WRONG : server cannot see the client's home directory
COPY sales FROM '/home/me/sales.csv' WITH (FORMAT csv, HEADER true);

-- RIGHT : psql reads the local file and streams it
\copy sales FROM '/home/me/sales.csv' WITH (FORMAT csv, HEADER true)
```

## AP-3 : COPY into a fully-indexed table

CAUSE : a large `COPY` runs against a table that keeps all its secondary indexes
and FK constraints during the load.

SYMPTOM : the load is slow and CPU-bound; each row forces a B-tree insert per
index plus possible page splits. Indexes end up bloated from the random insert
order.

FIX : drop secondary indexes and FK constraints before the load, recreate them
afterward with `CREATE INDEX CONCURRENTLY`. Building an index once is a single
sorted pass; maintaining it per row is not.

```sql
DROP INDEX big_secondary_idx;
COPY big FROM '/srv/import/big.csv' WITH (FORMAT csv, HEADER true);
CREATE INDEX CONCURRENTLY big_secondary_idx ON big (col);
```

## AP-4 : Skipping ANALYZE after a bulk load

CAUSE : the load finishes and the application starts querying immediately, before
`ANALYZE` runs.

SYMPTOM : queries that should use an index do a `Seq Scan`; join orders are wrong;
`EXPLAIN` shows row estimates wildly off from reality. The planner has zero or
stale statistics on the freshly loaded data.

FIX : ALWAYS run `VACUUM ANALYZE table` (or at minimum `ANALYZE table`) as the
final step of any bulk load. Autovacuum will eventually run `ANALYZE`, but not
before the first queries hit the table.

```sql
COPY big FROM '/srv/import/big.csv' WITH (FORMAT csv, HEADER true);
VACUUM ANALYZE big;   -- not optional
```

## AP-5 : COPY FREEZE on a pre-existing table

CAUSE : `COPY ... FREEZE true` is run against a table that was NOT created or
`TRUNCATE`d in the current transaction.

SYMPTOM : no error, but the rows are not actually frozen. A later `VACUUM` still
has to rewrite every tuple to freeze it, so the intended benefit is lost.

FIX : create or `TRUNCATE` the target inside the SAME transaction as the
`COPY FREEZE`. `FREEZE` is honored only when the table was created/truncated in
the current subtransaction, no cursors are open, and no older snapshots are held.
`COPY FREEZE` is never allowed on a partitioned table.

```sql
BEGIN;
TRUNCATE archive_2025;                       -- same transaction
COPY archive_2025 FROM '/srv/import/a.csv'
  WITH (FORMAT csv, HEADER true, FREEZE true);
COMMIT;
```

## AP-6 : text-format defaults used on a CSV file

CAUSE : `COPY t FROM 'file.csv'` is run without `FORMAT csv`, so PostgreSQL uses
`text` format : tab delimiter, `\N` for NULL, backslash escapes.

SYMPTOM : `ERROR: extra data after last expected column` or `ERROR: missing data
for column` (SQLSTATE 22P04 `bad_copy_file_format`), or commas appear inside a
single text column because the row was never split.

FIX : always pass `FORMAT csv` and the matching `DELIMITER`, `QUOTE`, `NULL`, and
`HEADER` options for a comma-separated file.

```sql
-- WRONG : text format cannot parse a comma-separated file
COPY sales FROM '/srv/import/sales.csv';

-- RIGHT
COPY sales FROM '/srv/import/sales.csv'
  WITH (FORMAT csv, DELIMITER ',', HEADER true);
```

## AP-7 : HEADER true assumed to validate column order

CAUSE : `HEADER true` is used and assumed to match header names to columns.

SYMPTOM : columns are loaded into the wrong target column with no error, because
`HEADER true` only SKIPS the first line; it does not check what it contains. A
reordered CSV silently corrupts the load.

FIX : on v17+, use `HEADER MATCH`, which verifies that the header names match the
target column list in order and aborts on a mismatch. On v15/v16, explicitly list
the column order in the `COPY` statement so the file's physical order is pinned.

```sql
-- v17+ : aborts if the header line is not exactly id,amount,ts
COPY sales (id, amount, ts) FROM '/srv/import/sales.csv'
  WITH (FORMAT csv, HEADER MATCH);
```

## AP-8 : A single bad row aborts a large load

CAUSE : `COPY FROM` on dirty data without `ON_ERROR ignore`; one unparseable row
fails the whole statement.

SYMPTOM : `ERROR: invalid input syntax for type ...` (SQLSTATE 22P02
`invalid_text_representation`) at row N, and every row already loaded is rolled
back.

FIX : on v17+, add `ON_ERROR ignore` to skip bad rows and `LOG_VERBOSITY verbose`
to get a per-row NOTICE. On v15/v16, load into an all-`text` staging table first,
then validate and cast with SQL before moving into the typed table.

```sql
-- v17+
COPY raw_feed FROM '/srv/import/feed.csv'
  WITH (FORMAT csv, HEADER true, ON_ERROR ignore, LOG_VERBOSITY verbose);
```

## AP-9 : postgres_fdw without use_remote_estimate

CAUSE : a foreign server is created without `use_remote_estimate 'true'`, so the
planner uses local cost guesses for remote tables.

SYMPTOM : `EXPLAIN VERBOSE` shows the whole remote table fetched and filtered
locally; `WHERE`, `JOIN`, and aggregate pushdown do not happen, so large result
sets cross the network.

FIX : set `use_remote_estimate 'true'` on the server (or per foreign table) so the
planner asks the remote for real estimates and chooses pushdown. Always confirm
the generated remote SQL with `EXPLAIN VERBOSE`.

```sql
ALTER SERVER analytics_db OPTIONS (ADD use_remote_estimate 'true');
EXPLAIN VERBOSE SELECT * FROM remote.orders WHERE total > 1000;
-- expect a "Remote SQL:" line that contains the WHERE clause
```

## AP-10 : Bulk INSERT through a foreign table with batch_size 1

CAUSE : rows are inserted into a `postgres_fdw` foreign table while `batch_size`
keeps its default of `1`.

SYMPTOM : a bulk `INSERT INTO remote.t SELECT ...` is slow; postgres_fdw sends one
row per network round trip to the remote server.

FIX : set `batch_size` on the server or foreign table so postgres_fdw groups rows
into one multi-row remote `INSERT` per round trip.

```sql
ALTER SERVER analytics_db OPTIONS (ADD batch_size '1000');
INSERT INTO remote.orders SELECT * FROM local_orders_staging;
```

## AP-11 : Attempting to write through a file_fdw table

CAUSE : `INSERT`, `UPDATE`, or `DELETE` is run against a `file_fdw` foreign table.

SYMPTOM : `ERROR: cannot insert into foreign table ...` — `file_fdw` tables are
read-only.

FIX : use `file_fdw` for reading and querying only. To persist or transform the
data, `COPY` the file into a real table, or `INSERT INTO real_table SELECT ...
FROM file_fdw_table`.

```sql
-- WRONG
INSERT INTO ext_sales VALUES (...);

-- RIGHT : materialize through INSERT ... SELECT
INSERT INTO sales SELECT * FROM ext_sales WHERE amount > 0;
```
