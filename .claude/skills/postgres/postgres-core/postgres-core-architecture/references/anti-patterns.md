# Anti-Patterns : Core Architecture

Architecture-level anti-patterns with cause, symptom, SQLSTATE (when applicable), and fix pattern. Sources are official PostgreSQL 17 docs unless noted.

---

## AP-1 : Long-running idle-in-transaction sessions

**Symptom** : `pg_stat_user_tables.n_dead_tup` keeps growing on hot tables. Autovacuum runs but reclaims nothing. Bloat builds steadily. `pg_class.relpages` >> expected pages from rows.

**Cause** : A backend has an open transaction (state = `idle in transaction`). Its snapshot pins the global xmin horizon. VACUUM cannot reclaim any tuple whose `xmax` is newer than that snapshot, even though the deleting transaction has committed.

**SQLSTATE** : not an error path : silent space leak.

**Fix pattern** :

```sql
-- Detect
SELECT pid, usename, state, xact_start,
       now() - xact_start AS xact_age,
       backend_xmin, query
FROM pg_stat_activity
WHERE state IN ('idle in transaction', 'idle in transaction (aborted)')
  AND xact_start < now() - interval '5 minutes';

-- Prevent (postgresql.conf or ALTER SYSTEM)
ALTER SYSTEM SET idle_in_transaction_session_timeout = '30s';
SELECT pg_reload_conf();

-- Surgical cleanup
SELECT pg_cancel_backend(pid)
FROM pg_stat_activity
WHERE state IN ('idle in transaction', 'idle in transaction (aborted)')
  AND xact_start < now() - interval '1 hour';
```

Source : postgresql.org/docs/17/runtime-config-client.html (`idle_in_transaction_session_timeout`), postgresql.org/docs/17/routine-vacuuming.html.

---

## AP-2 : VACUUM FULL on a live system

**Symptom** : application stalls completely on a table during maintenance. Replicas lag. Connections pile up against `max_connections`.

**Cause** : `VACUUM FULL` takes `ACCESS EXCLUSIVE` for the entire rewrite. It blocks every read and write on the table : including `SELECT 1 FROM t LIMIT 1`. The rewrite duration scales with table size, NOT with bloat.

**SQLSTATE** : Other sessions see `55P03` lock_not_available with `lock_timeout`, otherwise they queue.

**Fix pattern** : NEVER run `VACUUM FULL` on tables that production traffic touches.

```sql
-- Use pg_repack for online table rewrite
CREATE EXTENSION pg_repack;  -- (requires shared_preload_libraries setting)
-- From shell :
-- pg_repack -d mydb -t public.orders --jobs=4

-- Or pg_squeeze for trigger-based online compact
-- Or accept the downtime and schedule VACUUM FULL in a maintenance window
```

Source : postgresql.org/docs/17/sql-vacuum.html.

---

## AP-3 : Pushing shared_buffers past 40% of system RAM

**Symptom** : I/O numbers look bad despite plenty of free memory. `pg_stat_io` shows high read counts for blocks that should be cached. Performance degrades after a warmup that should have helped.

**Cause** : PostgreSQL's buffer cache and the OS page cache double-buffer the same data. Every page read enters both. Pushing `shared_buffers` too high crowds out the OS page cache, hurts dirty-page write back, and causes contention on the buffer-mapping locks.

**SQLSTATE** : not an error path : silent performance regression.

**Fix pattern** :

```ini
# postgresql.conf
shared_buffers = 8GB           # 25% of 32GB RAM
effective_cache_size = 24GB    # planner hint : remaining RAM
```

For very large machines (>128GB RAM), benchmark before going above 16-32GB ; gains plateau quickly.

Source : postgresql.org/docs/17/runtime-config-resource.html.

---

## AP-4 : synchronous_commit = off globally on a database with real durability

**Symptom** : after an unclean shutdown (kernel panic, kill -9 on postmaster, power loss), the database starts cleanly but recent committed transactions are missing.

**Cause** : `synchronous_commit = off` lets COMMIT return as soon as WAL is in the buffer, before fsync. On crash, WAL up to the last walwriter flush survives ; transactions in between are lost.

**SQLSTATE** : not an error path : silent data loss bounded by `wal_writer_delay` (default 200ms).

**Fix pattern** : keep `synchronous_commit = on` as the database default and lower it only PER-TRANSACTION for non-critical work.

```sql
-- ALWAYS leave default high
ALTER DATABASE app SET synchronous_commit = on;

-- LOWER per-transaction for bulk / retryable work
BEGIN;
  SET LOCAL synchronous_commit = off;
  COPY events FROM '/data/events.csv' (FORMAT csv);
COMMIT;
```

