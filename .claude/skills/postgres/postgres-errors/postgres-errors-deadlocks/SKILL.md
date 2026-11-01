---
name: postgres-errors-deadlocks
description: >
  Use when transactions fail with deadlock detected, designing locking order, or building a queue with row locks.
  Prevents retrying only the failed statement instead of the whole transaction, inconsistent lock-acquisition order across code paths, and long transactions amplifying lock contention.
  Covers SQLSTATE 40P01 deadlock_detected, deadlock_timeout, the automatic victim-abort, log_lock_waits diagnosis, pg_locks + pg_blocking_pids inspection, consistent lock-ordering prevention, SELECT FOR UPDATE NOWAIT / SKIP LOCKED, whole-transaction retry pattern.
  Keywords: deadlock, deadlock detected, 40P01, deadlock_timeout, lock wait, SELECT FOR UPDATE, NOWAIT, SKIP LOCKED, pg_blocking_pids, log_lock_waits, retry transaction, deadlock detected error, transactions keep deadlocking, how to avoid deadlocks, lock ordering
license: MIT
compatibility: "Designed for Claude Code. Requires PostgreSQL 15, 16, or 17."
metadata:
  author: OpenAEC-Foundation
  version: "1.0"
---

# postgres-errors-deadlocks

## Quick Reference :

A deadlock is two or more transactions each holding a lock the other one needs :
neither can proceed. PostgreSQL detects the cycle automatically and aborts one
transaction (the victim) so the rest complete. The victim's session sees :

```
ERROR:  deadlock detected           -- SQLSTATE 40P01
DETAIL: Process 123 waits for ShareLock on transaction 456; blocked by process 789.
        Process 789 waits for ShareLock on transaction 999; blocked by process 123.
HINT:   See server log for query details.
```

A deadlock is NEVER a PostgreSQL bug. It is always the application acquiring
locks in an inconsistent order across code paths. The two fixes :

1. **Prevent** : every code path acquires locks on multiple rows or tables in the
   SAME order. `SELECT ... FOR UPDATE ... ORDER BY id` makes the order
   deterministic.
2. **Recover** : on `40P01` the WHOLE transaction is already rolled back. Retry
   the ENTIRE transaction from `BEGIN`, with backoff. NEVER retry just the
   failed statement.

Three transaction-rollback errors that all mean "retry the whole transaction" :

| SQLSTATE | Condition | Cause |
|----------|-----------|-------|
| `40P01` | `deadlock_detected` | lock cycle, one transaction was the victim |
| `40001` | `serialization_failure` | serializable/repeatable-read conflict |
| `55P03` | `lock_not_available` | `NOWAIT` could not lock a row immediately |

`40P01` and `40001` are retryable. `55P03` is a deliberate fast-fail you chose
with `NOWAIT` : retry only if waiting is acceptable.

## When To Use This Skill :

ALWAYS use this skill when :
- A transaction fails with `ERROR: deadlock detected` (SQLSTATE 40P01)
- Designing the lock-acquisition order for code that locks multiple rows/tables
- Building a work-queue table consumed by multiple workers
- Writing transaction retry logic, or deciding what to retry on a conflict
- Diagnosing a live lock cycle with `pg_locks` and `pg_blocking_pids`

NEVER use this skill for :
- Plain lock waits with no cycle (a slow transaction, not a deadlock)
- `ACCESS EXCLUSIVE` blocking during DDL (use postgres-impl-zero-downtime-migration)
- Serializable isolation design itself (use postgres-core-transactions-mvcc)

## Decision Trees :

### Which error did I get, and do I retry :

```
Transaction failed. Read the SQLSTATE :
├── 40P01 deadlock_detected
│      -> lock cycle. Transaction is already rolled back.
│         RETRY the whole transaction with backoff. Then fix lock ordering.
├── 40001 serialization_failure
│      -> isolation-level conflict. Also rolled back.
│         RETRY the whole transaction with backoff.
├── 55P03 lock_not_available
│      -> a NOWAIT clause could not lock a row immediately.
│         This is the fast-fail you asked for. Retry ONLY if waiting is OK.
└── No error, just slow
       -> a lock WAIT, not a deadlock. No cycle. Find the blocker
          with pg_blocking_pids; the wait resolves on its own.
```

