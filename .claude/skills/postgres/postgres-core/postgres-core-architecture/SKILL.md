---
name: postgres-core-architecture
description: >
  Use when designing for PostgreSQL concurrency, understanding why a query sees old rows, or debugging "table is bloated" / "where did the rows go" symptoms.
  Prevents misunderstanding MVCC visibility, snapshot isolation, hint-bit first-read cost, and the transactional DDL guarantee.
  Covers MVCC tuple visibility, snapshot isolation, process model (postmaster, backends, autovacuum, walwriter, checkpointer), shared buffers, WAL durability, visibility map, hint bits, FSM, transactional DDL.
  Keywords: MVCC, snapshot isolation, xmin, xmax, ctid, hint bits, visibility map, WAL, shared_buffers, autovacuum, transactional DDL, table is bloated, query sees old data, where did the rows go, why is my select slow after insert, what is postgres doing
license: MIT
compatibility: "Designed for Claude Code. Requires PostgreSQL 15, 16, or 17."
metadata:
  author: OpenAEC-Foundation
  version: "1.0"
---

# postgres-core-architecture

## Quick Reference :

PostgreSQL is a multi-process (NOT multi-threaded) RDBMS that implements Multi-Version Concurrency Control. Every row version (tuple) carries `xmin` (inserting transaction) and `xmax` (deleting/updating transaction) header fields. Readers and writers never block each other because each statement (READ COMMITTED) or transaction (REPEATABLE READ / SERIALIZABLE) sees a consistent snapshot. UPDATE never overwrites a tuple in place : it inserts a new tuple version and marks the old one with `xmax`. DELETE only sets `xmax`. Reclamation of dead tuples is the job of VACUUM and autovacuum. The Write-Ahead Log is the single source of durability : WAL records flush before dirty data pages, so crash recovery replays WAL since the last checkpoint to rebuild any committed transaction. DDL is transactional (almost all of it) : `BEGIN; ALTER TABLE ... ; CREATE INDEX ... ; ROLLBACK;` rolls back the entire schema change. The one exception that matters daily is `CREATE INDEX CONCURRENTLY` and `REINDEX CONCURRENTLY`, which run outside any transaction.

The architectural consequences shape every design decision : `max_connections` is a hard limit because each connection is a forked OS process (use PgBouncer in transaction mode); `shared_buffers` should be ~25% of RAM (PostgreSQL also relies on the OS page cache, so do not push past 40%); `synchronous_commit` controls how many disk flushes happen before COMMIT returns; the visibility map enables index-only scans and is what makes "almost all" queries cheap on tables that have been VACUUMed; hint bits are set lazily on first read after INSERT/UPDATE/DELETE, which is why an otherwise-warm `SELECT` immediately after a bulk load can still produce write I/O.

## When To Use This Skill :

ALWAYS use this skill when :
- Designing concurrent write patterns and choosing an isolation level
- A user reports "the SELECT sees old rows" / "where did my updates go" / "the table is huge but `count(*)` is tiny"
- Tuning `shared_buffers`, `work_mem`, `wal_buffers`, `synchronous_commit`, or `max_connections`
- Reasoning about whether a DDL statement is safe inside a transaction
- Choosing between PostgreSQL built-in types (numeric, jsonb, array, range, uuid, enum, domain) : see `references/type-system.md`

NEVER use this skill for :
- Concrete index design : see `postgres-core-indexing-strategy`
- Concrete VACUUM tuning : see `postgres-impl-vacuum-autovacuum-bloat`
- Lock conflicts and deadlocks : see `postgres-errors-deadlocks-lock-waits`
- Zero-downtime DDL execution patterns : see `postgres-impl-zero-downtime-ddl`

## Decision Trees :

### Pick an isolation level :

```
Workload writes overlap on the same rows?
├── No : READ COMMITTED (default ; per-statement snapshot, no serialization failures)
└── Yes : Multi-statement transaction reads MUST stay consistent?
    ├── No : READ COMMITTED + explicit SELECT FOR UPDATE on rows you intend to write
    ├── Yes, snapshot-stable reads only : REPEATABLE READ (per-transaction snapshot;
    │   write conflict raises SQLSTATE 40001, retry the transaction)
    └── Yes, AND business invariants span multiple rows/tables :
        SERIALIZABLE (SSI predicate locking; expect 40001 under contention,
        retry loop is mandatory)
```

### Will this DDL block production traffic? :

