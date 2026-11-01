---
name: postgres-errors-serialization
description: >
  Use when choosing a transaction isolation level, handling serialization_failure errors, or preventing write-skew anomalies.
  Prevents treating 40001 as a hard error instead of retrying, retrying only the last statement instead of the whole transaction, and assuming REPEATABLE READ prevents write skew (only SERIALIZABLE does).
  Covers READ COMMITTED vs REPEATABLE READ vs SERIALIZABLE, SSI predicate locks, SQLSTATE 40001 serialization_failure, the whole-transaction retry pattern with backoff, SET TRANSACTION ISOLATION LEVEL, read-only deferrable transactions, isolation-level decision tree.
  Keywords: serialization failure, 40001, isolation level, READ COMMITTED, REPEATABLE READ, SERIALIZABLE, SSI, predicate lock, write skew, retry transaction, could not serialize access, transaction keeps failing, which isolation level, phantom read
license: MIT
compatibility: "Designed for Claude Code. Requires PostgreSQL 15, 16, or 17."
metadata:
  author: OpenAEC-Foundation
  version: "1.0"
---

# postgres-errors-serialization

## Quick Reference :

PostgreSQL has three usable isolation levels. `READ UNCOMMITTED` is accepted by
the parser but behaves exactly like `READ COMMITTED`. There is no dirty-read
mode.

| Level | Snapshot scope | Prevents | Still allows |
|-------|----------------|----------|--------------|
| `READ COMMITTED` (default) | per statement | dirty reads | nonrepeatable reads, phantom reads, write skew |
| `REPEATABLE READ` | per transaction | dirty + nonrepeatable + phantom reads | write skew (serialization anomalies) |
| `SERIALIZABLE` | per transaction + SSI | everything, including write skew | nothing |

A transaction running at `REPEATABLE READ` or `SERIALIZABLE` can fail with :

```
ERROR:  could not serialize access due to concurrent update
```
```
ERROR:  could not serialize access due to read/write dependencies among transactions
```

Both carry **SQLSTATE 40001** `serialization_failure`. This is NOT a bug and NOT
a hard error. PostgreSQL aborted the transaction to keep the result equivalent
to some serial order. The one correct response : **retry the ENTIRE transaction
from `BEGIN`**, with backoff. The retry sees the now-committed conflicting data
in its fresh snapshot, so it usually succeeds.

`SERIALIZABLE` uses Serializable Snapshot Isolation (SSI) : it tracks predicate
locks (`SIReadLock` rows in `pg_locks`) to detect dangerous read/write
dependency cycles. SSI locks NEVER block and NEVER cause deadlocks. They only
trigger a 40001 abort. SSI can produce false positives : it may abort a
transaction even when no real anomaly would have occurred. The price of
`SERIALIZABLE` is retry overhead, not lock waits.

## When To Use This Skill :

ALWAYS use this skill when :
- A transaction fails with `ERROR: could not serialize access ...` (SQLSTATE 40001)
- Choosing an isolation level for a transaction or for `default_transaction_isolation`
- Writing transaction retry logic and deciding what to retry on a conflict
- Logic depends on a check-then-act invariant across rows (balance limits,
  double-booking, capacity counts) where write skew is possible
- Building consistent multi-statement reports that must see one stable snapshot

NEVER use this skill for :
- `40P01 deadlock_detected` lock cycles (use postgres-errors-deadlocks)
- `23xxx` constraint violations (use postgres-errors-constraint-violations) :
  those are NEVER retryable
- Plain row-lock waits with no abort (a slow transaction, not a serialization
  failure)

## Decision Trees :

### Which isolation level :

