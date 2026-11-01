---
name: postgres-impl-vacuum-bloat
description: >
  Use when a table is bloated, queries slow down over time, autovacuum is not keeping up, or you see transaction-ID wraparound warnings.
  Prevents VACUUM FULL locking a live production table, wraparound emergency from disabled autovacuum, and index bloat from frequent indexed-column updates at fillfactor 100.
  Covers MVCC dead tuples, VACUUM vs VACUUM FULL vs ANALYZE, autovacuum scale-factor tuning (global + per-table), bloat detection via pg_stat_user_tables, HOT updates, FILLFACTOR, transaction-ID + multixact wraparound, pg_repack alternative.
  Keywords: VACUUM, autovacuum, VACUUM FULL, bloat, dead tuples, n_dead_tup, FILLFACTOR, HOT update, transaction id wraparound, relfrozenxid, pg_repack, table is bloated, queries getting slower, database not accepting commands wraparound, autovacuum not keeping up, disk usage growing
license: MIT
compatibility: "Designed for Claude Code. Requires PostgreSQL 15, 16, or 17."
metadata:
  author: OpenAEC-Foundation
  version: "1.0"
---

# postgres-impl-vacuum-bloat

## Quick Reference :

Every UPDATE and DELETE in PostgreSQL leaves the old row version on disk as a
dead tuple (MVCC). VACUUM reclaims that space; autovacuum runs VACUUM in the
background. When autovacuum falls behind, tables bloat (dead tuples outnumber
live tuples), queries scan more pages than needed, and disk usage climbs.
This skill diagnoses bloat, tunes autovacuum, maximizes HOT updates, and
handles transaction-ID wraparound.

Core decision summary :

- Reclaim space WITHOUT a lock : plain `VACUUM` (reuses space inside the table).
- Return space to the OS : `VACUUM FULL` (ACCESS EXCLUSIVE lock) OR `pg_repack`
  (online). NEVER `VACUUM FULL` a live production table.
- Stale query plans : `ANALYZE` (statistics only, no space reclaim).
- Autovacuum behind on a big table : lower `autovacuum_vacuum_scale_factor`
  per-table, do NOT disable autovacuum.
- Wraparound warning : run a plain database-wide `VACUUM` immediately.
- Index bloat from UPDATEs : lower `fillfactor` so HOT updates skip the index.

Default autovacuum trigger : a table is vacuumed when dead tuples exceed
`autovacuum_vacuum_threshold (50) + autovacuum_vacuum_scale_factor (0.2) * reltuples`.

## When To Use This Skill :

ALWAYS use this skill when :
- A table is bloated or disk usage grows without matching row growth.
- Queries on an UPDATE/DELETE-heavy table slow down over time.
- `pg_stat_user_tables.n_dead_tup` is large or `last_autovacuum` is old/NULL.
- You see "must be vacuumed within N transactions" or "database is not
  accepting commands ... to avoid wraparound" messages.
- You need to reclaim disk space from a table on a live system.
- You are tuning autovacuum, FILLFACTOR, or HOT-update behavior.

NEVER use this skill for :
- Query plan analysis or EXPLAIN reading : use postgres-impl-query-performance-toolkit.
- Choosing or building indexes : use postgres-impl-indexing-strategy.
- Bulk-load throughput tuning : use postgres-impl-bulk-loading.
- MVCC snapshot/isolation semantics : use the MVCC core skill.

## Decision Trees :

### Which VACUUM variant do I run? :

```
Goal?
├── Reclaim dead-tuple space for reuse inside the table (no OS return)
│   └── VACUUM table;            -- SHARE UPDATE EXCLUSIVE, online
├── Refresh planner statistics only
│   └── ANALYZE table;           -- no space reclaim
├── Reclaim space AND refresh statistics
│   └── VACUUM (ANALYZE) table;  -- online
├── Return disk space to the operating system
│   ├── Table is on a LIVE system  -> use pg_repack (online rewrite)
│   └── Maintenance window OK      -> VACUUM FULL table; (ACCESS EXCLUSIVE)
└── Prevent wraparound aggressively (freeze all old tuples now)
    └── VACUUM (FREEZE) table;    -- or plain VACUUM, autovacuum handles it
```

### Autovacuum is not keeping up :

```
Symptom: n_dead_tup keeps growing, last_autovacuum stays old.
├── One huge table?
│   └── Per-table lower scale factor:
│       ALTER TABLE t SET (autovacuum_vacuum_scale_factor = 0.05,
│                          autovacuum_vacuum_threshold   = 10000);
├── Autovacuum runs but is too slow (long-running, never finishes)?
│   └── Raise autovacuum_vacuum_cost_limit / lower autovacuum_vacuum_cost_delay
│       and raise maintenance_work_mem (see references/methods.md).
├── A long-running or "idle in transaction" session holds an old xmin?
│   └── VACUUM cannot remove tuples newer than that xmin.
│       Find it via pg_stat_activity, end it, set
│       idle_in_transaction_session_timeout.
└── Insert-only table never triggered until wraparound?
    └── autovacuum_vacuum_insert_scale_factor / _threshold handle it (v13+);
        confirm they are not disabled.
```

