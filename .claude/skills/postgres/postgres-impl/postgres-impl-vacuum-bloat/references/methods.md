# methods : VACUUM, autovacuum, bloat monitoring

API surface for postgres-impl-vacuum-bloat. Verified against PostgreSQL 17
official documentation (routine-vacuuming.html, sql-vacuum.html,
storage-hot.html). Defaults are the standard `postgresql.conf` defaults for
PostgreSQL 15, 16, and 17 unless a version annotation says otherwise.

## VACUUM command syntax

```
VACUUM [ ( option [, ...] ) ] [ table_and_columns [, ...] ]

table_and_columns is:
    table_name [ ( column_name [, ...] ) ]
```

`VACUUM` with no table vacuums every table in the current database the user
can vacuum. Specifying columns only affects the ANALYZE part.

### VACUUM options

| Option | Values | Effect |
|--------|--------|--------|
| `FULL` | boolean (default false) | Rewrite the whole table into a new file, return space to the OS. Takes ACCESS EXCLUSIVE lock. Needs disk for a second copy. |
| `FREEZE` | boolean (default false) | Aggressive freezing : equivalent to `vacuum_freeze_min_age = 0` and `vacuum_freeze_table_age = 0`. |
| `VERBOSE` | boolean (default false) | Print a per-table activity report (dead tuples removed, pages skipped, index work). |
| `ANALYZE` | boolean (default false) | Also update planner statistics. `VACUUM ANALYZE` is the shorthand. |
| `DISABLE_PAGE_SKIPPING` | boolean (default false) | Disable visibility-map page skipping. Use only for corruption recovery. |
| `SKIP_LOCKED` | boolean (default false) | Skip any relation that cannot be locked immediately instead of waiting. |
| `INDEX_CLEANUP` | `AUTO` (default) / `ON` / `OFF` | `AUTO` lets the server decide; `OFF` always skips index vacuuming (faster, but indexes keep dead pointers). |
| `PROCESS_MAIN` | boolean (default true) | Process the main relation. `false` skips it (e.g. to process only TOAST). |
| `PROCESS_TOAST` | boolean (default true) | Process the associated TOAST table. |
| `TRUNCATE` | boolean (default true) | Truncate trailing empty pages and return that space to the OS. |
| `PARALLEL` | integer | Use background workers for index/TOAST cleanup (v13+). `PARALLEL 0` disables it. Cannot combine with `FULL`. |
| `SKIP_DATABASE_STATS` | boolean (default false) | Skip updating the database-wide oldest-unfrozen-XID statistic. |
| `ONLY_DATABASE_STATS` | boolean (default false) | Only update database-wide statistics, vacuum nothing. |
| `BUFFER_USAGE_LIMIT` | size (128 kB - 16 GB) | Ring-buffer size for VACUUM's Buffer Access Strategy. `0` disables the strategy. **v16+.** |

### Lock behaviour

| Command | Lock | Concurrency |
|---------|------|-------------|
| plain `VACUUM` | SHARE UPDATE EXCLUSIVE | Reads and writes continue; space reused inside the table, not returned to OS. |
| `VACUUM FULL` | ACCESS EXCLUSIVE | Blocks all reads and writes on the table for the whole rewrite. |
| `ANALYZE` | SHARE UPDATE EXCLUSIVE | Reads and writes continue. |
| `CLUSTER` | ACCESS EXCLUSIVE | Same blocking profile as `VACUUM FULL`, plus index ordering. |

### PARALLEL details (v13+)

- Worker count = min(eligible indexes, `PARALLEL` value, `max_parallel_maintenance_workers`).
- An index must be larger than `min_parallel_index_scan_size` to participate.
- At least 2 indexes are required before any worker launches; one worker per index.
- Not guaranteed : the server may use fewer workers, or none.
- `PARALLEL` cannot be combined with `FULL`; it does not affect a combined `ANALYZE`.

## Autovacuum parameters

Trigger formulas :

```
vacuum threshold        = autovacuum_vacuum_threshold        + autovacuum_vacuum_scale_factor        * reltuples
vacuum insert threshold = autovacuum_vacuum_insert_threshold + autovacuum_vacuum_insert_scale_factor * reltuples
analyze threshold       = autovacuum_analyze_threshold       + autovacuum_analyze_scale_factor       * reltuples
```

`reltuples` is `pg_class.reltuples` (estimated live row count).

| Parameter | Default | Meaning |
|-----------|---------|---------|
| `autovacuum` | on | Master switch for the autovacuum launcher. |
| `autovacuum_vacuum_threshold` | 50 | Base dead-tuple count in the vacuum formula. |
| `autovacuum_vacuum_scale_factor` | 0.2 | Fraction of table size added to the vacuum threshold. |
| `autovacuum_vacuum_insert_threshold` | 1000 | Base inserted-tuple count for insert-driven vacuum (v13+). `-1` disables. |
| `autovacuum_vacuum_insert_scale_factor` | 0.2 | Fraction of table size for insert-driven vacuum (v13+). |
| `autovacuum_analyze_threshold` | 50 | Base modified-tuple count in the analyze formula. |
| `autovacuum_analyze_scale_factor` | 0.1 | Fraction of table size added to the analyze threshold. |
| `autovacuum_max_workers` | 3 | Maximum simultaneous autovacuum worker processes. |
| `autovacuum_naptime` | 1min | Interval between launcher runs per database. |
| `autovacuum_vacuum_cost_delay` | 2ms | Sleep injected between cost-limited work units (`-1` uses `vacuum_cost_delay`). |
| `autovacuum_vacuum_cost_limit` | -1 | Cost accumulated before sleeping (`-1` uses `vacuum_cost_limit`, default 200). |
| `vacuum_freeze_min_age` | 50000000 | Minimum XID age before a tuple is frozen during vacuum. |
| `vacuum_freeze_table_age` | 150000000 | XID age that triggers an aggressive (whole-table) vacuum. Capped at 0.95 * `autovacuum_freeze_max_age`. |
| `autovacuum_freeze_max_age` | 200000000 | XID age that forces an anti-wraparound autovacuum that cannot be skipped. |
| `vacuum_multixact_freeze_min_age` | 5000000 | Minimum multixact age before freezing. |
| `vacuum_multixact_freeze_table_age` | 150000000 | Multixact age that triggers an aggressive vacuum. |
| `autovacuum_multixact_freeze_max_age` | 400000000 | Multixact age that forces an anti-wraparound autovacuum. |
| `maintenance_work_mem` | 64MB | Memory per VACUUM / CREATE INDEX. Raising it speeds large vacuums. |
| `vacuum_buffer_usage_limit` | 256kB | Default ring-buffer size for VACUUM/ANALYZE Buffer Access Strategy (v16+). |

