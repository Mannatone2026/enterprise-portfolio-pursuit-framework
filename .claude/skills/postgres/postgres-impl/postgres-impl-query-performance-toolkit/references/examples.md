# Examples : Query Performance Toolkit

Working diagnostic queries with verbatim plan output. All examples target PostgreSQL
15, 16, and 17 unless a `v16+` / `v17+` annotation says otherwise.

## 1. Reading a plain EXPLAIN :

```sql
EXPLAIN SELECT * FROM tenk1 WHERE unique1 = 42;
```

```
Index Scan using tenk1_unique1 on tenk1  (cost=0.29..8.30 rows=1 width=244)
  Index Cond: (unique1 = 42)
```

- `cost=0.29..8.30` : arbitrary units, NOT milliseconds.
- `rows=1` : estimate.
- `Index Cond` : the predicate pushed into the index.

## 2. EXPLAIN ANALYZE : estimate vs actual :

```sql
EXPLAIN (ANALYZE) SELECT * FROM tenk1 WHERE unique1 < 100;
```

```
Bitmap Heap Scan on tenk1  (cost=5.06..224.98 rows=100 width=244)
                           (actual time=0.094..0.190 rows=100 loops=1)
  Recheck Cond: (unique1 < 100)
  Heap Blocks: exact=90
  ->  Bitmap Index Scan on tenk1_unique1  (cost=0.00..5.04 rows=100 width=0)
                                          (actual time=0.071..0.071 rows=100 loops=1)
        Index Cond: (unique1 < 100)
Planning Time: 0.110 ms
Execution Time: 0.250 ms
```

Estimate `rows=100` matches actual `rows=100` exactly: statistics are healthy. The two
`actual time` values are the real timings; `Execution Time` is the total.

## 3. EXPLAIN with BUFFERS : finding the I/O-heavy node :

```sql
EXPLAIN (ANALYZE, BUFFERS)
  SELECT count(*) FROM tenk1 WHERE tenthous < 5000;
```

```
Aggregate  (cost=483.00..483.01 rows=1 width=8) (actual time=4.52..4.52 rows=1 loops=1)
  Buffers: shared hit=345
  ->  Seq Scan on tenk1  (cost=0.00..470.00 rows=5000 width=0)
                         (actual time=0.01..3.9 rows=5000 loops=1)
        Filter: (tenthous < 5000)
        Rows Removed by Filter: 5000
        Buffers: shared hit=345
```

`shared hit=345` with no `read` : fully cached, the time is CPU not I/O. `Rows Removed
by Filter: 5000` shows the Seq Scan read 10000 rows to emit 5000; a partial index on
`tenthous` would cut that.

## 4. EXPLAIN ANALYZE on a write, safely :

```sql
BEGIN;
EXPLAIN (ANALYZE, BUFFERS, WAL)
  UPDATE tenk1 SET hundred = hundred + 1 WHERE unique1 < 100;
ROLLBACK;
```

```
Update on tenk1  (cost=5.06..225.23 rows=0 width=0)
                 (actual time=2.05..2.05 rows=0 loops=1)
  Buffers: shared hit=350 dirtied=12
  WAL: records=200 bytes=21560
  ->  Bitmap Heap Scan on tenk1  (cost=5.06..225.23 rows=100 width=10) ...
Planning Time: 0.12 ms
Execution Time: 2.11 ms
```

The `UPDATE` ran and produced real `WAL` and `dirtied` numbers; `ROLLBACK` then undid
the row changes. Without the transaction wrapper the 100 rows would stay modified.

## 5. EXPLAIN FORMAT JSON for tooling :

```sql
EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)
  SELECT * FROM tenk1 WHERE unique1 = 42;
```

Returns a single JSON document with a `Plan` tree. Each node carries `Node Type`,
`Total Cost`, `Plan Rows`, `Actual Rows`, `Actual Total Time`, `Shared Hit Blocks`.
`YAML` and `XML` are also accepted by `FORMAT`.

## 6. Generic plan for a parameterized statement (v16+) :

```sql
-- v16+ : plan a statement with placeholders without binding values or executing
EXPLAIN (GENERIC_PLAN)
  SELECT * FROM orders WHERE customer_id = $1 AND status = $2;
```

Use this to inspect the plan a prepared statement would get, on v16 and v17.

## 7. pg_stat_statements : top queries by total cost :

```sql
SELECT query,
       calls,
       round(total_exec_time::numeric, 1)  AS total_ms,
       round(mean_exec_time::numeric, 2)   AS mean_ms,
       rows,
       100.0 * shared_blks_hit
         / NULLIF(shared_blks_hit + shared_blks_read, 0) AS hit_pct
FROM pg_stat_statements
ORDER BY total_exec_time DESC
LIMIT 10;
```

