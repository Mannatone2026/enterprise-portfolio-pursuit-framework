# Bulk Loading : Methods Reference

API surface for `COPY`, `file_fdw`, `postgres_fdw`, and the GUCs that govern a
bulk load. Verified against PostgreSQL 17 official documentation.

## COPY grammar

```sql
COPY table_name [ ( column_name [, ...] ) ]
    FROM { 'filename' | PROGRAM 'command' | STDIN }
    [ [ WITH ] ( option [, ...] ) ]
    [ WHERE condition ]

COPY { table_name [ ( column_name [, ...] ) ] | ( query ) }
    TO { 'filename' | PROGRAM 'command' | STDOUT }
    [ [ WITH ] ( option [, ...] ) ]
```

- `FROM 'filename'` / `TO 'filename'` : server-side, absolute path on the DB host.
- `FROM STDIN` / `TO STDOUT` : data streams over the client connection.
- `PROGRAM 'command'` : server runs the shell command, reads/writes its stdio.
- `COPY (query) TO` : exports the result of any `SELECT` / `VALUES` / DML with
  `RETURNING`. The query form is `TO` only, never `FROM`.
- `WHERE condition` : filters rows on `COPY FROM` before they are inserted (v12+).

## COPY options

| Option | Valid values | Format | Direction | Notes |
|--------|--------------|--------|-----------|-------|
| `FORMAT` | `text`, `csv`, `binary` | all | both | default `text` |
| `FREEZE` | boolean | text/csv | FROM | see FREEZE rules below |
| `DELIMITER` | one-byte char | text/csv | both | default tab (text), `,` (csv) |
| `NULL` | string | text/csv | both | default `\N` (text), empty (csv) |
| `DEFAULT` | string | text/csv | FROM | v16+ : marker meaning "use column default" |
| `HEADER` | boolean OR `MATCH` | text/csv | both | `MATCH` is v17+ |
| `QUOTE` | one-byte char | csv | both | default `"` |
| `ESCAPE` | one-byte char | csv | both | default = `QUOTE` char |
| `FORCE_QUOTE` | `(cols)` or `*` | csv | TO | quote even non-NULL plain values |
| `FORCE_NOT_NULL` | `(cols)` or `*` | csv | FROM | `*` is v17+ |
| `FORCE_NULL` | `(cols)` or `*` | csv | FROM | `*` is v17+ |
| `ON_ERROR` | `stop`, `ignore` | text/csv | FROM | v17+ : default `stop` |
| `LOG_VERBOSITY` | `default`, `verbose` | all | FROM | v17+ |
| `ENCODING` | encoding name | all | both | default = client encoding |

### Version-gated options

- `DEFAULT 'string'` : v16+. When an input field equals the given string, the
  column's `DEFAULT` expression is used instead of the literal value.
- `HEADER MATCH` : v17+. Verifies that the header line's column names match the
  target column list in order. A mismatch aborts the `COPY`.
- `ON_ERROR ignore` : v17+. Rows that fail input conversion are skipped; a NOTICE
  reports the skipped count. `text`/`csv` only, `COPY FROM` only.
- `LOG_VERBOSITY verbose` : v17+. Emits a per-row NOTICE (line number, column)
  for each row skipped by `ON_ERROR ignore`.
- `FORCE_NULL *` / `FORCE_NOT_NULL *` : v17+ wildcard form applying to all columns.

### COPY FREEZE rules

`FREEZE true` is honored ONLY when ALL of these hold :
- The table was created or `TRUNCATE`d in the current subtransaction.
- No cursors are open in the transaction.
- No older snapshots are held by the transaction.

`COPY FREEZE` is NOT allowed on a partitioned table. Frozen rows become visible to
every session on commit, bypassing normal MVCC visibility. If the conditions are
not met, `FREEZE` is silently treated as a no-op (rows load unfrozen).

### Server-side access privileges

`COPY` naming a file or `PROGRAM` is allowed only to database superusers or to
roles granted one of :
- `pg_read_server_files` : for `COPY FROM 'filename'`
- `pg_write_server_files` : for `COPY TO 'filename'`
- `pg_execute_server_program` : for `COPY ... PROGRAM`

`COPY ... FROM STDIN` / `TO STDOUT` need no special role : the data moves over the
existing client connection. A client-side file is reached with the psql
meta-command `\copy`, which wraps `COPY ... FROM STDIN` / `TO STDOUT`.

## psql \copy meta-command

```
\copy table_name [ ( columns ) ] FROM 'filename' [ WITH ] ( options )
\copy table_name [ ( columns ) ] TO   'filename' [ WITH ] ( options )
\copy ( query ) TO 'filename' [ WITH ] ( options )
```

- Runs entirely inside psql : the file is opened by the psql process, so file
  permissions follow the client user, not the postgres OS user.
