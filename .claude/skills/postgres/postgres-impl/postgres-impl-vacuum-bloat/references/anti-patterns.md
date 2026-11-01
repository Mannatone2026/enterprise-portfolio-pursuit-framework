# anti-patterns : VACUUM, bloat, autovacuum

Each entry : cause, symptom, fix-pattern, SQLSTATE where applicable.
Verified against PostgreSQL 17 documentation. Behaviour is identical on 15,
16, and 17 unless noted.

## AP-1 : VACUUM FULL on a live production table

CAUSE : `VACUUM FULL` rewrites the entire table into a new file and holds an
ACCESS EXCLUSIVE lock for the whole rewrite, not just a moment.

SYMPTOM : every SELECT, INSERT, UPDATE and DELETE on the table blocks until
the rewrite finishes; on a large table this is minutes to hours of outage.
Connections pile up; the application appears frozen.

FIX-PATTERN : never run `VACUUM FULL` on a table that live traffic depends
on. Use `pg_repack` for an online rewrite that takes ACCESS EXCLUSIVE only
briefly at the final swap. Reserve `VACUUM FULL` for maintenance windows,
and always set `lock_timeout` first so the lock request fails fast instead
of queuing.

SQLSTATE : none for the blocking itself (it is wait, not error). If
`lock_timeout` is set and exceeded the waiter gets `55P03 lock_not_available`.

## AP-2 : Disabling autovacuum to "save resources"

CAUSE : `autovacuum = off` globally, or `autovacuum_enabled = false` left on
many tables. Dead tuples are never reclaimed and old XIDs are never frozen.

SYMPTOM : steady table and index bloat; then the wraparound sequence :
`WARNING: database "..." must be vacuumed within N transactions`, and finally
`ERROR: database is not accepting commands that assign new XIDs to avoid
wraparound data loss`. All write commands stop.

FIX-PATTERN : keep autovacuum on. To reduce its impact, tune it (raise
`autovacuum_vacuum_cost_limit`, lower `autovacuum_vacuum_cost_delay`, raise
`maintenance_work_mem`) instead of disabling it. Per-table
`autovacuum_enabled = false` is acceptable only for short-lived staging
tables during a bulk load, and must be re-enabled afterward. Note that an
anti-wraparound vacuum runs even on tables with `autovacuum_enabled = false`.

SQLSTATE : the wraparound shutdown is enforced by the server before a write
command runs; it surfaces as the quoted ERROR rather than a stable
application-level SQLSTATE. Treat the log message as the signal.

## AP-3 : Ignoring n_dead_tup growth

CAUSE : no monitoring of `pg_stat_user_tables.n_dead_tup`; autovacuum is
behind but nobody notices until queries are slow.

SYMPTOM : queries on an UPDATE/DELETE-heavy table get progressively slower;
sequential and index scans read pages full of dead tuples; `n_dead_tup`
exceeds `n_live_tup`; disk usage grows without matching row growth.

FIX-PATTERN : monitor `dead_pct` and `last_autovacuum` regularly (see
references/examples.md §1). When a table is chronically behind, lower its
per-table `autovacuum_vacuum_scale_factor`. To clear an existing backlog run
a manual `VACUUM (VERBOSE, ANALYZE)`; to also shrink the on-disk file use
`pg_repack`.

SQLSTATE : none (performance degradation, not an error).

## AP-4 : fillfactor 100 plus frequent indexed-column UPDATEs

CAUSE : the table keeps the default `fillfactor = 100` (no reserved page
space) and updates touch a column that an index references. Both HOT
conditions fail, so every UPDATE writes a new index entry.

SYMPTOM : low `n_tup_hot_upd / n_tup_upd` ratio; indexes bloat faster than
the table; index-only scans degrade; `REINDEX` is needed often.

FIX-PATTERN : lower `fillfactor` to 80-90 on update-heavy tables so the new
row version fits on the same page, and avoid indexing columns that are
updated on every row change. Apply the new `fillfactor` to existing pages by
rewriting the table (`pg_repack`, or `VACUUM FULL` in a window). Verify the
HOT ratio recovers (see references/examples.md §7).