### Wraparound warning appeared :

```
Message: "database <db> must be vacuumed within N transactions"  (WARNING, ~40M left)
   or  : "database is not accepting commands ... to avoid wraparound"  (ERROR, <3M left)
├── 1. Find what blocks freezing:
│      - pg_prepared_xacts        -> COMMIT/ROLLBACK old prepared txns
│      - pg_stat_activity         -> end long-running / idle-in-txn sessions
│      - pg_replication_slots     -> drop abandoned slots holding old xmin
├── 2. Run a plain database-wide VACUUM (NOT VACUUM FULL, NOT VACUUM FREEZE):
│      vacuumdb --all --verbose      (or VACUUM; per database)
└── 3. After recovery: lower autovacuum_freeze_max_age headroom is not the
       fix; ensure autovacuum is enabled and tuned so it never recurs.
```

## Patterns :

### Pattern 1 : Detect bloat with pg_stat_user_tables

ALWAYS : measure dead-tuple ratio and autovacuum recency before acting.
NEVER : assume a table is bloated from disk size alone (large live data looks
the same as bloat on disk).

```sql
SELECT relname,
       n_live_tup,
       n_dead_tup,
       round(n_dead_tup * 100.0
             / nullif(n_live_tup + n_dead_tup, 0), 1) AS dead_pct,
       last_autovacuum,
       last_autoanalyze
FROM pg_stat_user_tables
ORDER BY n_dead_tup DESC
LIMIT 20;
```

WHY : `n_dead_tup` is the reclaimable backlog; `dead_pct` above ~20% on a
hot table signals autovacuum is behind. `last_autovacuum` being NULL or
days old confirms it. For an exact (slower) measurement use the
`pgstattuple` extension : `SELECT * FROM pgstattuple('my_table');` returns
`dead_tuple_percent` and `free_percent`. See references/examples.md.

### Pattern 2 : Per-table autovacuum tuning for large tables

ALWAYS : lower `autovacuum_vacuum_scale_factor` on tables over a few million
rows so vacuum fires on an absolute backlog, not a percentage.
NEVER : raise the global scale factor to fix one table.

```sql
-- 100M-row table: global 0.2 means 20M dead tuples before vacuum fires.
-- Per-table override makes it fire at ~2M dead tuples instead.
ALTER TABLE events SET (
  autovacuum_vacuum_scale_factor  = 0.01,
  autovacuum_vacuum_threshold     = 10000,
  autovacuum_analyze_scale_factor = 0.005
);
```

WHY : the trigger formula is
`threshold + scale_factor * reltuples`. With the 0.2 default, bigger tables
wait proportionally longer, so the largest tables bloat the most. A small
scale factor plus a fixed threshold keeps the dead-tuple backlog bounded.

### Pattern 3 : Maximize HOT updates with FILLFACTOR

ALWAYS : lower `fillfactor` (to 80-90) on UPDATE-heavy tables AND avoid
updating indexed columns, so updates qualify as Heap-Only Tuple updates.
NEVER : leave `fillfactor` at the default 100 on a table that is updated
constantly while expecting low index bloat.

```sql
ALTER TABLE sessions SET (fillfactor = 90);
-- fillfactor only affects pages written AFTER this command.
-- Rewrite the table to apply it to existing pages:
VACUUM FULL sessions;        -- maintenance window, OR
-- pg_repack -t sessions     -- online

-- Verify HOT effectiveness:
SELECT relname, n_tup_upd, n_tup_hot_upd,
       round(n_tup_hot_upd * 100.0 / nullif(n_tup_upd, 0), 1) AS hot_pct
FROM pg_stat_user_tables
WHERE n_tup_upd > 0
ORDER BY n_tup_upd DESC;
```

WHY : a HOT update needs two conditions, both required : (1) no indexed
column is modified, (2) the page holding the old row has free space for the
new version. HOT updates create no new index entries and can be cleaned up
without VACUUM, so they prevent index bloat. A low `hot_pct` on a heavily
updated table means an indexed column is being touched or pages are full.

### Pattern 4 : Monitor transaction-ID and multixact wraparound

ALWAYS : monitor `age(relfrozenxid)` and act long before it reaches
`autovacuum_freeze_max_age` (default 200M).
NEVER : disable autovacuum, ignore wraparound warnings, or use
`VACUUM FULL` to recover from a wraparound emergency.

