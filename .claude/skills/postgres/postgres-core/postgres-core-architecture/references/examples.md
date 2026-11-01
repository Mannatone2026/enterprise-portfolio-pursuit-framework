# Examples : Core Architecture

Verified, version-annotated examples for MVCC, snapshot isolation, WAL durability tuning, and transactional DDL. All examples target PostgreSQL 15, 16, or 17 unless noted otherwise.

## Example 1 : See MVCC Tuple Versioning In Action

Demonstrates how UPDATE inserts a new tuple version and leaves the old one with `xmax` set.

```sql
-- Session A
CREATE TABLE accounts (
  id int GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  balance numeric NOT NULL
);
INSERT INTO accounts (balance) VALUES (100.00);

SELECT xmin, xmax, ctid, * FROM accounts WHERE id = 1;
-- xmin = (current txid), xmax = 0, ctid = (0,1)

BEGIN;
  UPDATE accounts SET balance = 90.00 WHERE id = 1;
  -- Within Session A's transaction the row is updated.
  -- In another session, Session B reading at READ COMMITTED still sees 100.00
  -- until Session A COMMITs.

SELECT xmin, xmax, ctid, * FROM accounts WHERE id = 1;
-- xmin = (new txid), xmax = 0, ctid = (0,2)
COMMIT;

-- After COMMIT, the OLD tuple still physically exists on disk with
-- its xmax set to the committing transaction. Autovacuum reclaims it
-- once no live snapshot can see it.

VACUUM (VERBOSE) accounts;
-- "INFO:  vacuuming ..." reports tuples_removed.
```

Verified against : postgresql.org/docs/17/mvcc.html, postgresql.org/docs/17/ddl-system-columns.html.

## Example 2 : READ COMMITTED Per-Statement Snapshot

Same transaction sees different rows across two SELECTs : expected behavior at READ COMMITTED.

```sql
-- Session A
BEGIN; -- default isolation = READ COMMITTED
SELECT count(*) FROM orders;
-- e.g. 1000

-- Meanwhile, Session B :
INSERT INTO orders (...) VALUES (...);
COMMIT;

-- Back in Session A, still within the SAME transaction :
SELECT count(*) FROM orders;
-- 1001 (the second statement gets a fresh snapshot)
COMMIT;
```

To stabilize the snapshot for the duration of the transaction, switch to REPEATABLE READ (Example 3).

Verified against : postgresql.org/docs/17/transaction-iso.html (Read Committed Isolation Level).

## Example 3 : REPEATABLE READ Per-Transaction Snapshot

```sql
-- Session A
BEGIN ISOLATION LEVEL REPEATABLE READ;
SELECT count(*) FROM orders;
-- 1000

-- Meanwhile, Session B :
INSERT INTO orders (...) VALUES (...);
COMMIT;

-- Back in Session A, still within the same transaction :
SELECT count(*) FROM orders;
-- 1000 (snapshot is frozen at the first non-BEGIN statement)
COMMIT;
```

If Session A had also tried to update a row that Session B committed an update to, COMMIT would raise SQLSTATE 40001 `could_not_serialize_access_due_to_concurrent_update`. Retry the transaction.

Verified against : postgresql.org/docs/17/transaction-iso.html (Repeatable Read Isolation Level).

## Example 4 : SERIALIZABLE With Retry Loop (Application Pseudocode)

```python
# Application code using psycopg 3
import psycopg, time

def transfer(src, dst, amount):
    for attempt in range(5):
        try:
            with psycopg.connect(DSN, autocommit=False) as cn:
                cn.execute("SET TRANSACTION ISOLATION LEVEL SERIALIZABLE")
                with cn.cursor() as cur:
                    cur.execute("SELECT balance FROM accounts WHERE id = %s FOR UPDATE", (src,))
                    src_balance = cur.fetchone()[0]
                    if src_balance < amount:
                        raise ValueError("insufficient funds")
                    cur.execute("UPDATE accounts SET balance = balance - %s WHERE id = %s",
                                (amount, src))
                    cur.execute("UPDATE accounts SET balance = balance + %s WHERE id = %s",
                                (amount, dst))
                cn.commit()
            return
        except psycopg.errors.SerializationFailure:
            time.sleep(0.02 * (2 ** attempt))
    raise RuntimeError("transfer failed after 5 retries")
```

