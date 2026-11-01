# anti-patterns : zero-downtime migrations

Each entry : cause, symptom, fix-pattern, SQLSTATE where applicable.
Verified against PostgreSQL 17 documentation. Behaviour is identical on 15,
16, and 17 unless noted.

## AP-1 : Plain CREATE INDEX on a busy table

CAUSE : `CREATE INDEX` without `CONCURRENTLY` locks the table against writes
and builds the index in a single scan.

SYMPTOM : every INSERT, UPDATE and DELETE on the table blocks for the whole
build; on a large table this is minutes of write outage. Reads still work,
which can hide the problem in light testing.

FIX-PATTERN : use `CREATE INDEX CONCURRENTLY`. It takes only `SHARE UPDATE
EXCLUSIVE`, so writes continue; the cost is two table scans and a longer
wall time. Verify `pg_index.indisvalid` afterward.

SQLSTATE : none (blocking is wait, not error).

## AP-2 : ALTER COLUMN TYPE in place on a large table

CAUSE : `ALTER TABLE ... ALTER COLUMN ... TYPE` rewrites the entire table
and all its indexes under `ACCESS EXCLUSIVE`.

SYMPTOM : the table is fully unavailable (no reads, no writes) for the whole
rewrite; on a multi-GB table this is a long, unbounded outage.

FIX-PATTERN : use expand-contract : add a new nullable column, dual-write
with a trigger or application code, backfill in chunks, add `NOT NULL` via
`NOT VALID` + `VALIDATE`, switch reads, then drop the old column. The narrow
no-rewrite exception (binary-coercible type, no `USING` change) is the only
case where an in-place change is safe.

SQLSTATE : none.

## AP-3 : ADD COLUMN with a volatile DEFAULT

CAUSE : `ADD COLUMN ... DEFAULT now()` (or `random()`, or any volatile
expression). A volatile default must be computed per row, which forces a
full table rewrite.

SYMPTOM : an `ADD COLUMN` that was expected to be instant instead rewrites
every page under `ACCESS EXCLUSIVE`, blocking all access.

FIX-PATTERN : add the column with no default (or a non-volatile constant
default, which is stored in metadata with no rewrite), then backfill the
per-row value in committed chunks.

SQLSTATE : none.

## AP-4 : Migration DDL without lock_timeout

CAUSE : running `ALTER TABLE` with the default `lock_timeout = 0` (wait
forever). The `ALTER` requests `ACCESS EXCLUSIVE`; if a long-running query
holds the table, the `ALTER` queues, and because lock requests are ordered,
every query arriving after the `ALTER` also queues behind it.

SYMPTOM : one slow `SELECT` plus one fast, metadata-only `ALTER` freezes the
entire table. Connections pile up; the application appears down even though
the `ALTER` itself would take milliseconds.

FIX-PATTERN : `SET lock_timeout = '3s';` before every migration statement,
so a blocked `ALTER` gives up instead of stalling traffic. The migration
runner catches the failure and retries after a short pause. Add
`statement_timeout` to bound the operation once it starts.

SQLSTATE : `55P03 lock_not_available` when `lock_timeout` is exceeded.

## AP-5 : One giant UPDATE to backfill a column

CAUSE : backfilling a new column with a single `UPDATE` over the whole
table.

SYMPTOM : every touched row is locked for the entire run; dead tuples
accumulate until the one transaction commits, bloating the table; a replica
must apply the whole change at once, spiking replication lag; the long
transaction also holds back VACUUM cleanup elsewhere.

FIX-PATTERN : backfill in committed batches of 1000-10000 rows
(`WHERE id BETWEEN :lo AND :hi`). Short batches release locks quickly, let
autovacuum reclaim space between them, and keep replication lag bounded.

SQLSTATE : none (resource exhaustion / lag, not an error). A per-batch
`statement_timeout` can surface `57014 query_canceled` if a batch is too big.

## AP-6 : CREATE INDEX CONCURRENTLY inside a transaction block

CAUSE : a migration tool wraps every statement in `BEGIN` / `COMMIT`, but
`CREATE INDEX CONCURRENTLY`, `REINDEX ... CONCURRENTLY`, and `DETACH
PARTITION CONCURRENTLY` cannot run inside a transaction block.

SYMPTOM : the statement fails immediately with an error stating that
`CREATE INDEX CONCURRENTLY` cannot run inside a transaction block.

FIX-PATTERN : run concurrent statements outside any transaction. Most
migration tools have a flag to disable the automatic transaction wrapper for
a specific migration (for example a "non-transactional" or "no-transaction"
migration mode); use it for every `CONCURRENTLY` statement.

SQLSTATE : `25001 active_sql_transaction`.

## AP-7 : Ignoring an INVALID index after a failed concurrent build

CAUSE : a `CREATE INDEX CONCURRENTLY` failed (deadlock, unique violation)
and left an `INVALID` index, which was never cleaned up.

SYMPTOM : the index is ignored by the planner, so the query it was meant to
speed up still does a sequential scan, yet every INSERT/UPDATE/DELETE still
pays the cost of maintaining the dead index.

FIX-PATTERN : detect with `SELECT indexrelid::regclass FROM pg_index WHERE
NOT indisvalid;`. Then `DROP INDEX CONCURRENTLY` and retry the build, or
`REINDEX INDEX CONCURRENTLY` (v12+) to rebuild it in place.

SQLSTATE : the original failure was typically `40P01 deadlock_detected` or
`23505 unique_violation`; the lingering invalid index itself raises nothing.

## AP-8 : SET NOT NULL directly on a large table

CAUSE : `ALTER TABLE ... ALTER COLUMN ... SET NOT NULL` run as a single
statement. It scans every row to confirm there are no NULLs, holding
`ACCESS EXCLUSIVE` for the whole scan.

SYMPTOM : the table is locked against all access for the duration of a full
scan, proportional to table size.

FIX-PATTERN : add a `CHECK (col IS NOT NULL) NOT VALID` constraint (no
scan), `VALIDATE` it under `SHARE UPDATE EXCLUSIVE` (writes continue), then
run `SET NOT NULL` : the valid `CHECK` proves non-null, so `SET NOT NULL`
skips its scan.

SQLSTATE : none for the blocking; the validate step fails with
`23514 check_violation` if the column actually contains NULLs.

## AP-9 : Bare RENAME COLUMN on a live system

CAUSE : renaming a column in place and expecting it to be zero-downtime
because the rename itself is fast metadata.

SYMPTOM : the rename is instant, but the running application code still
references the old name. The schema and the deployed code disagree for the
window between the migration and the code deploy, causing errors on one side
or the other.

FIX-PATTERN : use expand-contract : add the new column, dual-write to both,
backfill, switch application reads, then drop the old column. This decouples
the schema change from the code deploy so neither side ever sees a missing
column.

SQLSTATE : `42703 undefined_column` is what the mismatched side raises.

## Sources

- https://www.postgresql.org/docs/17/sql-altertable.html
- https://www.postgresql.org/docs/17/sql-createindex.html
- https://www.postgresql.org/docs/17/sql-reindex.html
- https://www.postgresql.org/docs/17/errcodes-appendix.html