Source : postgresql.org/docs/17/wal-async.html, postgresql.org/docs/17/runtime-config-wal.html.

---

## AP-5 : Catching SQLSTATE 40001 and surfacing it to the user

**Symptom** : random transaction failures under load surface as application errors. Users see "please retry" or 500-class responses.

**Cause** : SQLSTATE `40001` (`serialization_failure`) and `40P01` (`deadlock_detected`) are PostgreSQL's contention signals at REPEATABLE READ / SERIALIZABLE / locking-conflict paths. They are designed as retry signals, not failures.

**SQLSTATE** : `40001` (serialization_failure), `40P01` (deadlock_detected).

**Fix pattern** : every transaction at REPEATABLE READ or higher MUST sit inside an application-level retry loop. Same for any code path that takes locks.

```python
# Python / psycopg
import psycopg, time

def retry_serializable(work_fn, max_attempts=5):
    for attempt in range(max_attempts):
        try:
            with psycopg.connect(DSN, autocommit=False) as cn:
                cn.execute("SET TRANSACTION ISOLATION LEVEL SERIALIZABLE")
                work_fn(cn)
                cn.commit()
            return
        except psycopg.errors.SerializationFailure:
            time.sleep(0.02 * (2 ** attempt))
        except psycopg.errors.DeadlockDetected:
            time.sleep(0.02 * (2 ** attempt))
    raise RuntimeError("transaction failed after retries")
```

Source : postgresql.org/docs/17/transaction-iso.html, postgresql.org/docs/17/errcodes-appendix.html.

---

## AP-6 : Treating xmin/xmax as transaction timestamps

**Symptom** : application reasons about row age from `xmin`, gets misordered or absurd results after the database has been up for months.

**Cause** : `xmin` and `xmax` are 32-bit XIDs that wrap roughly every 2 billion transactions. PostgreSQL freezes old tuples (marks `xmin` as "always older than any live snapshot") via VACUUM, which means `xmin` of a frozen row appears as `FrozenTransactionId` (= 2) regardless of when the row was actually written.

**SQLSTATE** : not an error path : silent ordering bug.

**Fix pattern** : if you need creation time, store an explicit column.

```sql
CREATE TABLE events (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  created_at timestamptz NOT NULL DEFAULT now(),
  ...
);
CREATE INDEX events_created_at_idx ON events (created_at);
```

Source : postgresql.org/docs/17/routine-vacuuming.html (transaction-id wraparound).

---

## AP-7 : Mixing CREATE INDEX CONCURRENTLY into a BEGIN/COMMIT block

**Symptom** : migration script errors with `CREATE INDEX CONCURRENTLY cannot run inside a transaction block`. Other statements that were supposed to be part of the same atomic change are not applied.

**Cause** : concurrent index build performs multiple internal transactions to acquire its weaker locks. It cannot be wrapped in an outer transaction.

**SQLSTATE** : `25001` (`active_sql_transaction`).

**Fix pattern** : split the migration into one transaction for transactional DDL plus separate top-level statements for each `CONCURRENTLY` operation.

```sql
-- Transaction 1 (transactional DDL)
BEGIN;
  ALTER TABLE orders ADD COLUMN region text;
  ALTER TABLE orders
    ADD CONSTRAINT orders_region_chk CHECK (region IN ('eu','us','ap')) NOT VALID;
COMMIT;

-- Statement 2 (no transaction)
SET lock_timeout = '5s';
CREATE INDEX CONCURRENTLY orders_region_idx ON orders (region);

-- Statement 3 (no transaction)
ALTER TABLE orders VALIDATE CONSTRAINT orders_region_chk;
```

Source : postgresql.org/docs/17/sql-createindex.html.

---

## AP-8 : Trusting n_live_tup / n_dead_tup immediately after a write burst

**Symptom** : just-loaded data is invisible to `pg_stat_user_tables`. Autovacuum thresholds appear to not fire. Application monitoring reports impossible row counts.

**Cause** : `pg_stat_user_tables` columns are statistics, updated by autovacuum's ANALYZE pass. They are not real-time. The actual page count (`pg_class.relpages`) and tuple count (`pg_class.reltuples`) update only on VACUUM / ANALYZE.

**SQLSTATE** : not an error path : silent staleness.

**Fix pattern** : trigger ANALYZE after large writes that downstream queries depend on, and never use `n_live_tup` as a count of rows. Use `count(*)`.