```sql
-- Per-table XID age (includes the table's TOAST relation):
SELECT c.oid::regclass AS table_name,
       greatest(age(c.relfrozenxid), age(t.relfrozenxid)) AS xid_age,
       mxid_age(c.relminmxid)                            AS mxid_age
FROM pg_class c
LEFT JOIN pg_class t ON c.reltoastrelid = t.oid
WHERE c.relkind IN ('r', 'm')
ORDER BY xid_age DESC
LIMIT 20;

-- Per-database age:
SELECT datname, age(datfrozenxid), mxid_age(datminmxid)
FROM pg_database
ORDER BY age(datfrozenxid) DESC;
```

WHY : XIDs are 32-bit. When the oldest unfrozen XID gets ~40M transactions
from wraparound the server logs a WARNING; with under 3M left it stops
accepting write commands ("database is not accepting commands that assign
new XIDs to avoid wraparound data loss"). VACUUM freezes old tuples to push
this back. `multixact` IDs (used for multi-transaction row locks) wrap the
same way and need the same monitoring via `mxid_age()`.

### Pattern 5 : Reclaim disk space online with pg_repack

ALWAYS : on a live system, use `pg_repack` to rewrite a bloated table and
return space to the OS, NOT `VACUUM FULL`.
NEVER : run `VACUUM FULL` on a table that production traffic depends on.

```bash
# Requires: CREATE EXTENSION pg_repack;  + the pg_repack client binary.
# The target table needs a PRIMARY KEY or a non-partial unique index.
pg_repack --dbname mydb --table events --jobs 2
```

WHY : `VACUUM FULL` holds an ACCESS EXCLUSIVE lock for the entire rewrite,
blocking every read and write, and needs free disk space for a second copy.
`pg_repack` builds the new copy alongside the live table and only takes a
brief ACCESS EXCLUSIVE lock at the final swap, so the table stays available.
`CLUSTER` has the same blocking downside as `VACUUM FULL`.

### Pattern 6 : Manual VACUUM with VERBOSE and PARALLEL

ALWAYS : use `VACUUM (VERBOSE)` to see what was removed and `VACUUM
(PARALLEL n)` (v13+) to speed up index cleanup on large multi-index tables.
NEVER : combine `PARALLEL` with `FULL` (rejected by the parser).

```sql
VACUUM (VERBOSE, ANALYZE) orders;
VACUUM (PARALLEL 4) big_indexed_table;   -- v13+, plain VACUUM only
VACUUM (FREEZE, VERBOSE) archive_2024;   -- freeze all tuples now
```

WHY : `VERBOSE` reports dead tuples removed, pages skipped, and index work.
`PARALLEL n` uses up to n background workers (one per eligible index, capped
by `max_parallel_maintenance_workers`); it needs at least two indexes to
launch any worker. v16 added `BUFFER_USAGE_LIMIT`; v17 removed the historical
1GB cap on dead-tuple tracking memory. See references/methods.md.

## Anti-Patterns :

(Full cause + symptom + fix in references/anti-patterns.md)

- `VACUUM FULL` on a live production table : ACCESS EXCLUSIVE lock blocks all
  reads and writes for the whole rewrite.
- Disabling autovacuum globally : guarantees a transaction-ID wraparound
  shutdown of all write commands.
- Ignoring `n_dead_tup` growth : bloat makes every scan touch dead pages,
  queries slow down progressively.
- `fillfactor = 100` plus frequent indexed-column UPDATEs : no HOT updates,
  index bloat grows unbounded.
- One global `autovacuum_vacuum_scale_factor` for huge tables : 0.2 of 100M
  rows means 20M dead tuples accumulate before vacuum fires.
- No `lock_timeout` before `VACUUM FULL` / `pg_repack` swap : the lock
  request can queue behind and then ahead of normal queries : SQLSTATE 55P03.
- `VACUUM FULL` during a wraparound emergency : slower and lock-heavy when a
  plain `VACUUM` is what clears the XIDs.

## Reference Links :

- [references/methods.md](references/methods.md) : Full VACUUM option surface,
  autovacuum parameter table with defaults, monitoring views and columns.
- [references/examples.md](references/examples.md) : Working bloat-detection,
  tuning, HOT, wraparound and pg_repack code samples, version-annotated.
- [references/anti-patterns.md](references/anti-patterns.md) : Anti-patterns
  with cause, symptom, fix-pattern, and SQLSTATE where applicable.

## See Also :

- postgres-impl-query-performance-toolkit : EXPLAIN, pg_stat_statements,
  diagnosing whether slowness is bloat or a bad plan.
- postgres-impl-indexing-strategy : index types, fillfactor on indexes,
  unused-index detection.
- postgres-impl-bulk-loading : disabling autovacuum during a load and
  running VACUUM ANALYZE afterward.
- Vooronderzoek section : §7 (VACUUM, Bloat, Autovacuum).
- Official docs : https://www.postgresql.org/docs/17/routine-vacuuming.html ,
  https://www.postgresql.org/docs/17/sql-vacuum.html ,
  https://www.postgresql.org/docs/17/storage-hot.html
