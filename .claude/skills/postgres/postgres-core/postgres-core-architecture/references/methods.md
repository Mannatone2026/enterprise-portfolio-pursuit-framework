# Methods : Core Architecture Reference

Full parameter tables, process-model reference, and GUC categories used in `postgres-core-architecture`.

## Process Model

| Process | Started by | Role | Lifecycle |
|---|---|---|---|
| `postmaster` (supervisor) | `pg_ctl start` | Listens for connections, forks backends, restarts crashed children | Cluster lifetime |
| `backend` (per-connection) | postmaster fork on connect | Executes SQL for one client | Connection lifetime |
| `walwriter` | postmaster | Periodically flushes WAL buffers | Cluster lifetime |
| `bgwriter` | postmaster | Trickle-writes dirty shared buffers to data files | Cluster lifetime |
| `checkpointer` | postmaster | Performs full checkpoints on `checkpoint_timeout` / `max_wal_size` | Cluster lifetime |
| `autovacuum launcher` | postmaster | Schedules autovacuum workers | Cluster lifetime when `autovacuum = on` |
| `autovacuum worker` | autovacuum launcher | Runs VACUUM + ANALYZE on selected relations | Per-relation, short-lived |
| `logical replication launcher` | postmaster | Schedules subscription apply workers | Cluster lifetime when subscriptions exist |
| `walsender` | postmaster fork on replication connect | Streams WAL to a physical / logical replica | Replication-connection lifetime |
| `walreceiver` | startup process on standby | Receives WAL from primary, writes to local WAL | Standby lifetime |
| `archiver` | postmaster | Calls `archive_command` for completed WAL segments | Cluster lifetime when `archive_mode = on` |
| `WAL summarizer` (v17+) | postmaster | Maintains block-change summaries for incremental backups | Cluster lifetime when `summarize_wal = on` |
| `slotsync worker` (v17+) | postmaster | Synchronizes logical replication slots to standby for failover | Cluster lifetime when sync slots configured |

Source : postgresql.org/docs/17/admin.html overview; postgresql.org/docs/17/wal-configuration.html.

## Tuple Header Fields (MVCC)