### Which FOR lock mode :

```
What will the transaction do with the row?
├── UPDATE a key column, or DELETE the row
│      -> FOR UPDATE          (strongest row lock)
├── UPDATE only non-key columns
│      -> FOR NO KEY UPDATE   (lets FOR KEY SHARE readers through)
├── Read and forbid concurrent modification
│      -> FOR SHARE           (shared; blocks UPDATE/DELETE)
└── Read and only forbid the row's key from changing / row being deleted
       -> FOR KEY SHARE       (weakest; what FK checks take)
```

### Preventing a deadlock :

```
Code locks more than one row or table?
├── Lock them in ONE statement when possible
│      -> UPDATE ... WHERE id IN (...)   (PostgreSQL picks a consistent order)
├── Multiple statements unavoidable?
│      -> impose a global order : SELECT ... FOR UPDATE ORDER BY id
│         EVERY code path uses the SAME ordering key.
└── Acquire the most restrictive mode FIRST
       -> do not take FOR SHARE then upgrade to FOR UPDATE on the same row.
```

## Patterns :

### Pattern 1 : Retry the whole transaction on 40P01

ALWAYS retry the ENTIRE transaction from `BEGIN` when you catch `40P01`. NEVER
retry only the statement that raised the error.

```python
# psycopg 3 : retry the whole unit of work, with exponential backoff
import time, psycopg
from psycopg import errors

def run_txn(conn, work):
    for attempt in range(5):
        try:
            with conn.transaction():       # BEGIN ... COMMIT
                work(conn)
            return
        except (errors.DeadlockDetected, errors.SerializationFailure):
            conn.rollback()
            time.sleep(0.05 * 2 ** attempt)  # 50ms, 100ms, 200ms, ...
    raise RuntimeError("transaction failed after 5 attempts")
```

WHY : when PostgreSQL raises `40P01` it has ALREADY rolled back the victim
transaction completely. Every statement in it must run again. Re-issuing only the
failed statement runs it outside the original transaction's intent and corrupts
state. The retry needs backoff (and ideally jitter) so the racing transactions do
not collide again in lockstep.

### Pattern 2 : Consistent lock ordering with ORDER BY

ALWAYS lock multiple rows in a deterministic order, the same in every code path.
NEVER let two code paths lock the same rows in opposite orders.

```sql
-- Both "transfer A->B" and "transfer B->A" lock the lower id first :
BEGIN;
SELECT * FROM accounts
WHERE id IN (:from_id, :to_id)
ORDER BY id            -- the global ordering key, identical everywhere
FOR UPDATE;

UPDATE accounts SET balance = balance - :amt WHERE id = :from_id;
UPDATE accounts SET balance = balance + :amt WHERE id = :to_id;
COMMIT;
```

WHY : the official guidance is to "avoid them by being certain that all
applications using a database acquire locks on multiple objects in a consistent
order." A deadlock needs a cycle; a single global lock order makes a cycle
impossible. `ORDER BY id` in the locking `SELECT` pins that order.

### Pattern 3 : Lock everything in one statement

ALWAYS prefer a single statement that touches all the rows over several
statements that each lock one. NEVER spread a multi-row update across statements
when one statement expresses it.

```sql
-- One UPDATE : PostgreSQL locks the matched rows itself, no cross-statement gap
UPDATE inventory
SET qty = qty - 1
WHERE sku = ANY(:skus);
```

WHY : a deadlock window opens only BETWEEN statements, while a transaction holds
some locks and reaches for more. A single statement gives a concurrent
transaction no interleaving point. Fewer statements means a smaller cycle surface.

### Pattern 4 : NOWAIT to fail fast instead of waiting

ALWAYS use `NOWAIT` when blocking on a locked row is worse than failing now.
NEVER use it where the caller has no retry or fallback path.

