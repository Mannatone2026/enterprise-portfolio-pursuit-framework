---
name: postgres-impl-query-performance-toolkit
description: >
  Use when a query is slow, reading an EXPLAIN plan, finding the worst queries on a server, or diagnosing lock waits.
  Prevents trusting EXPLAIN cost as real time, running EXPLAIN ANALYZE on a destructive query in production (it executes), and chasing the plan when stale statistics are the real cause.
  Covers EXPLAIN / EXPLAIN ANALYZE / BUFFERS, reading plan nodes (Seq/Index/Bitmap/Nested-Loop/Hash/Merge/Sort/Aggregate/Memoize v14+), cost model, estimate-vs-actual rows, pg_stat_statements forensics, pg_stat_activity, pg_locks + pg_blocking_pids, extended statistics (CREATE STATISTICS).
  Keywords: EXPLAIN, EXPLAIN ANALYZE, BUFFERS, query plan, Seq Scan, Index Scan, Bitmap, Nested Loop, Hash Join, Memoize, pg_stat_statements, pg_stat_activity, pg_locks, pg_blocking_pids, CREATE STATISTICS, query is slow, how to read explain, find slow queries, why is this query slow, lock wait
license: MIT
compatibility: "Designed for Claude Code. Requires PostgreSQL 15, 16, or 17."
metadata:
  author: OpenAEC-Foundation
  version: "1.0"
---

# postgres-impl-query-performance-toolkit

## Quick Reference :

This skill is the diagnostic toolkit for "why is this slow". It merges three workflows:
read a plan (EXPLAIN), find the worst queries server-wide (pg_stat_statements), and
unstick a blocked session (pg_stat_activity + pg_locks).

The single most common mistake: trusting `EXPLAIN` cost numbers as real time. Cost is
in arbitrary planner units, not milliseconds. Only `EXPLAIN ANALYZE` reports real time,
and it does so by EXECUTING the query. The second most common mistake: tuning the plan
when the planner's row estimate is wrong because statistics are stale. Fix the estimate
first (`ANALYZE`), then re-read the plan.

```
slow query reported
  ├─ have the exact query?      → EXPLAIN (ANALYZE, BUFFERS) it     (Pattern 1, 3)
  │     └─ data-modifying?      → wrap in BEGIN / ROLLBACK          (Pattern 2)
  ├─ "the server" is slow?      → pg_stat_statements top-N          (Pattern 5)
  ├─ a query hangs / never ends → pg_stat_activity long-runners     (Pattern 6)
  └─ a query waits on a lock    → pg_locks + pg_blocking_pids       (Pattern 7)
```

Estimate-vs-actual rule: in `EXPLAIN ANALYZE`, compare `rows=` (estimate) against
`actual ... rows=`. A mismatch above ~10x means the planner is flying blind. Run
`ANALYZE table`; if columns are correlated, add `CREATE STATISTICS` (Pattern 4, 8).

## When To Use This Skill :

ALWAYS use this skill when :
- A query is slow and you need to read or interpret an EXPLAIN plan
- You must find the most expensive queries across an entire server
- A session is blocked, hanging, or "stuck" and you need to find the blocker
- A query's runtime does not match its plan's cost estimate
- The planner picks a Seq Scan, bad join order, or wrong join type and you need to know why

NEVER use this skill for :
- Choosing or creating indexes as a design task (use postgres-impl-indexing-strategy)
- Bloat, dead tuples, or autovacuum tuning (use postgres-impl-vacuum-bloat-toolkit)
- Deadlock resolution and serialization-failure retry logic (use postgres-errors-deadlocks)
- Connection pool sizing and `work_mem` capacity planning (use postgres-core-configuration)

## Decision Trees :

### Which EXPLAIN variant to run :

```
need the plan SHAPE only, query is destructive or very slow
  → EXPLAIN query                 (no execution, cost estimates only)

need REAL timing and row counts
  → EXPLAIN (ANALYZE, BUFFERS) query
      │
      ├─ query is SELECT (read-only)        → run directly
      └─ query is INSERT/UPDATE/DELETE/MERGE → BEGIN; EXPLAIN ANALYZE ...; ROLLBACK;
                                               (ANALYZE EXECUTES the statement)

need machine-readable output for tooling
  → EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) query

need to know which GUC settings shaped the plan
  → EXPLAIN (ANALYZE, BUFFERS, SETTINGS) query
```

