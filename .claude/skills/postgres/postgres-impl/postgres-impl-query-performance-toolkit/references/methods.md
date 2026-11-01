# Methods : Query Performance Toolkit

API surface for EXPLAIN, plan nodes, and the statistics views. All verified against
PostgreSQL 17 official documentation. Version annotations mark features added after v15.

## EXPLAIN options :

Syntax : `EXPLAIN [ ( option [, ...] ) ] statement`

| Option | Default | Effect |
|--------|---------|--------|
| `ANALYZE` | off | Executes the statement, reports real `actual time` and `rows`. |
| `VERBOSE` | off | Adds output column lists, schema-qualified names, function names. |
| `COSTS` | on | Shows the `cost=startup..total rows=N width=W` estimates. |
| `SETTINGS` | off | Lists non-default planner GUCs that influenced the plan. |
| `BUFFERS` | off | Reports buffer usage per node (`shared hit/read/dirtied/written`). |
| `WAL` | off | Reports WAL records / bytes generated (meaningful with `ANALYZE` on writes). |
| `TIMING` | on | Per-node timing. Set `off` to reduce `ANALYZE` overhead on slow clocks. |
| `SUMMARY` | on with `ANALYZE` | Adds planning time and total execution time footer. |
| `MEMORY` | off | Reports planner memory consumption. **v17+**. |
| `SERIALIZE` | off | Measures cost of converting output to text/binary. **v17+**. |
| `GENERIC_PLAN` | off | Plans a parameterized statement without executing. **v16+**. |
| `FORMAT` | `TEXT` | Output as `TEXT`, `XML`, `JSON`, or `YAML`. |

Legacy positional form `EXPLAIN ANALYZE statement` and `EXPLAIN VERBOSE statement`
still work; the parenthesized form is required to combine three or more options.

### Cost line anatomy :

```
Seq Scan on tenk1  (cost=0.00..445.00 rows=10000 width=244)
```

- `cost=0.00..445.00` : `startup_cost..total_cost` in arbitrary planner units. Startup
  cost is work before the first row is emitted (sorting, hashing). Total cost assumes
  the node runs to completion.
- `rows=10000` : estimated number of rows the node will EMIT (after its filter), not
  the number scanned.
- `width=244` : estimated average row width in bytes.

With `ANALYZE`, each node gains a second parenthesis:

```
(actual time=0.017..0.051 rows=10 loops=1)
```

- `actual time=min..max` : real milliseconds, time-to-first-row .. time-to-last-row,
  averaged per loop.
- `rows` : real rows emitted, averaged per loop.
- `loops` : how many times the node ran. Multiply `actual time` and `rows` by `loops`
  for the node total. An inner node of a Nested Loop has `loops` = outer row count.

## Plan node catalog :

### Scan nodes :

| Node | When the planner picks it | Read as |
|------|---------------------------|---------|
| Seq Scan | Small table, or query returns a large fraction of rows. | Reads every heap page. Optimal for small / low-selectivity. |
| Index Scan | Highly selective predicate (~1-10% of table). | Walks the index, then fetches matching heap rows. |
| Index Only Scan | Query columns all live in the index; visibility map is clean. | Skips the heap entirely when pages are all-visible. |
| Bitmap Index Scan | Medium selectivity, or combining several indexes. | Builds a bitmap of matching tuple locations. |
| Bitmap Heap Scan | Always paired above a Bitmap Index Scan. | Fetches heap pages in physical order; `Recheck Cond` re-tests rows. |

`BitmapAnd` / `BitmapOr` combine two Bitmap Index Scans before the heap fetch.

### Join nodes :

| Node | Optimal when | Cost shape |
|------|--------------|-----------|
| Nested Loop | Outer side is tiny; inner has an index on the join key. | Inner runs once per outer row (`loops` = outer rows). |
| Hash Join | Medium-to-large equality join; one side fits in `work_mem`. | Builds a hash table on the smaller side, probes with the other. |
| Merge Join | Very large equality join, or inputs already sorted on the key. | Both inputs sorted, then merged in one linear pass. |