```sql
-- Reports SQLSTATE 55P03 immediately if the row is already locked
SELECT * FROM jobs WHERE id = :id FOR UPDATE NOWAIT;
```

WHY : "With NOWAIT, the statement reports an error, rather than waiting, if a
selected row cannot be locked immediately." This converts an unbounded wait into
an instant, catchable `55P03 lock_not_available`. The caller decides : retry
later, pick another row, or surface a "busy" response.

### Pattern 5 : SKIP LOCKED for a work queue

ALWAYS use `SKIP LOCKED` when multiple workers pull jobs from one table. NEVER
use it for general-purpose reads : it deliberately returns an inconsistent view.

```sql
-- Each worker grabs a different unlocked job, no contention, no deadlock
BEGIN;
SELECT id, payload FROM jobs
WHERE status = 'pending'
ORDER BY created_at
FOR UPDATE SKIP LOCKED
LIMIT 1;
-- ... process the job ...
UPDATE jobs SET status = 'done' WHERE id = :id;
COMMIT;
```

WHY : "any selected rows that cannot be immediately locked are skipped ... can be
used to avoid lock contention with multiple consumers accessing a queue-like
table." Worker A locks job 1, worker B skips job 1 and locks job 2. No worker
waits, so no lock cycle can form.

### Pattern 6 : Diagnose a live lock cycle

ALWAYS use `pg_blocking_pids()` joined to `pg_stat_activity` to see who blocks
whom. NEVER guess from query timing alone.

```sql
-- Every waiting backend and the PIDs blocking it
SELECT a.pid,
       a.wait_event_type,
       a.state,
       pg_blocking_pids(a.pid) AS blocked_by,
       left(a.query, 80)       AS current_query
FROM pg_stat_activity a
WHERE cardinality(pg_blocking_pids(a.pid)) > 0;
```

WHY : `pg_blocking_pids(pid)` returns the array of process IDs holding a lock the
given PID waits for. When PID X is blocked by Y and Y is blocked by X, that is the
cycle. Set `log_lock_waits = on` so any wait longer than `deadlock_timeout` is
written to the server log with the blocking query for post-mortem analysis.

### Pattern 7 : Keep transactions short

ALWAYS commit as soon as the unit of work is done. NEVER hold an open transaction
across user think-time, network calls, or batch sleeps.

WHY : a transaction holds every lock it has taken until `COMMIT` or `ROLLBACK`.
A long transaction widens the window in which another transaction can form a
cycle with it, and amplifies plain lock waits for everyone behind it. `deadlock_timeout`
(default `1s`) is the delay before the detector even runs : long transactions
make that 1s wait routine instead of rare.

## Anti-Patterns :

(Full cause + symptom + fix in references/anti-patterns.md)

- Retrying only the failed statement after `40P01` : the whole transaction is gone
- Inconsistent lock order across code paths : the direct cause of every deadlock
- Long transactions holding locks : deadlock window + lock-wait amplification
- Lock upgrade `FOR SHARE` then `FOR UPDATE` on the same row : self-inflicted cycle
- Treating `40P01` as a PostgreSQL bug : it is always application lock ordering
- Retry without backoff or jitter : the racing transactions collide again at once

## Reference Links :

- [references/methods.md](references/methods.md) : Lock modes, conflict matrix, GUCs, locking-clause grammar, diagnostic views
- [references/examples.md](references/examples.md) : Working retry loops, queue pattern, diagnostic queries, version-annotated
- [references/anti-patterns.md](references/anti-patterns.md) : Anti-patterns with cause, symptom, SQLSTATE, and fix

## See Also :

- postgres-core-transactions-mvcc : isolation levels and `40001` serialization failures
- postgres-impl-zero-downtime-migration : `ACCESS EXCLUSIVE` lock waits during DDL
- postgres-impl-schema-archaeology : inspecting `pg_locks` and blocking sessions
- Vooronderzoek section : §19 (Anti-Patterns), §6 (Performance + EXPLAIN)
- Official docs : https://www.postgresql.org/docs/17/explicit-locking.html
- Official docs : https://www.postgresql.org/docs/17/errcodes-appendix.html