### Is this plan node a problem :

```
Seq Scan
  ├─ table is small (< ~1000 rows)          → NOT a problem, optimal
  ├─ query returns most of the table        → NOT a problem, optimal
  └─ large table, selective WHERE, no index → PROBLEM → missing/unused index

estimate vs actual rows
  ├─ within ~3x                             → estimate is fine
  └─ off by 10x or more                     → PROBLEM → stale stats → ANALYZE
                                              correlated cols → CREATE STATISTICS

Sort / Aggregate node
  ├─ "Sort Method: quicksort  Memory: ..."  → in memory, fine
  └─ "Sort Method: external merge  Disk:..." → spilled to disk → raise work_mem

Nested Loop with large outer side
  └─ inner runs once per outer row, loops=N high → PROBLEM → expect Hash/Merge Join

Rows Removed by Filter very high
  └─ node reads many rows, discards most    → PROBLEM → index the filter column
```

### Server feels slow, no single query named :

```
1. SELECT ... FROM pg_stat_statements ORDER BY total_exec_time DESC LIMIT 10;
       → high total_exec_time + high calls  → frequent query, optimise or cache
       → high mean_exec_time + low calls    → one heavy query, EXPLAIN ANALYZE it
       → high shared_blks_read              → I/O bound, check indexes / work_mem
2. SELECT ... FROM pg_stat_activity WHERE state = 'active';
       → long query_start age               → long-running query, EXPLAIN ANALYZE it
       → state = 'idle in transaction'      → leaked transaction, holds locks/bloat
3. wait_event_type = 'Lock'                  → blocked → lock-wait tree below
```

### A query is blocked on a lock :

```
1. SELECT pid, state, wait_event_type, query
   FROM pg_stat_activity WHERE wait_event_type = 'Lock';
2. SELECT pg_blocking_pids(<blocked_pid>);   → array of blocker PIDs
3. inspect blocker:
   SELECT pid, state, query, xact_start
   FROM pg_stat_activity WHERE pid = ANY(pg_blocking_pids(<blocked_pid>));
4. resolve at the blocker, never the victim:
       blocker is 'idle in transaction'      → application leaked a transaction
       blocker is 'active' long DDL/UPDATE   → wait, or pg_cancel_backend(blocker_pid)
```

## Patterns :

### Pattern 1 : Plain EXPLAIN for shape, EXPLAIN ANALYZE for truth

ALWAYS treat `EXPLAIN` cost as a relative planner estimate, never as time.
ALWAYS use `EXPLAIN ANALYZE` when you need real milliseconds and real row counts.
NEVER report "this query takes 445ms" from a plain `EXPLAIN` cost of `445.00`.

```sql
-- Plan shape only, no execution. Cost is in arbitrary units.
EXPLAIN SELECT * FROM orders WHERE customer_id = 42;
-- Seq Scan on orders  (cost=0.00..445.00 rows=10000 width=244)

-- Real execution. actual time IS milliseconds.
EXPLAIN ANALYZE SELECT * FROM orders WHERE customer_id = 42;
-- Index Scan ... (cost=0.29..8.30 rows=1 width=244)
--                (actual time=0.018..0.020 rows=1 loops=1)
```

WHY : the `cost=startup..total` pair is computed from `seq_page_cost`, `cpu_tuple_cost`,
and friends. It exists only to let the planner rank plans against each other. Two plans
with cost 100 and 200 say "the planner expects the second to be twice as expensive",
nothing about wall-clock time. `actual time=min..max` is the real per-loop timing.

### Pattern 2 : EXPLAIN ANALYZE on writes must be wrapped in a transaction

ALWAYS wrap `EXPLAIN ANALYZE` of an `INSERT`, `UPDATE`, `DELETE`, or `MERGE` in
`BEGIN; ... ROLLBACK;` unless you intend the rows to actually change.
NEVER run a bare `EXPLAIN ANALYZE UPDATE ...` in production: ANALYZE executes the
statement, so the rows are modified, triggers fire, and WAL is written.