```
What does this transaction need?
├── Single-statement, or each statement independent, races handled
│   explicitly with SELECT FOR UPDATE / ON CONFLICT / atomic UPDATE
│      -> READ COMMITTED   (default, keep it, ~99% of transactions)
│
├── Multiple SELECTs that must all see ONE consistent snapshot
│   (financial report, export, pg_dump-style consistency)
│      -> REPEATABLE READ  (txn-wide snapshot, no phantom reads)
│         Long read-only -> add READ ONLY DEFERRABLE (never aborts)
│
└── Correctness depends on an invariant across rows the transaction
    does NOT all modify (write skew) and you cannot express it as a
    single constraint or locking statement
       -> SERIALIZABLE     (only level that prevents write skew)
          MANDATORY: wrap every SERIALIZABLE txn in a retry loop
```

### I got a 40001, what now :

```
SQLSTATE of the error?
├── 40001 serialization_failure
│   │
│   ├── message "... due to concurrent update"
│   │      -> REPEATABLE READ/SERIALIZABLE txn touched a row another
│   │         txn changed after its snapshot. RETRY WHOLE TRANSACTION.
│   │
│   └── message "... due to read/write dependencies among transactions"
│          -> SSI detected a dangerous cycle under SERIALIZABLE.
│             RETRY WHOLE TRANSACTION. (May be a false positive, still retry.)
│
├── 40P01 deadlock_detected      -> retryable, see postgres-errors-deadlocks
└── 23505 / 23503 / 23xxx        -> NOT retryable. Constraint violation.
                                     Fix the data. See postgres-errors-constraint-violations.
```

### What to retry :

```
Retry the WHOLE transaction, from BEGIN.
├── NEVER retry only the failed statement. The snapshot is already poisoned,
│   the rest of the transaction was rolled back too.
├── Re-run every read AND every write inside the transaction body.
└── Cap retries (e.g. 5 attempts) with exponential backoff + jitter, then
    surface the failure. An unbounded retry loop hides a real hotspot.
```

## Patterns :

### Pattern 1 : Whole-transaction retry loop

ALWAYS : wrap any `REPEATABLE READ` or `SERIALIZABLE` transaction in a loop that
retries the complete transaction on SQLSTATE `40001`.
NEVER : let a 40001 propagate as a fatal error, and NEVER retry a single
statement.

```
attempt = 0
loop:
    begin transaction (isolation level SERIALIZABLE)
    try:
        run ALL reads and writes of the transaction body
        commit
        break                       # success
    except SQLSTATE '40001' or '40P01':
        rollback
        attempt += 1
        if attempt >= MAX_RETRIES:   # e.g. 5
            raise                    # real contention, surface it
        sleep(backoff(attempt) + jitter)
        continue                     # retry the WHOLE transaction
    except any other SQLSTATE:
        rollback
        raise                        # 23xxx etc. are NOT retryable
```

WHY : on 40001 the transaction is already fully rolled back. The retry runs
against a fresh snapshot that includes the conflicting commit, so the logical
conflict is gone. Backoff + jitter stops retrying transactions from
re-colliding in lockstep. See references/examples.md for a Python/psycopg
implementation.

### Pattern 2 : Set the isolation level

ALWAYS : set the level as part of `BEGIN`, or with `SET TRANSACTION` before the
first query.
NEVER : issue `SET TRANSACTION` after a `SELECT`/`INSERT`/`UPDATE` has already
run in the transaction. It is too late and has no effect on that statement.

```sql
-- Option A : per-transaction, on BEGIN (preferred: explicit, local)
BEGIN ISOLATION LEVEL SERIALIZABLE;
  -- ... statements ...
COMMIT;

-- Option B : SET TRANSACTION, must be the first command after BEGIN
BEGIN;
SET TRANSACTION ISOLATION LEVEL REPEATABLE READ;
  -- ... statements ...
COMMIT;

-- Option C : session default for every following transaction
SET SESSION CHARACTERISTICS AS TRANSACTION ISOLATION LEVEL SERIALIZABLE;

-- Option D : database / role default (DBA scope)
ALTER DATABASE app SET default_transaction_isolation = 'repeatable read';
```