### Per-table storage parameters

Set with `ALTER TABLE t SET (param = value);` (or in `CREATE TABLE ... WITH`).
A per-table value overrides the global setting for that table only.

| Storage parameter | Notes |
|-------------------|-------|
| `autovacuum_enabled` | `false` disables autovacuum for the table. Anti-wraparound vacuums still run. |
| `autovacuum_vacuum_scale_factor` | Per-table override; lower it for large tables. |
| `autovacuum_vacuum_threshold` | Per-table override of the base count. |
| `autovacuum_vacuum_insert_scale_factor` / `_threshold` | Per-table insert-driven vacuum tuning. |
| `autovacuum_analyze_scale_factor` / `_threshold` | Per-table analyze tuning. |
| `autovacuum_freeze_min_age` / `_freeze_max_age` / `_freeze_table_age` | Per-table freeze tuning. |
| `autovacuum_vacuum_cost_delay` / `_cost_limit` | Per-table autovacuum throttle. |
| `fillfactor` | Percentage of a page filled by INSERTs (default 100). Lower it (80-90) to reserve space for HOT updates. Affects only pages written after the change. |

## Monitoring views and columns

### pg_stat_user_tables / pg_stat_all_tables

| Column | Meaning |
|--------|---------|
| `n_live_tup` | Estimated live rows. |
| `n_dead_tup` | Estimated dead rows awaiting vacuum (the bloat backlog). |
| `n_mod_since_analyze` | Rows modified since the last ANALYZE. |
| `n_ins_since_vacuum` | Rows inserted since the last VACUUM (v13+; drives insert-vacuum). |
| `n_tup_upd` | Total updated rows. |
| `n_tup_hot_upd` | Updates that used the HOT path (no new index entry). |
| `n_tup_newpage_upd` | Updates that moved the row to a new page (v16+). |
| `last_vacuum` / `last_autovacuum` | Timestamp of the last manual / auto VACUUM. |
| `last_analyze` / `last_autoanalyze` | Timestamp of the last manual / auto ANALYZE. |
| `vacuum_count` / `autovacuum_count` | Counters for VACUUM runs. |

### Wraparound and bloat functions / views

| Object | Use |
|--------|-----|
| `age(xid)` | Transactions between the given XID and the current XID. Apply to `relfrozenxid` / `datfrozenxid`. |
| `mxid_age(mxid)` | Multixact-age equivalent of `age()`. Apply to `relminmxid` / `datminmxid`. |
| `pg_class.relfrozenxid` / `relminmxid` | Per-table oldest unfrozen XID / multixact. |
| `pg_database.datfrozenxid` / `datminmxid` | Per-database oldest unfrozen XID / multixact. |
| `pg_stat_progress_vacuum` | Live progress of a running VACUUM (phase, heap blocks scanned). |
| `pgstattuple('t')` | Extension : exact `dead_tuple_percent`, `free_percent`, tuple counts. |
| `pgstattuple_approx('t')` | Extension : faster approximate bloat estimate, skips fully-visible pages. |
| `pg_relation_size('t')` / `pg_total_relation_size('t')` | On-disk size of the table / table + indexes + TOAST. |

### Command-line tools

| Tool | Use |
|------|-----|
| `vacuumdb --all --analyze` | Run VACUUM (and ANALYZE) across every database. |
| `vacuumdb --all --jobs N` | Parallel VACUUM across tables (v17 also supports `--missing-stats-only`). |
| `pg_repack -d db -t table` | Online table/index rewrite; brief ACCESS EXCLUSIVE only at the swap. Needs the `pg_repack` extension and a primary key or non-partial unique index. |

## Version notes

- v13+ : `autovacuum_vacuum_insert_threshold` / `_insert_scale_factor`,
  `VACUUM (PARALLEL n)`.
- v16+ : `VACUUM (BUFFER_USAGE_LIMIT ...)`, `vacuum_buffer_usage_limit`
  setting, `pg_stat_io`, `n_tup_newpage_upd` column.
- v17+ : VACUUM uses a TID store that removes the historical 1GB cap on
  dead-tuple tracking memory, so a higher `maintenance_work_mem` is fully
  used during large vacuums.

## Sources

- https://www.postgresql.org/docs/17/sql-vacuum.html
- https://www.postgresql.org/docs/17/routine-vacuuming.html
- https://www.postgresql.org/docs/17/storage-hot.html
- https://www.postgresql.org/docs/17/runtime-config-autovacuum.html
- https://www.postgresql.org/docs/17/runtime-config-resource.html