```sql
BEGIN;
EXPLAIN (ANALYZE, BUFFERS)
  UPDATE orders SET status = 'shipped' WHERE status = 'pending';
ROLLBACK;   -- plan measured, table unchanged
```

WHY : `EXPLAIN ANALYZE` is documented to run the query to obtain real timings. For a
write, "running it" means committing the change unless the surrounding transaction is
rolled back. The plan and `actual time` are still fully measured inside the rolled-back
transaction. See references/anti-patterns.md AP-1.

### Pattern 3 : BUFFERS to locate the I/O-heavy node

ALWAYS add `BUFFERS` to `EXPLAIN ANALYZE` when diagnosing an I/O-bound query.
NEVER assume a node is slow from `actual time` alone; check whether time came from
disk reads or CPU.

```sql
EXPLAIN (ANALYZE, BUFFERS)
  SELECT * FROM orders WHERE order_date > '2026-01-01';
-- Bitmap Heap Scan on orders ...
--   Buffers: shared hit=120 read=8400
--   ->  Bitmap Index Scan ...
--         Buffers: shared hit=4 read=30
```

WHY : `shared hit` = block found in PostgreSQL's buffer cache (fast). `shared read` =
block fetched from the OS / disk (slow, and may itself be an OS-cache hit). A node with
a high `read` count is the I/O bottleneck. `dirtied` and `written` mean the node forced
buffer eviction. Reading the largest `read` value points straight at the node to fix.

### Pattern 4 : Estimate-vs-actual mismatch means stale statistics, not a bad plan

ALWAYS compare the planner `rows=` estimate against `actual ... rows=` before touching
indexes or query text.
NEVER add indexes or rewrite SQL while the row estimate is off by 10x or more: the
planner chose its plan from wrong numbers, so fix the numbers first.

```sql
EXPLAIN ANALYZE SELECT * FROM orders WHERE region = 'EU' AND channel = 'web';
-- ... rows=12 ... (actual ... rows=48000 loops=1)   <-- estimate 4000x too low

ANALYZE orders;            -- refresh per-column statistics
-- re-run EXPLAIN ANALYZE; if estimate still wrong AND region/channel correlate:
CREATE STATISTICS orders_region_channel (dependencies, mcv)
  ON region, channel FROM orders;
ANALYZE orders;            -- extended statistics only take effect after ANALYZE
```

WHY : the planner estimates row counts from per-column histograms gathered by `ANALYZE`.
After heavy `INSERT`/`UPDATE`/`DELETE`, those histograms drift and estimates rot. A
mismatch is the root cause; the wrong join type or Seq Scan is only the symptom.

### Pattern 5 : pg_stat_statements for server-wide query forensics

ALWAYS use `pg_stat_statements` to find the worst queries when no single query is named.
NEVER guess which query is "the slow one"; rank them by `total_exec_time`.

```sql
-- one-time setup: postgresql.conf needs
--   shared_preload_libraries = 'pg_stat_statements'   (restart required)
--   compute_query_id = on
CREATE EXTENSION IF NOT EXISTS pg_stat_statements;

SELECT query, calls, total_exec_time, mean_exec_time,
       100.0 * shared_blks_hit
         / NULLIF(shared_blks_hit + shared_blks_read, 0) AS hit_pct
FROM pg_stat_statements
ORDER BY total_exec_time DESC
LIMIT 10;
```

WHY : `pg_stat_statements` normalizes queries (constants become `$1`, `$2`, ...), so
10000 calls of the same shape aggregate into one row. `total_exec_time` finds the query
that costs the server most overall; `mean_exec_time` finds the single slowest call.
Reset the baseline with `SELECT pg_stat_statements_reset();` before a measured test.

### Pattern 6 : pg_stat_activity to catch long-running and idle-in-transaction sessions

ALWAYS inspect `pg_stat_activity` to find a query that hangs or never finishes.
NEVER kill a session before reading its `state` and `query` columns.

```sql
SELECT pid, state, wait_event_type, wait_event,
       now() - query_start AS run_time, query
FROM pg_stat_activity
WHERE state <> 'idle'
ORDER BY run_time DESC;
```