WHY : the isolation level is fixed at the first statement's snapshot. Changing
it later is meaningless. Per-transaction `BEGIN ISOLATION LEVEL` keeps the
decision next to the code that depends on it.

### Pattern 3 : SERIALIZABLE for write skew

ALWAYS : use `SERIALIZABLE` when correctness depends on an invariant spanning
rows the transaction does not all lock. `REPEATABLE READ` does NOT catch this.

```sql
-- Two on-call doctors; rule: at least one must stay on shift.
-- Each doctor's transaction reads "is another doctor still on?",
-- sees yes, and sets itself off. Under REPEATABLE READ both commit
-- and the shift ends up empty. This is write skew.

BEGIN ISOLATION LEVEL SERIALIZABLE;
  SELECT count(*) FROM doctors
    WHERE on_call = true AND shift_id = 42;     -- predicate-locked by SSI
  -- application checks count > 1
  UPDATE doctors SET on_call = false
    WHERE id = :me AND shift_id = 42;
COMMIT;   -- one of the two concurrent txns gets 40001 -> retry
```

WHY : SSI predicate-locks the *range* the `SELECT` examined, not just rows
returned. When a concurrent transaction writes into that range and a dangerous
read/write cycle forms, one transaction is aborted with 40001. `REPEATABLE READ`
only locks rows actually modified, so it never sees the conflict.

### Pattern 4 : READ ONLY DEFERRABLE for long reports

ALWAYS : add `READ ONLY DEFERRABLE` to a long-running `SERIALIZABLE` report or
backup transaction so it can never abort with 40001.

```sql
BEGIN ISOLATION LEVEL SERIALIZABLE READ ONLY DEFERRABLE;
  -- blocks at start until a conflict-free snapshot is available,
  -- then runs with no SSI overhead and zero risk of 40001
  SELECT ... ;   -- long aggregation / export
COMMIT;
```

WHY : `DEFERRABLE` has effect ONLY when the transaction is both `SERIALIZABLE`
and `READ ONLY`. It waits at startup for a snapshot guaranteed free of
serialization anomalies, then takes no predicate locks. A multi-hour report
that would otherwise be a frequent 40001 victim becomes abort-proof, at the
cost of a startup wait.

## Anti-Patterns :

(Full cause + symptom + fix in references/anti-patterns.md)

- Treating 40001 as a fatal error instead of retrying : SQLSTATE 40001
- Retrying only the failed statement, not the whole transaction : SQLSTATE 40001
- Running SERIALIZABLE without an application retry loop : SQLSTATE 40001
- Assuming REPEATABLE READ prevents write skew (it does not) : SQLSTATE 40001
- Unbounded retry loop hiding a real contention hotspot
- `SET TRANSACTION ISOLATION LEVEL` issued after the first query (no effect)
- Long SERIALIZABLE report without `READ ONLY DEFERRABLE` : SQLSTATE 40001
- Keeping `SELECT FOR UPDATE` under SERIALIZABLE where SSI already protects

## Reference Links :

- [references/methods.md](references/methods.md) : Isolation levels, SQLSTATE 40001 messages, SET TRANSACTION grammar, SSI tuning parameters, pg_locks SIReadLock
- [references/examples.md](references/examples.md) : Working retry loop (Python/psycopg + SQL), write-skew demo, deferrable report
- [references/anti-patterns.md](references/anti-patterns.md) : Anti-patterns with cause, symptom, SQLSTATE, fix

## See Also :

- postgres-errors-deadlocks : 40P01 deadlock cycles, the other retryable abort
- postgres-errors-constraint-violations : 23xxx errors, which are NEVER retryable
- postgres-core-architecture : MVCC, snapshots, xmin/xmax behind isolation
- Vooronderzoek section : §1 (Architecture + Runtime Model), §19 (Anti-Patterns)
- Official docs : https://www.postgresql.org/docs/17/transaction-iso.html
- Official docs : https://www.postgresql.org/docs/17/sql-set-transaction.html