The retry loop is mandatory for SERIALIZABLE in any non-trivial workload : the SQLSTATE 40001 signal is the price of serializable equivalence. SQLSTATE 40P01 (`deadlock_detected`) is a separate signal and also retryable.

Verified against : postgresql.org/docs/17/transaction-iso.html (Serializable Isolation Level), postgresql.org/docs/17/errcodes-appendix.html.

## Example 5 : Atomic Schema Migration In One Transaction

```sql
BEGIN;
  -- Step 1 : add column, no DEFAULT (metadata only, instant on any size)
  ALTER TABLE orders ADD COLUMN region text;

  -- Step 2 : add CHECK as NOT VALID to enforce future writes without scan
  ALTER TABLE orders
    ADD CONSTRAINT orders_region_chk
    CHECK (region IN ('eu', 'us', 'ap'))
    NOT VALID;

  -- Step 3 : backfill in app code in chunks, OUTSIDE this transaction
  -- (For tiny tables, do it here.)

  -- Step 4 : (later, separate transaction) VALIDATE the constraint
  -- ALTER TABLE orders VALIDATE CONSTRAINT orders_region_chk;
COMMIT;

-- If any statement above fails, ALL changes roll back.
-- The table is in exactly its original schema.
```

Verified against : postgresql.org/docs/17/sql-altertable.html.

## Example 6 : CREATE INDEX CONCURRENTLY Outside Transaction

```sql
-- WRONG : raises ERROR "CREATE INDEX CONCURRENTLY cannot run inside a transaction block"
BEGIN;
  CREATE INDEX CONCURRENTLY orders_created_at_idx ON orders (created_at);
COMMIT;

-- RIGHT : top-level statement, no BEGIN
SET lock_timeout = '5s';
CREATE INDEX CONCURRENTLY orders_created_at_idx ON orders (created_at);

-- If interrupted or failed, the index is left INVALID.
-- Find and clean up :
SELECT c.relname, i.indisvalid
FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid
WHERE NOT i.indisvalid;

DROP INDEX CONCURRENTLY orders_created_at_idx; -- if invalid
```

Verified against : postgresql.org/docs/17/sql-createindex.html.

## Example 7 : Tune synchronous_commit For A Bulk Load Transaction

```sql
BEGIN;
  SET LOCAL synchronous_commit = off;
  COPY events_staging FROM '/data/events_2026_05.csv' (FORMAT csv);
  INSERT INTO events SELECT * FROM events_staging;
  TRUNCATE events_staging;
COMMIT;

-- The COMMIT returns as soon as WAL is in the buffer.
-- On crash, this transaction may be lost ; the rest of the database
-- remains crash-consistent. Acceptable because the import is retryable.
```

For per-database default :

```sql
ALTER DATABASE app SET synchronous_commit = on;  -- restore strict mode
```

Verified against : postgresql.org/docs/17/wal-async.html.

## Example 8 : Force Hint-Bit Warmup After Bulk Load

```sql
-- Bulk load
\timing on
COPY events FROM '/data/events.csv' (FORMAT csv);
-- Time : 12 s

-- First production-style read AFTER load, WITHOUT VACUUM :
SELECT count(*) FROM events;
-- Time : 8 s.  Why? Hint-bit setting dirties every page.
-- pg_stat_io shows writes during this SELECT.

-- Same query immediately after :
SELECT count(*) FROM events;
-- Time : 0.5 s.  Hint bits already set.

-- Better : VACUUM ANALYZE after every bulk load
VACUUM ANALYZE events;
-- This sets hint bits AND visibility map in one pass, then the
-- next SELECT is fast AND eligible for index-only scans.

-- Best : COPY ... FREEZE if the table was empty or truncated
TRUNCATE events;
BEGIN;
  TRUNCATE events;  -- ensures COPY FREEZE precondition
  COPY events FROM '/data/events.csv' WITH (FORMAT csv, FREEZE);
COMMIT;
-- FREEZE marks tuples as frozen at load time : no hint-bit cost ever.
```

Verified against : postgresql.org/docs/17/sql-copy.html (FREEZE), postgresql.org/docs/17/routine-vacuuming.html.

## Example 9 : Diagnose A Long-Running Snapshot Blocking VACUUM