WHY : `state = 'active'` with a large `run_time` is a genuine long-running query, to be
examined with `EXPLAIN ANALYZE`. `state = 'idle in transaction'` is an application bug:
a transaction was opened and left open, pinning locks and blocking vacuum. Cancel the
current statement with `pg_cancel_backend(pid)`; terminate the whole session only with
`pg_terminate_backend(pid)`.

### Pattern 7 : pg_locks + pg_blocking_pids to find the blocker

ALWAYS resolve a lock wait at the blocking session, never at the blocked victim.
ALWAYS use `pg_blocking_pids(pid)` rather than hand-joining `pg_locks` to itself.

```sql
-- who is waiting, and who blocks them
SELECT waiting.pid          AS waiting_pid,
       waiting.query        AS waiting_query,
       blocking.pid         AS blocking_pid,
       blocking.state       AS blocking_state,
       blocking.query       AS blocking_query
FROM pg_stat_activity AS waiting
JOIN pg_stat_activity AS blocking
  ON blocking.pid = ANY(pg_blocking_pids(waiting.pid))
WHERE waiting.wait_event_type = 'Lock';
```

WHY : `pg_blocking_pids(pid)` returns an `integer[]` of the sessions blocking that pid,
covering both hard blocks (conflicting lock held) and soft blocks (ahead in the wait
queue). A prepared transaction blocker shows as PID `0`. The raw `pg_locks` view, joined
to itself, is error-prone; the function is the supported way. See references/methods.md.

### Pattern 8 : Extended statistics for correlated columns

ALWAYS create extended statistics when two filtered columns are functionally dependent
or correlated and the planner under-estimates the combined selectivity.
NEVER expect plain `ANALYZE` to learn cross-column correlation: per-column histograms
cannot represent it.

```sql
-- planner assumes city and zip are independent, multiplies selectivities,
-- and badly under-estimates the AND'd predicate
CREATE STATISTICS addr_city_zip (dependencies, ndistinct, mcv)
  ON city, zip FROM addresses;
ANALYZE addresses;
```

WHY : with `WHERE city = 'Berlin' AND zip = '10115'` the planner multiplies the two
selectivities, but zip implies city, so the true selectivity is just the zip
selectivity. `dependencies` teaches the functional dependency, `ndistinct` corrects
group-count estimates, `mcv` captures common value combinations. All three only apply
after the next `ANALYZE`.

## Anti-Patterns :

(Cause, symptom, and fix for each in references/anti-patterns.md)

- AP-1 : Bare `EXPLAIN ANALYZE` on an `UPDATE`/`DELETE` in production. It executes.
- AP-2 : Reporting plain `EXPLAIN` cost as if it were milliseconds.
- AP-3 : Treating every Seq Scan as a bug. On small or low-selectivity tables it wins.
- AP-4 : Ignoring an estimate-vs-actual row mismatch and tuning the plan instead.
- AP-5 : Leaving `SET enable_seqscan = off` in place after diagnosis. SQLSTATE not raised, but plans degrade silently.
- AP-6 : Killing the blocked (victim) session instead of the blocking session.
- AP-7 : Using `pg_terminate_backend` where `pg_cancel_backend` (SQLSTATE 57014, query_canceled) is enough.
- AP-8 : Reading `cost` from `EXPLAIN ANALYZE` while `actual time` is right there.

## Reference Links :

- [references/methods.md](references/methods.md) : EXPLAIN options, plan node catalog, pg_stat_statements / pg_stat_activity / pg_locks columns, CREATE STATISTICS kinds, function signatures
- [references/examples.md](references/examples.md) : Working diagnostic queries, version-annotated, with verbatim plan output
- [references/anti-patterns.md](references/anti-patterns.md) : Anti-patterns with cause, symptom, fix-pattern, SQLSTATE where applicable

## See Also :

- postgres-impl-indexing-strategy : once a plan shows a missing index, design the index there
- postgres-impl-vacuum-bloat-toolkit : when `idle in transaction` or bloat causes slow scans
- postgres-errors-deadlocks : when a lock wait escalates to a deadlock (SQLSTATE 40P01)
- postgres-core-configuration : tuning `work_mem`, `shared_buffers`, `effective_cache_size`
- Vooronderzoek section : §6 Performance + EXPLAIN
- Official docs : https://www.postgresql.org/docs/17/using-explain.html