- Accepts the same option list as `COPY`.
- Must be written on a single logical line (psql meta-command parsing).
- No superuser or `pg_read_server_files` membership required.

## file_fdw

Read-only foreign-data wrapper over a server-side file or program output.

```sql
CREATE EXTENSION file_fdw;
CREATE SERVER server_name FOREIGN DATA WRAPPER file_fdw;
CREATE FOREIGN TABLE name ( column definitions )
  SERVER server_name
  OPTIONS ( option 'value' [, ...] );
```

### file_fdw table-level options

| Option | Meaning |
|--------|---------|
| `filename` | File path, relative to the data directory. `filename` OR `program` required. |
| `program` | Command to execute; its stdout is read. `filename` OR `program` required. |
| `format` | Data format, same values as COPY `FORMAT` (`text`, `csv`, `binary`). |
| `header` | Boolean : input has a header line. |
| `delimiter` | Field delimiter character. |
| `quote` | Quote character (csv). |
| `escape` | Escape character (csv). |
| `null` | String representing SQL NULL. |
| `encoding` | File encoding. |

### file_fdw column-level options

| Option | Meaning |
|--------|---------|
| `force_not_null` | Boolean : never match this column against the null string. |
| `force_null` | Boolean : a quoted value equal to the null string reads as NULL. |

`file_fdw` foreign tables are READ-ONLY : `INSERT`/`UPDATE`/`DELETE` fail.

## postgres_fdw

Foreign-data wrapper to query and write a remote PostgreSQL database.

```sql
CREATE EXTENSION postgres_fdw;

CREATE SERVER server_name FOREIGN DATA WRAPPER postgres_fdw
  OPTIONS (host '...', port '5432', dbname '...');

CREATE USER MAPPING FOR local_role SERVER server_name
  OPTIONS (user 'remote_user', password '...');

CREATE FOREIGN TABLE name ( columns )
  SERVER server_name
  OPTIONS (schema_name '...', table_name '...');

IMPORT FOREIGN SCHEMA remote_schema [ LIMIT TO (...) | EXCEPT (...) ]
  FROM SERVER server_name INTO local_schema;
```

### Key server / table options

| Option | Type | Default | Effect |
|--------|------|---------|--------|
| `host` | string | local socket | remote hostname |
| `port` | integer | 5432 | remote port |
| `dbname` | string | required | remote database name |
| `use_remote_estimate` | boolean | `false` | run remote `EXPLAIN` for cost estimates |
| `fetch_size` | integer | 100 | rows fetched per round trip on `SELECT` |
| `batch_size` | integer | 1 | rows sent per round trip on `INSERT` |

`fetch_size` and `batch_size` can be set on the `SERVER` or per `FOREIGN TABLE`;
the table-level value wins.

### Pushdown behavior

- `WHERE` clauses are sent to the remote only when they use built-in (or declared
  extension) data types, operators, and functions, and those are `IMMUTABLE`.
- A join between two foreign tables on the SAME server is sent as one remote join.
- Aggregates, `ORDER BY`, and `LIMIT` push down under the same conditions.
- The exact remote SQL is visible with `EXPLAIN VERBOSE`.
- `use_remote_estimate 'true'` makes the planner request remote row-count and cost
  estimates, then adds `fdw_startup_cost` and `fdw_tuple_cost`. Without it the
  planner uses local guesses and can refuse a beneficial pushdown.

## Load-tuning GUCs

| GUC | Recommended for a load | Reason |
|-----|------------------------|--------|
| `maintenance_work_mem` | `1GB`+ (session) | faster post-load `CREATE INDEX` |
| `synchronous_commit` | `off` (session) | skips per-commit fsync wait |
| `max_wal_size` | raise temporarily | fewer checkpoints during the load |
| autovacuum on target | `ALTER TABLE t SET (autovacuum_enabled = false)` | no autovacuum churn mid-load |

Set session GUCs with `SET LOCAL` inside the load transaction so they revert
automatically. `UNLOGGED` tables (`CREATE UNLOGGED TABLE`) skip WAL entirely but
are emptied on a crash and never replicated : use them for reproducible ETL
staging only.

## Driver-side COPY (psycopg 3)

| Call | Use |
|------|-----|
| `cursor.copy("COPY t FROM STDIN")` | context manager; `copy.write_row(...)` per row |
| `copy.write(buffer)` | write raw `text`/`csv`/`binary` bytes into the stream |
| `cursor.executemany(sql, seq)` | batched parameterized `INSERT` when COPY does not fit |

For repeated parameterized inserts where `COPY` is not usable, a prepared
statement plus `executemany` (psycopg 3) or `execute_batch` (psycopg 2) avoids
re-planning each statement. `COPY FROM STDIN` still wins for pure bulk load.