```
query    | UPDATE pgbench_branches SET bbalance = bbalance + $1 WHERE bid = $2
calls    | 3000
total_ms | 25565.9
mean_ms  | 8.52
rows     | 3000
hit_pct  | 100.0
```

The query text is normalized: literal amounts became `$1` and `$2`, so every call of
this shape aggregates into one row.

## 8. pg_stat_statements : worst single call vs worst overall :

```sql
-- worst single call: one heavy query
SELECT query, calls, round(mean_exec_time::numeric, 1) AS mean_ms
FROM pg_stat_statements
ORDER BY mean_exec_time DESC
LIMIT 5;

-- reset the baseline before a measured test run
SELECT pg_stat_statements_reset();
```

## 9. pg_stat_activity : long-running and idle-in-transaction sessions :

```sql
SELECT pid,
       state,
       wait_event_type,
       wait_event,
       now() - query_start AS run_time,
       now() - xact_start  AS xact_age,
       left(query, 80)     AS query
FROM pg_stat_activity
WHERE state <> 'idle'
  AND backend_type = 'client backend'
ORDER BY run_time DESC NULLS LAST;
```

```
pid   | state                | wait_event_type | run_time        | query
------+----------------------+-----------------+-----------------+-----------------
8821  | active               | (null)          | 00:04:12.5      | SELECT ... big report
9043  | idle in transaction  | Client          | 00:00:00        | UPDATE orders ...
```

`8821` is a genuine long query: run `EXPLAIN ANALYZE` on it. `9043` is
`idle in transaction`: the application opened a transaction and stopped; it pins locks
and blocks vacuum until committed or terminated.

## 10. Find the blocker of a lock wait :

```sql
SELECT waiting.pid                       AS waiting_pid,
       left(waiting.query, 60)           AS waiting_query,
       blocking.pid                      AS blocking_pid,
       blocking.state                    AS blocking_state,
       left(blocking.query, 60)          AS blocking_query
FROM pg_stat_activity AS waiting
JOIN pg_stat_activity AS blocking
  ON blocking.pid = ANY(pg_blocking_pids(waiting.pid))
WHERE waiting.wait_event_type = 'Lock';
```

```
waiting_pid | blocking_pid | blocking_state       | blocking_query
------------+--------------+----------------------+-------------------------
9210        | 9043         | idle in transaction  | UPDATE orders SET ...
```

The blocker `9043` is `idle in transaction`: the fix is in the application that leaked
the transaction, not in the waiting query `9210`.

## 11. Cancel or terminate the blocker :

```sql
-- cancel the blocker's current statement, session stays alive
SELECT pg_cancel_backend(9043);

-- only if cancel is not enough: terminate the whole backend
SELECT pg_terminate_backend(9043);
```

NEVER act on the waiting pid; resolving the wait at the victim does nothing.

## 12. Raw pg_locks inspection :

```sql
SELECT locktype, relation::regclass AS rel, mode, granted, pid, waitstart
FROM pg_locks
WHERE NOT granted;
```

Rows here are processes waiting for a lock. Cross-reference each `pid` with
`pg_blocking_pids(pid)` to find the holder.

## 13. Fix an estimate-vs-actual mismatch :

```sql
EXPLAIN ANALYZE
  SELECT * FROM addresses WHERE city = 'Berlin' AND zip = '10115';
-- ... rows=3 ... (actual ... rows=2400 loops=1)   <-- estimate 800x too low

-- step 1: refresh per-column statistics
ANALYZE addresses;

-- step 2: if still wrong because city and zip are correlated, add extended stats
CREATE STATISTICS addr_city_zip (dependencies, ndistinct, mcv)
  ON city, zip FROM addresses;
ANALYZE addresses;   -- extended statistics apply only after this ANALYZE
```

Re-run `EXPLAIN ANALYZE`: the estimate should now track the actual row count, and the
planner can pick a correct join strategy downstream.

## 14. Diagnostic plan-type comparison :

```sql
-- force the alternative to compare costs, session-scoped only
SET enable_seqscan = off;
EXPLAIN ANALYZE SELECT * FROM orders WHERE status = 'pending';
RESET enable_seqscan;   -- ALWAYS reset; never leave it set
```

If the index plan is faster, the planner mis-costed the Seq Scan, usually because of a
stale estimate. Fix the estimate (Example 13) rather than leaving `enable_seqscan` off.