```
Statement is one of : CREATE INDEX CONCURRENTLY,
REINDEX CONCURRENTLY, DETACH PARTITION CONCURRENTLY?
├── Yes : runs outside any transaction. Takes only SHARE UPDATE EXCLUSIVE.
│         Cannot be in BEGIN/COMMIT block. Failed builds leave INVALID indexes.
└── No : Statement rewrites the entire table?
    ├── Yes (ALTER COLUMN TYPE with non-binary-coercible cast,
    │         ADD COLUMN with volatile DEFAULT) :
    │         ACCESS EXCLUSIVE for full rewrite. NEVER online on large tables.
    └── No : metadata-only (ADD COLUMN nullable, ADD CONSTRAINT NOT VALID,
              DROP COLUMN, SET STATISTICS). ACCESS EXCLUSIVE for a moment.
              SAFE inside transaction. Set lock_timeout before running.
```

### Did the SELECT see stale rows? :

```
Same transaction reads same row twice, sees different values?
├── Isolation = READ COMMITTED : EXPECTED. Each statement gets a fresh snapshot.
│                                Switch to REPEATABLE READ for stable reads.
└── Isolation = REPEATABLE READ or SERIALIZABLE :
    Snapshot was taken at first statement of transaction;
    later writes by other transactions are invisible. EXPECTED.

SELECT after COMMIT in another session does not show new rows?
├── Connection is in middle of an open transaction (autocommit off, BEGIN
│   without COMMIT) : new snapshot only after COMMIT/ROLLBACK
├── Reading from physical standby : check replication lag
│   (pg_last_wal_replay_lsn vs primary pg_current_wal_lsn)
└── Replica conflict avoidance is delaying replay :
    hot_standby_feedback / max_standby_streaming_delay
```

### Why is the table on disk much larger than rows × row width? :

```
Bloat. Caused by one or more :
├── Heavy UPDATE traffic and HOT not kicking in :
│   indexed column was touched OR fillfactor too high.
│   Check pg_stat_all_tables.n_tup_hot_upd vs n_tup_upd.
├── Long-running transaction or replication slot pinning the xmin horizon :
│   VACUUM cannot reclaim dead tuples newer than the oldest live snapshot.
│   Check SELECT * FROM pg_stat_activity WHERE state IN
│   ('idle in transaction', 'active') ORDER BY xact_start; AND
│   pg_replication_slots.confirmed_flush_lsn.
├── Autovacuum starved : raise autovacuum workers / lower scale factor /
│   raise maintenance_work_mem.
└── True dead-tuple ratio confirmed via pgstattuple : online rewrite with
    pg_repack or pg_squeeze (NEVER VACUUM FULL on live system).
```

## Patterns :

### Pattern 1 : Inspect MVCC tuple visibility directly

ALWAYS read `xmin`, `xmax`, and `ctid` when reasoning about MVCC behavior.
NEVER assume tuple identity from primary key alone : the same logical row goes through many `ctid` values across its UPDATE history.

```sql
SELECT xmin, xmax, ctid, * FROM my_table WHERE id = 42;

-- After UPDATE in another session and COMMIT, re-read :
SELECT xmin, xmax, ctid, * FROM my_table WHERE id = 42;
-- ctid changes (or stays if HOT update on same page) ;
-- xmin shows the new inserting txid.

-- Live + dead tuples per relation :
SELECT relname, n_live_tup, n_dead_tup, n_tup_hot_upd, n_tup_upd
FROM pg_stat_user_tables
WHERE relname = 'my_table';
```

WHY : `xmin`/`xmax` are the ground truth for visibility decisions. The planner stats (`n_dead_tup`) lag and only update after autovacuum runs ANALYZE, but `ctid` movement and `xmin` are real-time. Source : postgresql.org/docs/17/mvcc.html, postgresql.org/docs/17/monitoring-stats.html.

### Pattern 2 : Force per-transaction snapshot for multi-statement read consistency

ALWAYS use `BEGIN ISOLATION LEVEL REPEATABLE READ` when a report or export must produce a single consistent view across multiple SELECTs.
NEVER mix `BEGIN` (default READ COMMITTED) with multiple SELECTs and expect them to be consistent : the second SELECT can see writes the first did not.

```sql
BEGIN ISOLATION LEVEL REPEATABLE READ;
  SELECT count(*) FROM orders WHERE created_at >= '2026-05-01';
  SELECT sum(total) FROM orders WHERE created_at >= '2026-05-01';
  SELECT count(*) FROM order_items oi
    JOIN orders o ON o.id = oi.order_id
    WHERE o.created_at >= '2026-05-01';
COMMIT;
```