SQLSTATE : none (bloat, not an error).

## AP-5 : One global scale factor for very large tables

CAUSE : relying on the global `autovacuum_vacuum_scale_factor = 0.2` for
every table regardless of size.

SYMPTOM : the largest tables bloat the most. The trigger is
`threshold + scale_factor * reltuples`, so a 100M-row table waits for ~20M
dead tuples before autovacuum fires, while a 1000-row table is vacuumed
almost immediately.

FIX-PATTERN : set a low per-table `autovacuum_vacuum_scale_factor` (0.01-0.05)
plus a fixed `autovacuum_vacuum_threshold` on big tables, so vacuum fires on
a bounded absolute backlog. Never raise the global scale factor to paper
over one table.

SQLSTATE : none.

## AP-6 : No lock_timeout before VACUUM FULL or a pg_repack swap

CAUSE : requesting an ACCESS EXCLUSIVE lock with no `lock_timeout`. The lock
request queues; every new query arriving after it also queues behind it.

SYMPTOM : a "convoy" where the whole table is effectively unavailable while
the VACUUM FULL waits for one slow query, and all later queries wait for the
VACUUM FULL.

FIX-PATTERN : `SET lock_timeout = '5s';` before `VACUUM FULL` (and rely on
`pg_repack`'s own `--wait-timeout`). If the lock cannot be taken quickly, the
command fails cleanly and is retried later instead of stalling the table.

SQLSTATE : `55P03 lock_not_available` when `lock_timeout` is exceeded.

## AP-7 : VACUUM FULL or VACUUM FREEZE during a wraparound emergency

CAUSE : reacting to a wraparound warning with `VACUUM FULL` (to "really
clean it") or `VACUUM FREEZE`.

SYMPTOM : `VACUUM FULL` takes an ACCESS EXCLUSIVE lock and rewrites the whole
table, which is far slower and more disruptive than the plain `VACUUM` that
actually clears the XIDs; the emergency is prolonged.

FIX-PATTERN : in a wraparound emergency run a plain, database-wide `VACUUM`
(`vacuumdb --all`). First clear what blocks freezing : old prepared
transactions, long-running or idle-in-transaction sessions, and abandoned
replication slots (see references/examples.md §9).

SQLSTATE : none for the recovery path itself.

## AP-8 : Long-running and idle-in-transaction sessions starving VACUUM

CAUSE : a session holds an open transaction (especially `idle in
transaction`). VACUUM cannot remove any dead tuple newer than that session's
snapshot, because the session might still need to see it.

SYMPTOM : autovacuum runs and reports success, yet `n_dead_tup` keeps
climbing; `pg_stat_activity.backend_xmin` shows an old value on a stuck
session.

FIX-PATTERN : find the session via `pg_stat_activity` (order by
`backend_xmin` / `xact_start`), end it, and set
`idle_in_transaction_session_timeout` so it cannot recur. Also check
`pg_replication_slots` and `pg_prepared_xacts` for the same xmin-pinning
effect.

SQLSTATE : a session terminated by `idle_in_transaction_session_timeout`
receives `25P03 idle_in_transaction_session_timeout`.

## AP-9 : INDEX_CLEANUP OFF left on as a permanent setting

CAUSE : running `VACUUM (INDEX_CLEANUP OFF)` routinely (it is faster) or
pinning it per-table to skip index vacuuming.

SYMPTOM : the heap is vacuumed but indexes accumulate dead pointers; index
bloat grows and index scans slow down, even though table bloat looks fine.

FIX-PATTERN : leave `INDEX_CLEANUP` at `AUTO`. `OFF` is a deliberate,
temporary tool to get a fast anti-wraparound vacuum through under pressure,
not a steady-state configuration.

SQLSTATE : none.

## Sources

- https://www.postgresql.org/docs/17/sql-vacuum.html
- https://www.postgresql.org/docs/17/routine-vacuuming.html
- https://www.postgresql.org/docs/17/storage-hot.html
- https://www.postgresql.org/docs/17/errcodes-appendix.html
- https://www.postgresql.org/docs/17/runtime-config-client.html