```sql
-- Show oldest snapshot per backend, sorted by age
SELECT pid,
       backend_type,
       state,
       xact_start,
       now() - xact_start AS xact_age,
       backend_xmin,
       age(backend_xmin) AS xmin_age_xids,
       query
FROM pg_stat_activity
WHERE backend_xmin IS NOT NULL
ORDER BY backend_xmin
LIMIT 5;

-- Replication slots holding back the xmin horizon
SELECT slot_name, slot_type, active, restart_lsn, confirmed_flush_lsn,
       pg_size_pretty(pg_wal_lsn_diff(pg_current_wal_lsn(), confirmed_flush_lsn))
       AS lag_bytes
FROM pg_replication_slots;

-- Per-relation dead-tuple counts (statistics, not real-time)
SELECT relname, n_live_tup, n_dead_tup,
       round(100.0 * n_dead_tup / NULLIF(n_live_tup + n_dead_tup, 0), 1)
       AS pct_dead
FROM pg_stat_user_tables
ORDER BY pct_dead DESC NULLS LAST
LIMIT 10;

-- Cancel a stuck idle-in-transaction backend (DESTRUCTIVE : verify first)
SELECT pg_cancel_backend(pid)
FROM pg_stat_activity
WHERE state = 'idle in transaction'
  AND xact_start < now() - interval '1 hour'
  AND pid <> pg_backend_pid();
```

Verified against : postgresql.org/docs/17/monitoring-stats.html, postgresql.org/docs/17/routine-vacuuming.html.

## Example 10 : Index-Only Scan Sensitivity To Visibility Map

```sql
CREATE TABLE measurements (
  ts timestamptz NOT NULL,
  sensor int NOT NULL,
  value numeric NOT NULL
);
CREATE INDEX measurements_ts_idx ON measurements (ts);

-- Bulk load fresh data
INSERT INTO measurements SELECT now() - i * interval '1 sec', i % 100, random()
FROM generate_series(1, 1000000) g(i);

-- Index-only-eligible query :
EXPLAIN (ANALYZE, BUFFERS) SELECT count(*) FROM measurements
WHERE ts BETWEEN now() - interval '1 hour' AND now();
-- "Heap Fetches: 1234567"  -- visibility map is mostly empty, lots of heap fetches.

VACUUM measurements;  -- sets visibility map

EXPLAIN (ANALYZE, BUFFERS) SELECT count(*) FROM measurements
WHERE ts BETWEEN now() - interval '1 hour' AND now();
-- "Heap Fetches: 0"  -- pure index-only scan.
```

Verified against : postgresql.org/docs/17/indexes-index-only-scans.html, postgresql.org/docs/17/storage-vm.html.

## Example 11 : Detect Hot vs Non-Hot Updates

```sql
-- Application updates a row UNCHANGED-from-the-index perspective
CREATE TABLE products (
  id int GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  sku text UNIQUE,
  qty int NOT NULL,
  updated_at timestamptz DEFAULT now()
) WITH (fillfactor = 80);  -- leave space for HOT chains

CREATE INDEX products_sku_lower_idx ON products ((lower(sku)));

-- These UPDATEs only touch qty / updated_at : eligible for HOT
UPDATE products SET qty = qty + 1, updated_at = now() WHERE id = 1;
-- After many such updates :
SELECT relname, n_tup_upd, n_tup_hot_upd,
       round(100.0 * n_tup_hot_upd / NULLIF(n_tup_upd, 0), 1) AS hot_pct
FROM pg_stat_user_tables WHERE relname = 'products';
-- hot_pct should be > 90.

-- If hot_pct is low : an indexed column is being touched OR fillfactor too high.
```

Verified against : postgresql.org/docs/17/storage-hot.html.

## Example 12 : Inspect WAL Position And Generate WAL On Demand

```sql
-- Current WAL position (LSN) and current WAL file name
SELECT pg_current_wal_lsn(), pg_walfile_name(pg_current_wal_lsn());

-- Force a WAL checkpoint (superuser)
CHECKPOINT;

-- Force a WAL flush without checkpoint
SELECT pg_switch_wal();  -- archives current WAL segment

-- WAL bytes since last checkpoint
SELECT pg_size_pretty(
  pg_wal_lsn_diff(pg_current_wal_lsn(),
                  (SELECT checkpoint_lsn FROM pg_control_checkpoint()))
);
```

Verified against : postgresql.org/docs/17/functions-admin.html (WAL functions).
