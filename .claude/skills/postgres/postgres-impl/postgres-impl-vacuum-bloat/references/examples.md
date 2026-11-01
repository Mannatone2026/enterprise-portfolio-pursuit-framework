# examples : VACUUM, bloat, autovacuum, HOT, wraparound

Working code samples for postgres-impl-vacuum-bloat. Every snippet is
version-annotated. Verified against PostgreSQL 17 documentation; the
behaviour is identical on 15, 16, and 17 except where noted.

## 1. Detect bloat across all tables (v15+)

```sql
-- Dead-tuple ratio and autovacuum recency, worst tables first.
SELECT schemaname,
       relname,
       n_live_tup,
       n_dead_tup,
       round(n_dead_tup * 100.0
             / nullif(n_live_tup + n_dead_tup, 0), 1) AS dead_pct,
       last_autovacuum,
       autovacuum_count
FROM pg_stat_user_tables
WHERE n_dead_tup > 0
ORDER BY n_dead_tup DESC
LIMIT 25;
```

A `dead_pct` above ~20% on an active table, combined with an old or NULL
`last_autovacuum`, means autovacuum is behind on that table.

## 2. Exact bloat measurement with pgstattuple (v15+)

```sql
CREATE EXTENSION IF NOT EXISTS pgstattuple;

-- Exact scan: reads the whole table.
SELECT table_len,
       tuple_count,
       tuple_percent,
       dead_tuple_count,
       dead_tuple_percent,
       free_percent
FROM pgstattuple('public.orders');

-- Approximate: skips all-visible pages, much faster on large tables.
SELECT approx_tuple_count,
       approx_tuple_percent,
       dead_tuple_count,
       dead_tuple_percent,
       approx_free_percent
FROM pgstattuple_approx('public.orders');
```

Use `pgstattuple` when you need a definitive number; use `pgstattuple_approx`
for a quick check on a table too large to scan fully.

## 3. Manual VACUUM variants (v15+)

```sql
-- Reclaim dead-tuple space for reuse, refresh statistics, print a report.
VACUUM (VERBOSE, ANALYZE) public.orders;

-- Statistics only, no space reclaim.
ANALYZE public.orders;

-- Skip tables that cannot be locked immediately (good for cron jobs).
VACUUM (SKIP_LOCKED, ANALYZE);

-- Parallel index cleanup with up to 4 workers (v13+; needs >= 2 indexes).
VACUUM (PARALLEL 4, VERBOSE) public.big_indexed_table;

-- Limit VACUUM's buffer footprint so it does not evict the hot working set.
VACUUM (BUFFER_USAGE_LIMIT '4MB') public.orders;   -- v16+
```

## 4. Reclaim disk space (choose by environment)

```sql
-- Maintenance window only: ACCESS EXCLUSIVE, blocks all reads and writes.
SET lock_timeout = '5s';          -- fail fast instead of queuing forever
VACUUM FULL public.orders;
RESET lock_timeout;
```

```bash
# Live system: online rewrite, brief ACCESS EXCLUSIVE only at the swap.
# Requires: CREATE EXTENSION pg_repack;  and the pg_repack client binary.
pg_repack --dbname mydb --table public.orders --jobs 2
```

`pg_repack` needs the target table to have a primary key or a non-partial
unique index. It builds the rebuilt copy alongside the live table, so free
disk space for a second copy is still required.

## 5. Per-table autovacuum tuning for a large table (v15+)

```sql
-- A 100M-row table at the 0.2 global scale factor waits for ~20M dead
-- tuples. Lower the per-table scale factor and pin a fixed threshold.
ALTER TABLE public.events SET (
  autovacuum_vacuum_scale_factor        = 0.01,
  autovacuum_vacuum_threshold           = 10000,
  autovacuum_analyze_scale_factor       = 0.005,
  autovacuum_analyze_threshold          = 10000
);

-- Inspect the per-table settings currently in force.
SELECT relname, reloptions
FROM pg_class
WHERE relname = 'events';

-- Revert to the global defaults.
ALTER TABLE public.events RESET (
  autovacuum_vacuum_scale_factor,
  autovacuum_vacuum_threshold,
  autovacuum_analyze_scale_factor,
  autovacuum_analyze_threshold
);
```

## 6. Make autovacuum finish faster when it runs too slowly (v15+)

```sql
-- Autovacuum throttles itself via a cost budget. If a vacuum runs for hours
-- and never catches up, raise the budget and the working memory.
ALTER SYSTEM SET autovacuum_vacuum_cost_limit = 2000;   -- default -1 -> 200
ALTER SYSTEM SET autovacuum_vacuum_cost_delay = '2ms';
ALTER SYSTEM SET maintenance_work_mem = '512MB';
SELECT pg_reload_conf();
```

