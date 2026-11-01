# Anti-Patterns : Query Performance Toolkit

Each entry : cause, symptom, fix-pattern, and SQLSTATE where one applies. Most
performance mistakes raise no error; they degrade silently, so the symptom column is
how you recognise them.

## AP-1 : Bare EXPLAIN ANALYZE on a write in production

CAUSE : `EXPLAIN ANALYZE` is documented to EXECUTE the statement to obtain real
timings. For an `INSERT` / `UPDATE` / `DELETE` / `MERGE` that means the rows actually
change, triggers fire, and WAL is written.

SYMPTOM : data changed unexpectedly after "just checking the plan". Triggers ran. WAL
and replication traffic spiked. No error is raised; the damage is silent.

FIX : ALWAYS wrap a write in an explicit transaction and roll back.

```sql
BEGIN;
EXPLAIN (ANALYZE, BUFFERS) UPDATE orders SET status = 'x' WHERE id < 100;
ROLLBACK;
```

The plan and `actual time` are fully measured inside the rolled-back transaction.

SQLSTATE : none. This is a logic mistake, not an error condition.

## AP-2 : Reading plain EXPLAIN cost as milliseconds

CAUSE : the `cost=startup..total` numbers are in arbitrary planner units derived from
`seq_page_cost`, `cpu_tuple_cost`, and related GUCs. They exist only to rank plans.

SYMPTOM : reports like "the query takes 445ms" sourced from a plain `EXPLAIN` showing
`cost=0.00..445.00`. The real runtime is unrelated to that number.

FIX : for real time, run `EXPLAIN ANALYZE` and read `actual time` / `Execution Time`.
Use plain `EXPLAIN` only to inspect plan shape and compare relative cost between plans.

SQLSTATE : none.

## AP-3 : Treating every Seq Scan as a bug

CAUSE : the belief that an index scan is always faster than a sequential scan.

SYMPTOM : indexes added to small or low-selectivity tables; the planner ignores them
and keeps choosing Seq Scan, and the new indexes only add write overhead and bloat.

FIX : a Seq Scan is OPTIMAL when the table is small (the whole table is a few pages) or
when the query returns a large fraction of rows (random index lookups would cost more
than one linear pass). Only treat a Seq Scan as a problem on a large table with a
selective `WHERE` and a high `Rows Removed by Filter`. Confirm with `EXPLAIN ANALYZE`
before adding an index.

SQLSTATE : none.

## AP-4 : Ignoring an estimate-vs-actual row mismatch

CAUSE : focusing on the chosen node type (Seq Scan, Nested Loop) while the planner's
`rows=` estimate is wildly wrong because table statistics are stale.

SYMPTOM : `EXPLAIN ANALYZE` shows `rows=12` against `actual ... rows=48000`. The plan
looks bad, but rewriting SQL or adding indexes does not help, because the planner keeps
choosing from wrong numbers.

FIX : treat the estimate mismatch as the root cause. Run `ANALYZE table` to refresh
per-column statistics. If the mismatch survives and the filtered columns are correlated
(for example `city` and `zip`), add `CREATE STATISTICS ... (dependencies, mcv)` and
`ANALYZE` again. Only then re-read the plan.

SQLSTATE : none.

## AP-5 : Leaving enable_seqscan = off after diagnosis

CAUSE : `SET enable_seqscan = off` (and `enable_nestloop`, `enable_hashjoin`, ...) used
to force an alternative plan for comparison, then never reset.

SYMPTOM : queries that genuinely need a Seq Scan get a far more expensive forced index
or bitmap plan. The slowdown spreads across every query in the session or, if written
to postgresql.conf, the whole server.

FIX : these GUCs are diagnostic switches, not configuration. `RESET enable_seqscan;`
immediately after the comparison, or end the session. NEVER persist them to
postgresql.conf. If the forced plan really is faster, the planner mis-estimated; fix
the statistics (AP-4) instead of disabling the node type.

SQLSTATE : none.

## AP-6 : Killing the blocked victim instead of the blocker

CAUSE : seeing a query "stuck" and cancelling that query, without checking which
session actually holds the conflicting lock.

SYMPTOM : the cancelled query's client retries, hits the same lock, and stalls again.
The real blocker keeps holding its lock; the wait queue never drains.

FIX : identify the blocker with `pg_blocking_pids(blocked_pid)`, inspect it in
`pg_stat_activity`, and resolve there. If the blocker is `idle in transaction`, fix the
application that leaked the transaction. If it is a long active statement, wait for it
or cancel the BLOCKER, not the victim.

SQLSTATE : none for the wait itself; a lock wait that exceeds `lock_timeout` raises
55P03 `lock_not_available`.

## AP-7 : Using pg_terminate_backend where pg_cancel_backend suffices

CAUSE : reaching for `pg_terminate_backend` to stop one runaway query.

SYMPTOM : the entire backend connection is destroyed. Any open transaction on that
connection is aborted, the client's connection pool entry is invalidated, and the
client sees a connection error rather than a clean query cancellation.

FIX : use `pg_cancel_backend(pid)` first. It cancels only the current statement and
leaves the session and connection intact. Reserve `pg_terminate_backend(pid)` for a
session that ignores cancellation, such as one stuck `idle in transaction`.

SQLSTATE : a cancelled query raises 57014 `query_canceled`.

## AP-8 : Reading cost from EXPLAIN ANALYZE output

CAUSE : habit from plain `EXPLAIN`; reading the `cost=` field even though `actual time`
is present in the same line.

SYMPTOM : conclusions drawn from estimated cost while real measured time sits right
next to it. The cost and actual time can disagree sharply, and the cost is the wrong
one to trust here.

FIX : once `ANALYZE` is on, read `actual time`, `actual rows`, and `loops`. Use `cost`
only to see what the planner BELIEVED before execution, for example to explain WHY it
chose this plan.

SQLSTATE : none.

## AP-9 : pg_stat_statements without compute_query_id

CAUSE : adding `pg_stat_statements` to `shared_preload_libraries` and running
`CREATE EXTENSION` but leaving `compute_query_id` at its default.

SYMPTOM : the `pg_stat_statements` view stays empty or barely populated; queries are
not aggregating as expected.

FIX : set `compute_query_id = on` (or `auto`) in postgresql.conf and reload. The query
identifier is what lets the extension normalize and group statements. All three steps
are mandatory: `shared_preload_libraries`, `compute_query_id`, `CREATE EXTENSION`.

SQLSTATE : none.

## AP-10 : Expecting plain ANALYZE to learn cross-column correlation

CAUSE : assuming `ANALYZE` alone teaches the planner that two columns are related.

SYMPTOM : a query filtering on `WHERE a = ... AND b = ...` keeps getting a row estimate
far below actual, even right after `ANALYZE`, because the planner multiplies the two
column selectivities as if they were independent.

FIX : `ANALYZE` builds only per-column histograms; it cannot represent correlation.
Create extended statistics with `CREATE STATISTICS name (dependencies, ndistinct, mcv)
ON a, b FROM t;` then run `ANALYZE t` again so the extended statistics are populated.

SQLSTATE : none.