WHY : REPEATABLE READ takes the snapshot at the first non-`BEGIN` query of the transaction and holds it until COMMIT/ROLLBACK. Concurrent INSERTs/UPDATEs commit but are invisible to this session. A concurrent write to a row this session WROTE will raise SQLSTATE 40001 `could_not_serialize`. Source : postgresql.org/docs/17/transaction-iso.html.

### Pattern 3 : Handle SERIALIZABLE serialization failures with a retry loop

ALWAYS wrap SERIALIZABLE transactions in a retry loop on SQLSTATE 40001.
NEVER raise 40001 to the end user : it is a planner-internal contention signal, not an application error.

```sql
-- Application pseudocode (psycopg, asyncpg, JDBC, etc.) :
-- for attempt in 1..5 :
--     try :
--         BEGIN ISOLATION LEVEL SERIALIZABLE;
--           ... reads + writes that depend on each other ...
--         COMMIT;
--         break
--     except SQLSTATE '40001' :
--         ROLLBACK;
--         sleep(backoff * 2^attempt);
```

WHY : SERIALIZABLE uses Serializable Snapshot Isolation. Predicate locks detect read/write dependency cycles that would violate serial-equivalence and abort one of the participants. Retrying succeeds in almost all cases because by then the conflict has resolved. Source : postgresql.org/docs/17/transaction-iso.html (Serializable Isolation Level), postgresql.org/docs/17/errcodes-appendix.html.

### Pattern 4 : Tune the durability/throughput tradeoff via synchronous_commit

ALWAYS pick `synchronous_commit` per-transaction with `SET LOCAL` for cases where the durability requirement differs from the database default.
NEVER set `synchronous_commit = off` globally on a system with any real durability requirement.

```sql
-- Default (per-database setting) : synchronous_commit = on
-- For a bulk import where the WHOLE import will be retried on crash :
BEGIN;
  SET LOCAL synchronous_commit = off;
  COPY events FROM '/tmp/events.csv' (FORMAT csv);
  INSERT INTO event_summary SELECT date_trunc('hour', ts), count(*)
    FROM events GROUP BY 1;
COMMIT;
-- The COMMIT returns before WAL is flushed. On crash, this transaction
-- may be lost ; the rest of the database is consistent.
```

| `synchronous_commit` | What waits before COMMIT returns | Crash risk |
|---|---|---|
| `off` | WAL buffer write only | This transaction may vanish |
| `local` | Primary fsync to disk | Survives primary crash |
| `remote_write` | Standby received + OS-wrote WAL | Survives standby OS crash |
| `on` | Defaults to `local` unless a sync standby exists, then `remote_write` | -- |
| `remote_apply` | Standby has replayed the WAL record | Transaction visible on standby at return |

WHY : `synchronous_commit` is the single dial that trades durability for latency in PostgreSQL. The WAL writer flushes continuously in the background regardless ; this setting controls how long the COMMIT waits. Source : postgresql.org/docs/17/wal-async.html, postgresql.org/docs/17/runtime-config-wal.html.

### Pattern 5 : Use transactional DDL for safe schema migrations

ALWAYS group dependent schema changes in a single transaction so they are atomic.
NEVER mix `CREATE INDEX CONCURRENTLY` into the same transaction as other DDL : it must run outside any transaction.

```sql
BEGIN;
  ALTER TABLE orders ADD COLUMN region text;
  ALTER TABLE orders ADD CONSTRAINT orders_region_chk
    CHECK (region IN ('eu','us','ap')) NOT VALID;
  CREATE INDEX orders_region_idx_btree ON orders (region);
  -- if any of the above fails, all roll back atomically
COMMIT;

-- CREATE INDEX CONCURRENTLY runs OUTSIDE any transaction :
CREATE INDEX CONCURRENTLY orders_created_at_idx ON orders (created_at);
-- If this fails, the index is marked INVALID :
--   SELECT indexrelid::regclass, indisvalid FROM pg_index
--   WHERE NOT indisvalid;
-- Clean up with DROP INDEX before retrying.
```