A higher `maintenance_work_mem` lets one VACUUM pass remove more dead tuples;
on v17 there is no 1GB cap, so the full value is used.

## 7. HOT updates and FILLFACTOR (v15+)

```sql
-- Reserve 10% free space per page so updates can stay on-page (HOT).
ALTER TABLE public.sessions SET (fillfactor = 90);

-- fillfactor only affects pages written AFTER the change. Rewrite the table
-- to apply it to existing pages:
--   VACUUM FULL public.sessions;          -- maintenance window
--   pg_repack -t public.sessions          -- online

-- Measure HOT effectiveness.
SELECT relname,
       n_tup_upd,
       n_tup_hot_upd,
       round(n_tup_hot_upd * 100.0 / nullif(n_tup_upd, 0), 1) AS hot_pct
FROM pg_stat_user_tables
WHERE n_tup_upd > 1000
ORDER BY n_tup_upd DESC;
```

A HOT update requires BOTH : no indexed column changed AND free space on the
old row's page. A low `hot_pct` on a heavily updated table means one of
those two conditions is failing.

## 8. Monitor transaction-ID wraparound (v15+)

```sql
-- Per-table XID age, including each table's TOAST relation.
SELECT c.oid::regclass AS table_name,
       greatest(age(c.relfrozenxid), age(t.relfrozenxid)) AS xid_age,
       mxid_age(c.relminmxid)                            AS mxid_age
FROM pg_class c
LEFT JOIN pg_class t ON c.reltoastrelid = t.oid
WHERE c.relkind IN ('r', 'm')
ORDER BY xid_age DESC
LIMIT 20;

-- Per-database age. Compare against autovacuum_freeze_max_age (200M default).
SELECT datname,
       age(datfrozenxid)    AS xid_age,
       mxid_age(datminmxid) AS mxid_age
FROM pg_database
ORDER BY age(datfrozenxid) DESC;
```

When `xid_age` approaches `autovacuum_freeze_max_age`, the server forces an
anti-wraparound autovacuum that cannot be skipped.

## 9. Wraparound emergency recovery (v15+)

The server logs progressively :

```
WARNING:  database "mydb" must be vacuumed within 39985967 transactions
HINT:  To avoid XID assignment failures, execute a database-wide VACUUM in that database.
```

```
ERROR:  database is not accepting commands that assign new XIDs
        to avoid wraparound data loss in database "mydb"
HINT:  Execute a database-wide VACUUM in that database.
```

Recovery, in order :

```sql
-- 1. Old prepared transactions hold an old xmin: commit or roll them back.
SELECT gid, prepared, owner, database FROM pg_prepared_xacts;
-- ROLLBACK PREPARED 'the_gid';

-- 2. Long-running or idle-in-transaction sessions hold an old xmin: end them.
SELECT pid, state, xact_start, query
FROM pg_stat_activity
WHERE state IN ('active', 'idle in transaction')
ORDER BY xact_start;
-- SELECT pg_terminate_backend(<pid>);

-- 3. Abandoned replication slots hold an old xmin: drop them.
SELECT slot_name, active, xmin FROM pg_replication_slots;
-- SELECT pg_drop_replication_slot('dead_slot');
```

```bash
# 4. Run a plain, database-wide VACUUM. NOT VACUUM FULL, NOT VACUUM FREEZE.
vacuumdb --all --verbose
```

If write commands are already blocked, the server still allows the VACUUM
itself to run; once XIDs are reclaimed, writes resume automatically.

## 10. Find the session blocking VACUUM cleanup (v15+)

```sql
-- VACUUM cannot remove tuples newer than the oldest snapshot still held.
-- This session pins that snapshot:
SELECT pid,
       state,
       backend_xmin,
       now() - xact_start AS xact_age,
       query
FROM pg_stat_activity
WHERE backend_xmin IS NOT NULL
ORDER BY xact_age DESC;
```

```sql
-- Prevent the recurrence: cap idle-in-transaction sessions.
ALTER SYSTEM SET idle_in_transaction_session_timeout = '5min';
SELECT pg_reload_conf();
```

## 11. Watch a running VACUUM (v15+)

```sql
SELECT p.pid,
       p.relid::regclass AS table_name,
       p.phase,
       p.heap_blks_total,
       p.heap_blks_scanned,
       round(p.heap_blks_scanned * 100.0
             / nullif(p.heap_blks_total, 0), 1) AS pct_done
FROM pg_stat_progress_vacuum p;
```

## Sources

- https://www.postgresql.org/docs/17/sql-vacuum.html
- https://www.postgresql.org/docs/17/routine-vacuuming.html
- https://www.postgresql.org/docs/17/storage-hot.html
- https://www.postgresql.org/docs/17/progress-reporting.html
- https://www.postgresql.org/docs/17/pgstattuple.html