A Hash Join reporting `Batches: N` with `N > 1` spilled the hash table to disk; raising
`work_mem` may keep it to a single batch.

### Other nodes :

| Node | Meaning |
|------|---------|
| Sort | Orders rows. `Sort Method: quicksort` = in memory; `external merge` = disk spill. `top-N heapsort` for `ORDER BY ... LIMIT k`. |
| Incremental Sort | Reuses an already-sorted prefix, sorts only within groups. Available v15+. |
| Aggregate | Plain aggregate with no `GROUP BY`. |
| HashAggregate | `GROUP BY` over unsorted input; groups via a hash table. |
| GroupAggregate | `GROUP BY` over input already sorted on the grouping keys. |
| Memoize | Per-parameter result cache on the inner side of a parameterized Nested Loop. Available v15+. Big win for repeated identical lookups. |
| Materialize | Buffers a subplan's rows so they can be rescanned without re-execution. |
| Gather / Gather Merge | Collects rows from parallel worker processes. |
| Append / Merge Append | Concatenates child plans from partitions or `UNION ALL`. |

### ANALYZE-only diagnostic lines :

| Line | Meaning |
|------|---------|
| `Rows Removed by Filter: N` | The node scanned rows and discarded N. High N = expensive `WHERE`, candidate for an index. |
| `Sort Method: external merge  Disk: 1024kB` | Sort spilled to disk; `work_mem` too small for this sort. |
| `Buffers: shared hit=N read=M` | `hit` = buffer-cache hit; `read` = fetched from OS/disk. High `read` = I/O bottleneck. |
| `Buffers: shared dirtied=N written=M` | Node dirtied / wrote out buffers. |
| `Heap Fetches: N` | Index Only Scan still visited the heap N times (visibility map not all-visible). |
| `Workers Launched: N` | Parallel workers actually started (may be below `Workers Planned`). |

## pg_stat_statements :

Setup, all three required :
1. `shared_preload_libraries = 'pg_stat_statements'` in postgresql.conf (server restart).
2. `compute_query_id = on` (or `auto`) in postgresql.conf.
3. `CREATE EXTENSION pg_stat_statements;` in each database to query.

Key view columns :

| Column | Meaning |
|--------|---------|
| `query` | Normalized query text. Constants replaced by `$1`, `$2`, ... |
| `queryid` | Stable hash of the normalized query. |
| `calls` | Number of times executed. |
| `total_exec_time` | Total execution time across all calls, milliseconds. |
| `mean_exec_time` | Average execution time per call, milliseconds. |
| `min_exec_time` / `max_exec_time` | Fastest / slowest single execution. |
| `stddev_exec_time` | Standard deviation of execution time. |
| `rows` | Total rows retrieved or affected. |
| `shared_blks_hit` | Blocks served from the buffer cache. |
| `shared_blks_read` | Blocks read from OS / disk. |
| `wal_bytes` | Total WAL bytes generated. |

Planning-time columns (`total_plan_time`, `mean_plan_time`, ...) populate only when
`pg_stat_statements.track_planning = on`.

Functions :
- `pg_stat_statements_reset(userid Oid DEFAULT 0, dbid Oid DEFAULT 0, queryid bigint DEFAULT 0, minmax_only boolean DEFAULT false)` : resets stats; no args resets everything.
- `pg_stat_statements(showtext boolean DEFAULT true)` : underlying set-returning function.

Companion view `pg_stat_statements_info` exposes `dealloc` (entries evicted because
`pg_stat_statements.max` was exceeded) and `stats_reset`.

Permissions : query text and `queryid` are visible only to superusers and members of
`pg_read_all_stats`; other roles see aggregate numbers with the text hidden.

## pg_stat_activity :

One row per server process. Relevant columns :