WHY : transactional DDL is one of PostgreSQL's strongest operational guarantees ; almost every DDL statement obeys it. The exceptions are `CREATE INDEX CONCURRENTLY`, `REINDEX CONCURRENTLY`, `DROP INDEX CONCURRENTLY`, `DETACH PARTITION ... CONCURRENTLY`, `ALTER TYPE ... ADD VALUE` (enum value addition), `ALTER SYSTEM`, `VACUUM`, and `CREATE DATABASE`. Source : postgresql.org/docs/17/sql-createindex.html, postgresql.org/docs/17/sql-altertable.html.

### Pattern 6 : Warm hint bits after bulk load before serving production reads

ALWAYS run `VACUUM ANALYZE` (or at least `ANALYZE`) plus a touch-each-page read pass after bulk-loading large tables.
NEVER cut over read traffic to a freshly-loaded table without warming, or the first `SELECT` per page will do write I/O setting hint bits.

```sql
COPY events FROM '/data/events.csv' (FORMAT csv, FREEZE);
-- FREEZE (with TRUNCATE in same transaction) avoids hint-bit work entirely.

-- Without FREEZE, force hint-bit warming :
VACUUM (ANALYZE) events;
-- VACUUM sets hint bits and the visibility map in one pass.

-- Or, if VACUUM is impractical, read every page :
SELECT count(*) FROM events;
```

WHY : every tuple header has an `HEAP_XMIN_COMMITTED` / `HEAP_XMIN_INVALID` / `HEAP_XMAX_COMMITTED` / `HEAP_XMAX_INVALID` hint bit set lazily on first access. Setting these bits dirties the page, which then needs to be written to disk. A SELECT after a bulk INSERT therefore generates writes proportional to data size. `VACUUM` (and `COPY ... FREEZE` for fresh tables) sets the bits in bulk. Source : postgresql.org/docs/17/storage-page-layout.html, postgresql.org/docs/17/routine-vacuuming.html.

## Anti-Patterns :

(One-liners ; full diagnosis + fix in `references/anti-patterns.md`.)

- Long-running idle-in-transaction sessions blocking VACUUM : pin the xmin horizon, prevent dead-tuple reclamation. Symptom : `pg_stat_user_tables.n_dead_tup` keeps growing.
- `VACUUM FULL` on a live system : `ACCESS EXCLUSIVE` for the full rewrite. Use `pg_repack` instead.
- Pushing `shared_buffers` past 40% of RAM : double-buffering with OS page cache hurts more than it helps.
- `synchronous_commit = off` globally on a database with real durability : silently loses recent committed transactions on crash.
- Catching SQLSTATE 40001 as a generic error and surfacing it : it is a retry signal, not a failure.
- Treating `xmin`/`xmax` as transaction timestamps : they are 32-bit XIDs, wrap every ~2 billion, and the visible age depends on `relfrozenxid`.
- Mixing `CREATE INDEX CONCURRENTLY` into a `BEGIN/COMMIT` block : raises an error, then leaves work uncertain.
- Trusting `n_live_tup` / `n_dead_tup` immediately after a write burst : these are statistics, updated only by ANALYZE / autovacuum.

## Reference Links :

- [references/methods.md](references/methods.md) : Full parameter tables (shared_buffers, wal_*, synchronous_commit modes, isolation levels), process-model reference, GUC categories
- [references/examples.md](references/examples.md) : Verified MVCC / isolation / WAL / DDL transaction examples with expected output
- [references/anti-patterns.md](references/anti-patterns.md) : Architecture-level anti-patterns with cause, symptom, SQLSTATE (where applicable), and fix pattern
- [references/type-system.md](references/type-system.md) : Built-in types, casting rules, custom types (composite, enum, domain). Absorbed from the dropped types-domains skill per decision D-010.

## See Also :

- `postgres-core-version-matrix` : version-specific behavior across v15 / v16 / v17
- `postgres-core-indexing-strategy` : index access methods, partial / expression / multicolumn
- `postgres-impl-vacuum-autovacuum-bloat` : routine vacuuming, wraparound, autovacuum tuning
- `postgres-impl-zero-downtime-ddl` : multi-step migration patterns, NOT VALID + VALIDATE
- `postgres-errors-deadlocks-lock-waits` : lock-conflict resolution
- `postgres-errors-serialization-failures` : SERIALIZABLE / REPEATABLE READ retry patterns
- Vooronderzoek section : §1 (Architecture + Runtime Model), §13 (Security Model), §19 (Anti-Patterns)
- Official docs : https://www.postgresql.org/docs/17/mvcc.html, https://www.postgresql.org/docs/17/wal-intro.html, https://www.postgresql.org/docs/17/transaction-iso.html
