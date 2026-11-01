# Deadlocks : Examples

Working code for prevention, recovery, and diagnosis. Every snippet is verified
against PostgreSQL 17 official documentation. All patterns work on v15, v16, and
v17 ; deadlock handling has no version-specific behavior in this range.

## 1. A deadlock, reproduced

```sql
-- Session 1                          -- Session 2
BEGIN;                                BEGIN;
UPDATE accounts                       UPDATE accounts
  SET balance = balance - 10            SET balance = balance - 10
  WHERE id = 1;                         WHERE id = 2;
-- (holds lock on row 1)              -- (holds lock on row 2)

UPDATE accounts                       UPDATE accounts
  SET balance = balance + 10            SET balance = balance + 10
  WHERE id = 2;                         WHERE id = 1;
-- waits for session 2's row 2        -- waits for session 1's row 1
--                                    -- ERROR: deadlock detected  (40P01)
```

The cycle : session 1 holds row 1 and wants row 2; session 2 holds row 2 and
wants row 1. PostgreSQL aborts one of them.

## 2. The fix : consistent lock ordering

```sql
-- Every transfer locks the LOWER id first, in one locking SELECT.
-- Transfer 1->2 and transfer 2->1 now take the same locks in the same order.
BEGIN;
SELECT id, balance FROM accounts
WHERE id IN (1, 2)
ORDER BY id                       -- the global order; identical in all code paths
FOR UPDATE;

UPDATE accounts SET balance = balance - 10 WHERE id = 1;
UPDATE accounts SET balance = balance + 10 WHERE id = 2;
COMMIT;
```

## 3. Whole-transaction retry loop : psycopg 3

```python
import time, random, psycopg
from psycopg import errors

RETRYABLE = (errors.DeadlockDetected, errors.SerializationFailure)

def run_txn(conn, work, max_attempts=5):
    for attempt in range(max_attempts):
        try:
            with conn.transaction():          # BEGIN ... COMMIT
                work(conn)                    # ALL statements rerun on retry
            return
        except RETRYABLE:
            conn.rollback()
            backoff = 0.05 * 2 ** attempt + random.uniform(0, 0.05)
            time.sleep(backoff)               # exponential backoff + jitter
    raise RuntimeError(f"transaction failed after {max_attempts} attempts")
```

## 4. Whole-transaction retry loop : psycopg 2

```python
import time, random, psycopg2
from psycopg2.errors import DeadlockDetected, SerializationFailure

RETRYABLE = (DeadlockDetected, SerializationFailure)

def run_txn(conn, work, max_attempts=5):
    for attempt in range(max_attempts):
        try:
            with conn:                        # commits on success, rolls back on error
                with conn.cursor() as cur:
                    work(cur)
            return
        except RETRYABLE:
            time.sleep(0.05 * 2 ** attempt + random.uniform(0, 0.05))
    raise RuntimeError("transaction failed after retries")
```

## 5. SQL-level retry by SQLSTATE

```sql
-- Any client : branch on the five-character SQLSTATE, not the message text.
-- 40P01 = deadlock_detected, 40001 = serialization_failure -> retry from BEGIN.
-- The retry MUST re-issue every statement of the transaction.
```

## 6. NOWAIT : fail fast on a locked row

```sql
-- Raises SQLSTATE 55P03 immediately instead of waiting.
BEGIN;
SELECT * FROM jobs WHERE id = 42 FOR UPDATE NOWAIT;
-- on 55P03 : the row is busy. ROLLBACK and either retry later or report "busy".
COMMIT;
```

## 7. SKIP LOCKED : a concurrent work queue

```sql
-- Many workers run this loop. Each grabs a different unlocked job.
BEGIN;
SELECT id, payload
FROM jobs
WHERE status = 'pending'
ORDER BY created_at
FOR UPDATE SKIP LOCKED
LIMIT 1;
-- ... process the job in the application ...
UPDATE jobs SET status = 'done', finished_at = now() WHERE id = :id;
COMMIT;
```

```sql
-- Batch claim : grab up to N jobs at once, still contention-free.
BEGIN;
WITH claimed AS (
  SELECT id FROM jobs
  WHERE status = 'pending'
  ORDER BY created_at
  FOR UPDATE SKIP LOCKED
  LIMIT 10
)
UPDATE jobs j
SET status = 'processing'
FROM claimed c
WHERE j.id = c.id
RETURNING j.id, j.payload;
COMMIT;
```

## 8. Choosing the FOR mode

```sql
-- Will DELETE the row or change its primary key : strongest lock.
SELECT * FROM accounts WHERE id = :id FOR UPDATE;

-- Will UPDATE only non-key columns : lets FK checks (FOR KEY SHARE) proceed.
SELECT * FROM accounts WHERE id = :id FOR NO KEY UPDATE;

-- Read the row and forbid anyone modifying it for the rest of the transaction.
SELECT * FROM accounts WHERE id = :id FOR SHARE;

-- Only need the key to stay stable; allow concurrent non-key updates.
SELECT * FROM accounts WHERE id = :id FOR KEY SHARE;
```

## 9. Diagnose a live lock cycle

```sql
-- Who is waiting, and which PIDs are blocking them.
SELECT a.pid,
       a.state,
       a.wait_event_type,
       pg_blocking_pids(a.pid)        AS blocked_by,
       a.xact_start,
       left(a.query, 100)             AS current_query
FROM pg_stat_activity a
WHERE cardinality(pg_blocking_pids(a.pid)) > 0
ORDER BY a.xact_start;
```

```sql
-- All locks held or awaited on a specific table.
SELECT l.pid, l.mode, l.granted, a.state, left(a.query, 80) AS query
FROM pg_locks l
JOIN pg_stat_activity a ON a.pid = l.pid
WHERE l.relation = 'accounts'::regclass
ORDER BY l.granted DESC, l.pid;
```

```sql
-- Last resort : terminate a backend that is the head of a stuck chain.
-- Confirm the PID with the queries above first.
SELECT pg_terminate_backend(:pid);
```

## 10. Enable lock-wait logging for post-mortem analysis

```sql
-- Session-level for a quick investigation :
SET log_lock_waits = on;

-- Cluster-wide in postgresql.conf, then reload :
-- log_lock_waits = on
-- deadlock_timeout = '1s'      -- waits longer than this get logged
SELECT pg_reload_conf();
```

With `log_lock_waits = on`, every wait longer than `deadlock_timeout` writes a
log line naming the blocking process and the lock, so the cause is visible after
the fact without catching the cycle live.

## 11. A complete deadlock error message

```
ERROR:  deadlock detected
DETAIL: Process 18444 waits for ShareLock on transaction 1037; blocked by process 18445.
        Process 18445 waits for ShareLock on transaction 1036; blocked by process 18444.
        Process 18444: UPDATE accounts SET balance = balance + 10 WHERE id = 2;
        Process 18445: UPDATE accounts SET balance = balance + 10 WHERE id = 1;
HINT:   See server log for query details.
CONTEXT: while updating tuple (0,2) in relation "accounts"
```

The `DETAIL` lines name both sides of the cycle and the exact statements : read
them to find the two code paths whose lock order disagrees.