| Column | Meaning |
|--------|---------|
| `datname` | Database the backend is connected to. |
| `pid` | Backend process ID. Argument for cancel / terminate / blocking functions. |
| `usename` | Connected role. |
| `application_name` | Client-set application label. |
| `client_addr` | Client IP, NULL for local socket. |
| `backend_start` | When the backend connected. |
| `xact_start` | When the current transaction started. |
| `query_start` | When the current query started. `now() - query_start` = run time. |
| `state_change` | When `state` last changed. |
| `state` | See state values below. |
| `wait_event_type` / `wait_event` | What the backend is waiting on, NULL if not waiting. |
| `backend_xid` / `backend_xmin` | Transaction id / xmin horizon held by the backend. |
| `query` | Text of the current or most recent query. |
| `backend_type` | `client backend`, `autovacuum worker`, `walsender`, etc. |

`state` values : `active`, `idle`, `idle in transaction`,
`idle in transaction (aborted)`, `fastpath function call`, `disabled`.

`wait_event_type` values : `Activity`, `BufferPin`, `Client`, `Extension`,
`InjectionPoint`, `IO`, `IPC`, `Lock`, `LWLock`, `Timeout`. A value of `Lock` means
the backend is waiting for a heavyweight (table / row) lock.

## pg_locks :

One row per active or awaited lock. Columns :
`locktype`, `database`, `relation`, `page`, `tuple`, `virtualxid`, `transactionid`,
`classid`, `objid`, `objsubid`, `virtualtransaction`, `pid`, `mode`, `granted`,
`fastpath`, `waitstart`.

- `granted = true` : the process holds the lock.
- `granted = false` : the process is waiting for the lock; at least one conflicting
  lock is held or queued ahead of it.
- `mode` : lock mode, e.g. `AccessShareLock`, `RowExclusiveLock`, `AccessExclusiveLock`.
- `waitstart` : when the wait began (NULL if granted).

## Lock and session functions :

| Function | Returns | Use |
|----------|---------|-----|
| `pg_blocking_pids(integer)` | `integer[]` | PIDs blocking the given pid (hard and soft blocks). Empty array if not blocked. A prepared-transaction blocker shows as `0`. |
| `pg_cancel_backend(integer)` | `boolean` | Cancels the current query of a backend (SQLSTATE 57014). Session survives. |
| `pg_terminate_backend(integer)` | `boolean` | Terminates the whole backend. Use only when cancel is insufficient. |

## CREATE STATISTICS :

Multivariate form :
```
CREATE STATISTICS [ IF NOT EXISTS ] name
  [ ( kind [, ...] ) ]
  ON { column | ( expression ) }, { column | ( expression ) } [, ...]
  FROM table;
```

Statistics kinds :

| Kind | Captures | Helps |
|------|----------|-------|
| `ndistinct` | Number of distinct value combinations across the column group. | `GROUP BY` on multiple columns. |
| `dependencies` | Functional dependencies, e.g. `(zip) -> (city)`. | Multiple AND'd equality predicates on correlated columns. |
| `mcv` | Most-common combinations of values. | Filters that hit common value pairs. |

Omitting the kind clause builds all three. Extended statistics take effect only after
the next `ANALYZE` on the table. Single-expression form
`CREATE STATISTICS name ON (expression) FROM table` builds per-expression statistics,
similar to an expression index but without index maintenance cost.

## Diagnostic GUCs (session-scoped, never permanent) :

`SET enable_seqscan = off`, `SET enable_nestloop = off`, `SET enable_hashjoin = off`,
`SET enable_indexscan = off`. These bias the planner away from a node type so you can
compare the alternative plan's cost. They are diagnostic switches only. ALWAYS `RESET`
them or end the session afterwards; never write them to postgresql.conf.

## Sources :

- https://www.postgresql.org/docs/17/using-explain.html
- https://www.postgresql.org/docs/17/pgstatstatements.html
- https://www.postgresql.org/docs/17/monitoring-stats.html
- https://www.postgresql.org/docs/17/view-pg-locks.html
- https://www.postgresql.org/docs/17/functions-info.html
- https://www.postgresql.org/docs/17/sql-createstatistics.html