Every heap tuple stores these MVCC-related fields. (Names below are from `src/include/access/htup_details.h` and exposed via `pg_attribute`'s system columns.)

| Field | Type | Set when | Meaning |
|---|---|---|---|
| `xmin` | uint32 (XID) | INSERT | Inserting transaction ID |
| `xmax` | uint32 (XID) | UPDATE or DELETE | Deleting transaction ID (0 if still live) |
| `cmin` / `cmax` | uint32 (CID) | Same transaction with multiple commands | Command ID within a transaction |
| `ctid` | (block, offset) | Every tuple | Physical location ; new tuple's ctid replaces predecessor's `t_ctid` pointer |
| `t_infomask` | uint16 bitmask | Continuously | Hint bits : HEAP_XMIN_COMMITTED, HEAP_XMIN_INVALID, HEAP_XMAX_COMMITTED, HEAP_XMAX_INVALID, HEAP_HASNULL, etc. |
| `t_infomask2` | uint16 bitmask | Continuously | HEAP_HOT_UPDATED, HEAP_ONLY_TUPLE, attribute count |

Read these as system columns in any query :

```sql
SELECT xmin, xmax, cmin, cmax, ctid, * FROM my_table;
```

Source : postgresql.org/docs/17/ddl-system-columns.html, postgresql.org/docs/17/storage-page-layout.html.

## Snapshot Isolation Levels

PostgreSQL implements three of the four SQL-standard levels (READ UNCOMMITTED is silently promoted to READ COMMITTED).

| Level | Snapshot scope | Prevents | Can raise 40001? | Use case |
|---|---|---|---|---|
| READ UNCOMMITTED | Per-statement (same as READ COMMITTED) | Dirty reads | No | (no separate behavior) |
| READ COMMITTED (default) | Per-statement | Dirty reads | No | OLTP single-statement transactions |
| REPEATABLE READ | Per-transaction | Dirty reads, non-repeatable reads, phantom reads | Yes (on write conflict) | Multi-statement reports needing consistency |
| SERIALIZABLE | Per-transaction + predicate locking (SSI) | All anomalies : serial-equivalent | Yes (on dependency cycle) | Business invariants that span multiple rows / tables |

Set per-session : `SET TRANSACTION ISOLATION LEVEL REPEATABLE READ`
Set per-transaction : `BEGIN ISOLATION LEVEL SERIALIZABLE`
Set default : `default_transaction_isolation = 'repeatable read'` in postgresql.conf

Source : postgresql.org/docs/17/transaction-iso.html, postgresql.org/docs/17/sql-set-transaction.html.

## Memory GUCs (Resource Consumption)

| GUC | Default | Recommended | Scope | Notes |
|---|---|---|---|---|
| `shared_buffers` | 128MB | ~25% of system RAM, max 40% | Server start | Shared across all backends ; the buffer cache |
| `work_mem` | 4MB | 4-64MB | Per session / per node | Used per sort / hash node ; total = work_mem × concurrent_ops × sessions |
| `hash_mem_multiplier` | 2.0 (v15+) | 2.0 | Per session | Multiplier applied to work_mem for hash-based nodes |
| `maintenance_work_mem` | 64MB | 256MB - 2GB | Per session / per autovacuum worker | VACUUM, CREATE INDEX, ALTER TABLE ADD FK ; v17 removed the 1GB cap for VACUUM dead-tuple memory |
| `autovacuum_work_mem` | -1 (fall back to maintenance_work_mem) | maintenance_work_mem | Per autovacuum worker | Distinct cap for autovacuum workers |
| `effective_cache_size` | 4GB | RAM minus `shared_buffers` | Per session | Planner hint only ; allocates nothing |
| `temp_buffers` | 8MB | 8-128MB | Per session | Per-backend temp-table memory |
| `wal_buffers` | -1 (1/32 of shared_buffers, capped at WAL segment size) | -1 (auto) | Server start | Shared WAL buffer cache |
| `io_combine_limit` (v17+) | 128kB | 128kB | Per session | Maximum filesystem read batch size for streaming I/O subsystem |

Source : postgresql.org/docs/17/runtime-config-resource.html.

## WAL + Durability GUCs

| GUC | Default | Effect | Notes |
|---|---|---|---|
| `wal_level` | `replica` | `minimal`, `replica`, or `logical` | `logical` required for logical replication / pgoutput |
| `fsync` | `on` | Force fsync of WAL writes | NEVER set off in production |
| `synchronous_commit` | `on` | When COMMIT returns : `off` / `local` / `remote_write` / `on` / `remote_apply` | See SKILL.md Pattern 4 table |
| `synchronous_standby_names` | (empty) | Define sync replicas : `'FIRST 2 (s1,s2,s3)'` or `'ANY 2 (s1,s2,s3)'` | Determines what `on` and `remote_*` wait for |
| `wal_sync_method` | OS-dependent | `fdatasync`, `fsync`, `open_sync`, `open_datasync` | `pg_test_fsync` benchmarks each |
| `checkpoint_timeout` | 5min | Max time between checkpoints | 15-30min reduces I/O at cost of recovery time |
| `max_wal_size` | 1GB | Soft cap on WAL between checkpoints | Raise to 4-8GB on write-heavy systems |
| `min_wal_size` | 80MB | Recycle WAL down to this size | Avoids constant fsync-allocate cycles |
| `archive_mode` | `off` | `on` / `always` (always = archive on standby too) | Required for PITR |
| `archive_command` / `archive_library` | empty | Shell command / library called per WAL segment | `archive_library` (v15+) preferred when available |
| `summarize_wal` (v17+) | `off` | Enables WAL summarizer for incremental backups | Required for `pg_basebackup --incremental` |

Source : postgresql.org/docs/17/runtime-config-wal.html, postgresql.org/docs/17/wal-intro.html.

## Auxiliary Forks (Per-Relation)

Every relation has up to three extra forks alongside the main heap or index data :

| Fork | Suffix | Maintained by | Purpose |
|---|---|---|---|
| `main` | (none) | Backends + VACUUM | Heap pages or index pages |
| `fsm` (Free Space Map) | `_fsm` | Backends + VACUUM | Per-page free-space estimate used to pick INSERT/UPDATE targets |
| `vm` (Visibility Map) | `_vm` | VACUUM | Two bits per page : `all-visible` (no per-tuple visibility check needed) and `all-frozen` (skipped by anti-wraparound VACUUM). Powers index-only scans. |
| `init` (init fork) | `_init` | DDL | Unlogged-relation init image (re-applied on crash) |

Locate the files :

```sql
SELECT pg_relation_filepath('my_table');                -- main fork
SELECT pg_relation_filepath('my_table') || '_vm';       -- visibility map fork
SELECT pg_relation_filepath('my_table') || '_fsm';      -- free space map fork
```

Source : postgresql.org/docs/17/storage-vm.html, postgresql.org/docs/17/storage-fsm.html.

## Index-Only Scan Eligibility

An index-only scan is possible when ALL of these hold :

1. The query references only columns that are stored in the index (covered by indexed columns or `INCLUDE` columns).
2. The visibility map for the heap pages backing the matching index entries is `all-visible`.
3. The planner thinks the visibility-map hit rate is high enough that probing heap will be rare.

Symptoms when index-only scan should fire but does not :

- Recent writes : pages are not `all-visible` yet. Run `VACUUM` or wait for autovacuum.
- `EXPLAIN ANALYZE` shows `Heap Fetches: N` with non-zero N : visibility map missed for those rows.

Source : postgresql.org/docs/17/indexes-index-only-scans.html, postgresql.org/docs/17/storage-vm.html.

## Transactional DDL : What Is And Is Not Inside a Transaction

| Statement | Inside BEGIN/COMMIT? | Notes |
|---|---|---|
| `CREATE TABLE` / `ALTER TABLE` / `DROP TABLE` | Yes | All metadata changes roll back |
| `CREATE INDEX` (non-concurrent) | Yes | Holds ACCESS EXCLUSIVE for full build |
| `CREATE INDEX CONCURRENTLY` | NO | Runs in its own transaction(s). Must be top-level. Failed build → INVALID index. |
| `REINDEX CONCURRENTLY` (v12+) | NO | Same restriction |
| `DROP INDEX CONCURRENTLY` | NO | Same restriction |
| `DETACH PARTITION ... CONCURRENTLY` (v14+) | NO | Two-step internally, cannot be in transaction |
| `CREATE DATABASE` / `DROP DATABASE` | NO | Operates outside any transaction |
| `VACUUM` / `VACUUM FULL` | NO | Outside transaction |
| `ALTER SYSTEM SET ...` | Yes but not transactional | Edits `postgresql.auto.conf` immediately, not rolled back |
| `ALTER TYPE ... ADD VALUE` (enum) | NO (since v12) | Cannot be rolled back |
| `CLUSTER` | Yes (per-table form) | NO (`CLUSTER` without args) |
| `CREATE EXTENSION` / `DROP EXTENSION` | Yes | Most extensions ; some scripts may issue non-transactional ops |
| `CREATE PUBLICATION` / `CREATE SUBSCRIPTION` | Yes (but subscription may have `WITH (create_slot = true)` which makes a non-rollbackable slot) | Care needed |
| `CREATE ROLE` / `ALTER ROLE` / `DROP ROLE` | Yes | Globally visible after COMMIT |
| `GRANT` / `REVOKE` | Yes | Rollback restores prior ACL |

Source : postgresql.org/docs/17/sql-createindex.html, postgresql.org/docs/17/sql-altertable.html, postgresql.org/docs/17/sql-altertype.html.

## Inspection Queries

```sql
-- Current process tree (excluding self) :
SELECT pid, backend_type, state, query_start, wait_event_type, wait_event
FROM pg_stat_activity
WHERE pid <> pg_backend_pid()
ORDER BY backend_type, pid;

-- Active backend types break-down :
SELECT backend_type, count(*)
FROM pg_stat_activity
GROUP BY backend_type
ORDER BY 2 DESC;

-- Oldest snapshot pinning the xmin horizon :
SELECT pid, backend_type, state, xact_start, query_start,
       backend_xmin, query
FROM pg_stat_activity
WHERE backend_xmin IS NOT NULL
ORDER BY backend_xmin
LIMIT 5;

-- WAL position + bytes-since-last-checkpoint :
SELECT pg_current_wal_lsn() AS now,
       pg_walfile_name(pg_current_wal_lsn()) AS wal_file;

-- Per-database transaction-ID age (wraparound risk) :
SELECT datname, age(datfrozenxid) AS xid_age
FROM pg_database
ORDER BY age(datfrozenxid) DESC;

-- Buffer cache contents per relation :
-- Requires CREATE EXTENSION pg_buffercache;
SELECT c.relname,
       count(*) AS buffers,
       round(100.0 * count(*) / (SELECT setting::int FROM pg_settings
                                  WHERE name = 'shared_buffers'), 1) AS pct
FROM pg_buffercache b
JOIN pg_class c ON c.relfilenode = b.relfilenode
GROUP BY c.relname
ORDER BY 2 DESC
LIMIT 20;
```

Source : postgresql.org/docs/17/monitoring-stats.html (pg_stat_activity, pg_stat_io, pg_buffercache).