```sql
COPY events FROM '/data/events.csv' (FORMAT csv);
VACUUM ANALYZE events;  -- updates planner stats AND visibility map AND hint bits

SELECT count(*) FROM events;  -- authoritative
SELECT n_live_tup FROM pg_stat_user_tables WHERE relname = 'events';  -- statistic
```

Source : postgresql.org/docs/17/monitoring-stats.html, postgresql.org/docs/17/sql-analyze.html.

---

## AP-9 : Replication slot leak pinning xmin and growing WAL

**Symptom** : `pg_wal` directory grows without bound. Standby is offline. Autovacuum on the primary cannot reclaim dead tuples ; bloat accumulates.

**Cause** : a logical or physical replication slot whose consumer has disconnected. The slot retains its `restart_lsn` ; the primary cannot recycle WAL past that LSN. Logical slots also pin `catalog_xmin`, blocking VACUUM of system catalogs.

**SQLSTATE** : not an error path : silent disk growth.

**Fix pattern** : monitor slot lag, drop slots whose consumer has gone away.

```sql
-- Diagnose
SELECT slot_name, slot_type, active, restart_lsn, confirmed_flush_lsn,
       pg_size_pretty(
         pg_wal_lsn_diff(pg_current_wal_lsn(), confirmed_flush_lsn)
       ) AS lag_bytes
FROM pg_replication_slots
ORDER BY lag_bytes DESC NULLS LAST;

-- Drop a dead slot (DESTRUCTIVE : verify the standby is gone)
SELECT pg_drop_replication_slot('dead_standby_slot');

-- Prevent on primary :
ALTER SYSTEM SET max_slot_wal_keep_size = '64GB';  -- v13+
SELECT pg_reload_conf();
```

Source : postgresql.org/docs/17/warm-standby.html, postgresql.org/docs/17/view-pg-replication-slots.html.

---

## AP-10 : Assuming all DDL is transactional

**Symptom** : a migration script fails mid-way and leaves the database in a half-migrated state. Rollback does not restore the prior schema.

**Cause** : a handful of DDL statements are non-transactional or partially transactional :
- `CREATE / DROP / REINDEX INDEX CONCURRENTLY` : own transactions
- `DETACH PARTITION ... CONCURRENTLY` : own transactions
- `VACUUM`, `VACUUM FULL`, `CLUSTER` (without args) : outside transactions
- `ALTER TYPE ... ADD VALUE` : committed immediately since v12
- `ALTER SYSTEM SET` : persists immediately, NOT rolled back

**SQLSTATE** : `25001` for the CONCURRENTLY family ; others silent.

**Fix pattern** : structure migrations as a sequence : transactional batch → non-transactional statements → next transactional batch. Verify at each boundary.

```
[BEGIN; ALTER TABLE...; ALTER TABLE...; COMMIT;]
[CREATE INDEX CONCURRENTLY ...]
[BEGIN; ALTER TABLE ... VALIDATE CONSTRAINT ...; COMMIT;]
```

Source : postgresql.org/docs/17/sql-createindex.html, postgresql.org/docs/17/sql-altertype.html.

---

## AP-11 : Setting fsync = off in production

**Symptom** : a single power loss or kernel panic corrupts the database. PostgreSQL refuses to start.

**Cause** : `fsync = off` removes durability guarantees entirely. Even normal WAL handling assumes the OS will flush eventually ; on crash, the database can become physically corrupted in unrecoverable ways.

**SQLSTATE** : crash recovery may report `XX001` (`data_corrupted`).

**Fix pattern** : `fsync = on` always. For temporary "load test" scenarios, use a throw-away copy on a separate cluster.

Source : postgresql.org/docs/17/runtime-config-wal.html (fsync).

---

## AP-12 : Per-page checksums disabled then enabled requires rewrite

**Symptom** : enabling data checksums on an existing cluster appears to do nothing for old data. New bugs detected on new data only.

**Cause** : `initdb --data-checksums` is set at cluster initialization. v12+ added `pg_checksums --enable` but it requires the cluster to be offline and rewrites every page.

**SQLSTATE** : not an error path : silent partial protection.

**Fix pattern** : enable at `initdb` time on new clusters. For existing clusters, schedule a downtime window and run `pg_checksums --enable` against a stopped cluster.

```bash
# On stopped cluster :
pg_checksums --enable -D /var/lib/postgresql/17/main
# Hours for large clusters ; verify completion before restart.
```

Source : postgresql.org/docs/17/app-pgchecksums.html, postgresql.org/docs/17/checksums.html.
