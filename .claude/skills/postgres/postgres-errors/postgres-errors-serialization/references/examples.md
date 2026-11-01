# postgres-errors-serialization : examples

Working, version-annotated code. Verified against PostgreSQL 17
(transaction-iso.html, sql-set-transaction.html). Behavior is identical on
v15, v16, and v17.

## Example 1 : whole-transaction retry loop (Python / psycopg 3, v15+)

The retry loop owns `BEGIN` and `COMMIT`. The transaction body is a callback so
it can be re-run verbatim on a 40001 / 40P01 abort.

```python
import time
import random
import psycopg
from psycopg.errors import SerializationFailure, DeadlockDetected

MAX_RETRIES = 5

def run_in_serializable(conn, body):
    """Run `body(cur)` in a SERIALIZABLE transaction, retrying the WHOLE
    transaction on serialization failure. `body` must be idempotent across
    re-runs: it re-reads and re-writes every time."""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            with conn.transaction():                      # BEGIN ... COMMIT
                with conn.cursor() as cur:
                    cur.execute(
                        "SET TRANSACTION ISOLATION LEVEL SERIALIZABLE")
                    body(cur)
            return                                        # committed
        except (SerializationFailure, DeadlockDetected):
            # transaction is already rolled back by psycopg
            if attempt == MAX_RETRIES:
                raise                                     # real hotspot
            backoff = (2 ** attempt) * 0.01               # 20ms, 40ms, ...
            time.sleep(backoff + random.uniform(0, 0.01)) # + jitter
        # any other error (23505, etc.) is NOT caught -> propagates, no retry

# usage
def transfer(cur):
    cur.execute("SELECT balance FROM account WHERE id = 1")
    (bal,) = cur.fetchone()
    if bal < 100:
        raise ValueError("insufficient funds")
    cur.execute("UPDATE account SET balance = balance - 100 WHERE id = 1")
    cur.execute("UPDATE account SET balance = balance + 100 WHERE id = 2")

with psycopg.connect("dbname=app") as conn:
    run_in_serializable(conn, transfer)
```

Key points :
- The loop catches ONLY `SerializationFailure` (40001) and `DeadlockDetected`
  (40P01). Constraint errors fall through and are never retried.
- `conn.transaction()` issues `BEGIN`/`COMMIT`/`ROLLBACK`. The whole body runs
  again on retry : every read AND every write.
- Exponential backoff plus jitter stops two retrying transactions from
  re-colliding in lockstep.
- After `MAX_RETRIES` the error propagates : sustained 40001 means a genuine
  contention hotspot to fix, not to retry harder.

## Example 2 : the same retry loop in plain SQL terms (v15+)

What the application loop does, one iteration, expressed as SQL :

```sql
-- attempt N
BEGIN ISOLATION LEVEL SERIALIZABLE;
  SELECT balance FROM account WHERE id = 1;     -- read
  UPDATE account SET balance = balance - 100 WHERE id = 1;
  UPDATE account SET balance = balance + 100 WHERE id = 2;
COMMIT;
-- if COMMIT (or any statement) raised SQLSTATE 40001:
--   the transaction is ALREADY rolled back; run the whole block again.
```

There is no SQL-only retry construct. The loop MUST live in application code or
a driver wrapper. A server-side `PL/pgSQL` block cannot retry its own
transaction because a 40001 aborts the surrounding transaction entirely.

## Example 3 : write skew demo, REPEATABLE READ fails, SERIALIZABLE catches it (v15+)

Invariant : the two rows `('x')` and `('y')` must always sum to at least 1
"active" flag. Two concurrent transactions each turn off the flag they did not
read as the last active one.

```sql
-- setup
CREATE TABLE flags (name text PRIMARY KEY, active boolean);
INSERT INTO flags VALUES ('x', true), ('y', true);
```

Session A and Session B run concurrently :

```sql
-- Session A
BEGIN ISOLATION LEVEL SERIALIZABLE;
SELECT count(*) FROM flags WHERE active;        -- sees 2
UPDATE flags SET active = false WHERE name = 'x';

-- Session B  (interleaved, before A commits)
BEGIN ISOLATION LEVEL SERIALIZABLE;
SELECT count(*) FROM flags WHERE active;        -- also sees 2
UPDATE flags SET active = false WHERE name = 'y';

-- Session A
COMMIT;                                         -- succeeds

-- Session B
COMMIT;
-- ERROR:  could not serialize access due to read/write dependencies among transactions
-- SQLSTATE 40001  -> Session B retries the WHOLE transaction.
-- On retry it sees active count = 1 and its check prevents the bad UPDATE.
```

Under `REPEATABLE READ` both commits succeed and the invariant is broken : zero
flags active. `REPEATABLE READ` only conflicts on rows actually modified, and A
and B modified different rows. Only `SERIALIZABLE` predicate-locks the range the
`SELECT count(*)` examined and detects the cycle.

## Example 4 : consistent multi-statement report, REPEATABLE READ (v15+)

A report whose totals must all reflect ONE snapshot, even while writers commit.

```sql
BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY;
  SELECT sum(amount) FROM ledger WHERE kind = 'debit';
  SELECT sum(amount) FROM ledger WHERE kind = 'credit';
  SELECT count(*)    FROM ledger;
COMMIT;
-- all three queries see the database as of the first SELECT's snapshot
```

`REPEATABLE READ` is enough here : a read-only transaction never raises 40001,
and the txn-wide snapshot guarantees the three queries agree.

## Example 5 : long report that must never abort, SERIALIZABLE READ ONLY DEFERRABLE (v15+)

For a multi-hour aggregation that must be fully serializable yet cannot tolerate
a 40001 abort hours in :

```sql
BEGIN ISOLATION LEVEL SERIALIZABLE READ ONLY DEFERRABLE;
  -- blocks at startup until a conflict-free snapshot is available,
  -- then runs with no SSI overhead and zero risk of serialization failure
  SELECT region, sum(amount), count(*)
  FROM huge_ledger
  GROUP BY region;
COMMIT;
```

`DEFERRABLE` only takes effect because the transaction is BOTH `SERIALIZABLE`
AND `READ ONLY`. It trades a startup wait for guaranteed completion.

## Example 6 : inspect SSI predicate locks (v15+)

```sql
-- which transactions currently hold predicate (SIRead) locks
SELECT a.pid, a.state, l.relation::regclass, l.mode
FROM pg_locks l
JOIN pg_stat_activity a ON a.pid = l.pid
WHERE l.mode = 'SIReadLock'
ORDER BY a.pid;
```

A relation-wide `SIReadLock` (no page/tuple detail) signals a sequential scan
took a coarse predicate lock, a common source of false-positive 40001 aborts.
Add an index so the scan, and its predicate lock, become selective.
