# Deadlocks : Anti-Patterns

Each entry : cause, symptom, SQLSTATE where applicable, and the fix pattern.
Verified against PostgreSQL 17 official documentation.

## AP-1 : Retrying only the failed statement after 40P01

CAUSE : the application catches `40P01 deadlock_detected`, then re-runs only the
statement that raised the error instead of the whole transaction.

SYMPTOM : the retried statement runs against a transaction state that no longer
exists. Either it errors with `25P02 in_failed_sql_transaction`, or it commits
partial work that the rest of the original transaction was supposed to balance,
leaving the data inconsistent.

FIX : when PostgreSQL raises `40P01` the victim transaction is ALREADY rolled
back in full. Retry the ENTIRE transaction from `BEGIN`, re-issuing every
statement, with exponential backoff.

```python
# WRONG : re-run one statement
try:
    cur.execute(stmt_b)
except errors.DeadlockDetected:
    cur.execute(stmt_b)            # transaction is gone; this is broken

# RIGHT : re-run the whole unit of work
for attempt in range(5):
    try:
        with conn.transaction():
            cur.execute(stmt_a); cur.execute(stmt_b); cur.execute(stmt_c)
        break
    except errors.DeadlockDetected:
        conn.rollback()
        time.sleep(0.05 * 2 ** attempt)
```

## AP-2 : Inconsistent lock-acquisition order across code paths

CAUSE : one code path locks row A then row B; another locks row B then row A.
The classic example is a money transfer that locks the "from" account first, so
transfer X->Y and transfer Y->X take the two locks in opposite orders.

SYMPTOM : intermittent `ERROR: deadlock detected` (`40P01`) under concurrency,
impossible to reproduce single-threaded. The `DETAIL` lines of the error name
both statements and their opposite lock orders.

FIX : impose a single global lock order that EVERY code path obeys. Lock rows
with `SELECT ... FOR UPDATE ... ORDER BY <key>` so the order is deterministic
regardless of business direction. Official guidance : "the best defense against
deadlocks is generally to avoid them by being certain that all applications
using a database acquire locks on multiple objects in a consistent order."

```sql
-- Both directions lock the lower id first :
SELECT * FROM accounts WHERE id IN (:a, :b) ORDER BY id FOR UPDATE;
```

## AP-3 : Long transactions holding locks

CAUSE : a transaction stays open across user think-time, an external HTTP call,
a file read, or a sleep between batches, holding every lock it has acquired.

SYMPTOM : rising `40P01` rates plus widespread slow queries. Every lock the long
transaction holds blocks others; the wide time window lets a cycle form; waiters
behind it all pay at least `deadlock_timeout` (default `1s`) before anything
moves.

FIX : keep transactions short. Acquire locks as late as possible, do no slow
non-database work inside the transaction, and `COMMIT` as soon as the unit of
work is done. Set `idle_in_transaction_session_timeout` to kill transactions left
open by accident.

## AP-4 : Lock upgrade from FOR SHARE to FOR UPDATE

CAUSE : a transaction takes `FOR SHARE` (or a plain read) on a row, then later
tries to take `FOR UPDATE` on the same row. Two transactions doing this on the
same row each hold a share lock and each wait to upgrade.

SYMPTOM : `40P01` on a single row that the code never appears to lock twice. The
cycle is the mutual upgrade : neither can get the exclusive lock while the other
holds a share lock.

FIX : acquire the most restrictive lock you will need on the FIRST touch of the
row. Official guidance : "ensure that the first lock acquired on an object in a
transaction is the most restrictive mode that will be needed for that object."
If the row will be updated, take `FOR UPDATE` up front, not `FOR SHARE`.

```sql
-- WRONG : read now, upgrade later
SELECT balance FROM accounts WHERE id = :id FOR SHARE;
-- ... later ...
SELECT balance FROM accounts WHERE id = :id FOR UPDATE;   -- upgrade -> cycle risk

-- RIGHT : take the strong lock first
SELECT balance FROM accounts WHERE id = :id FOR UPDATE;
```

## AP-5 : Treating 40P01 as a PostgreSQL bug

CAUSE : a developer sees `deadlock detected` and assumes the database
misbehaved, files it as a PostgreSQL defect, or disables deadlock detection
ideas.

SYMPTOM : the deadlock keeps happening because the real cause, the application's
lock ordering, is never addressed. Time is lost looking in the wrong place.

FIX : a deadlock is ALWAYS an application lock-ordering problem. PostgreSQL only
detects and reports it. Read the `40P01` `DETAIL` lines, identify the two code
paths whose lock order disagrees, and impose a consistent order (see AP-2).
There is nothing to fix in PostgreSQL.

## AP-6 : Retrying without backoff or jitter

CAUSE : the retry loop re-runs the transaction immediately on `40P01`, with no
delay and no randomization.

SYMPTOM : the same two transactions deadlock again on the retry, and again,
because they restart in lockstep and re-create the identical race. Retry counts
exhaust and the work still fails.

FIX : use exponential backoff plus random jitter so racing transactions
desynchronize. Combined with a consistent lock order (AP-2), retries then
succeed quickly because the cycle no longer re-forms.

```python
backoff = base * 2 ** attempt + random.uniform(0, base)
time.sleep(backoff)
```

## AP-7 : Using SKIP LOCKED for general-purpose reads

CAUSE : `SELECT ... FOR UPDATE SKIP LOCKED` is used for ordinary queries because
it never blocks.

SYMPTOM : rows that happen to be locked by another transaction silently vanish
from the result set. Reports under-count, "missing" records appear at random,
and the bug is invisible in single-user testing.

FIX : `SKIP LOCKED` is only correct for queue-like workloads where each row is a
unit of work claimed by exactly one consumer. Official docs : it "provides an
inconsistent view of the data, so this is not suitable for general purpose
work." For normal reads, omit the locking clause; for must-lock reads that
should not block, use `NOWAIT` and handle `55P03`.
