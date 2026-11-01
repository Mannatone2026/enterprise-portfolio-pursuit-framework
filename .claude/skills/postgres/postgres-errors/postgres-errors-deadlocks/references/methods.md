# Deadlocks : Methods Reference

Lock modes, the conflict matrix, the locking-clause grammar, configuration
parameters, and diagnostic views. Verified against PostgreSQL 17 official
documentation.

## What a deadlock is

A deadlock occurs when two or more transactions each hold a lock that another
needs. Official example :

> "if transaction 1 acquires an exclusive lock on table A and then tries to
> acquire an exclusive lock on table B, while transaction 2 has already
> exclusive-locked table B and now wants an exclusive lock on table A, then
> neither one can proceed."

PostgreSQL resolves it automatically :

> "PostgreSQL automatically detects deadlock situations and resolves them by
> aborting one of the transactions involved, allowing the other(s) to complete.
> (Exactly which transaction will be aborted is difficult to predict and should
> not be relied upon.)"

The aborted transaction is the victim; its session receives SQLSTATE `40P01`.

## SQLSTATE codes

### Class 40 : Transaction Rollback

| Code | Condition name | Meaning |
|------|----------------|---------|
| `40000` | `transaction_rollback` | generic transaction rollback |
| `40001` | `serialization_failure` | could not serialize; retry the transaction |
| `40002` | `transaction_integrity_constraint_violation` | constraint violation forced rollback |
| `40003` | `statement_completion_unknown` | completion state unknown |
| `40P01` | `deadlock_detected` | this transaction was the deadlock victim |

### Class 55 : Object Not In Prerequisite State

| Code | Condition name | Meaning |
|------|----------------|---------|
| `55006` | `object_in_use` | object is in use and cannot be changed |
| `55P03` | `lock_not_available` | `NOWAIT` could not acquire the lock immediately |

`40P01` and `40001` are retryable : retry the whole transaction. `55P03` is the
deliberate fast-fail you requested with `NOWAIT`.

## Row-level lock modes

Acquired by the `FOR ...` locking clause of `SELECT`.

| Mode | Strength | Blocks | Use when |
|------|----------|--------|----------|
| `FOR UPDATE` | strongest | `UPDATE`, `DELETE`, `SELECT FOR UPDATE / NO KEY UPDATE / SHARE / KEY SHARE` on the row | you will `UPDATE` a key column or `DELETE` the row |
| `FOR NO KEY UPDATE` | strong | same as `FOR UPDATE` except it does NOT block `FOR KEY SHARE` | you will `UPDATE` only non-key columns |
| `FOR SHARE` | shared | `UPDATE`, `DELETE`, `SELECT FOR UPDATE / NO KEY UPDATE`; allows other `FOR SHARE` / `FOR KEY SHARE` | you read a row and must forbid concurrent modification |
| `FOR KEY SHARE` | weakest | `DELETE` and key-changing `UPDATE` only | you only need the row's key to stay stable (what FK checks take) |

A plain `UPDATE` of non-key columns takes `FOR NO KEY UPDATE` internally; an
`UPDATE` of a key column or a `DELETE` takes `FOR UPDATE`. A foreign-key check on
the parent row takes `FOR KEY SHARE`.

## Locking-clause grammar

```
SELECT ...
  FOR { UPDATE | NO KEY UPDATE | SHARE | KEY SHARE }
      [ OF table_name [, ...] ]
      [ NOWAIT | SKIP LOCKED ]
```

- `OF table_name` : in a multi-table query, lock only rows from the named tables.
- `NOWAIT` : "the statement reports an error, rather than waiting, if a selected
  row cannot be locked immediately." The error is `55P03 lock_not_available`.
- `SKIP LOCKED` : "any selected rows that cannot be immediately locked are
  skipped. Skipping locked rows provides an inconsistent view of the data, so
  this is not suitable for general purpose work, but can be used to avoid lock
  contention with multiple consumers accessing a queue-like table."
- Default (neither clause) : the statement waits until the lock is free.
- `NOWAIT` and `SKIP LOCKED` apply only to the row-level locks; the table-level
  `ROW SHARE` lock is still taken normally.

`NOWAIT` and `SKIP LOCKED` are mutually exclusive in one locking clause.

## Configuration parameters

### deadlock_timeout

> "This is the amount of time to wait on a lock before checking to see if there
> is a deadlock condition. The check for deadlock is relatively expensive, so the
> server doesn't run it every time it waits for a lock."

- Default : `1s`. Without a unit, the value is milliseconds.
- The detector runs only after a transaction has waited this long, so a real
  deadlock is broken roughly `deadlock_timeout` after it forms, not instantly.
- Guidance : the setting should exceed typical transaction time so a lock is
  usually released before the waiter bothers to check for a deadlock.

### log_lock_waits

- Default : `off`.
- When `on`, a log message is produced whenever a session waits longer than
  `deadlock_timeout` to acquire a lock. The message names the blocking process
  and the lock. Essential for diagnosing both deadlocks and slow lock waits.
- The same `deadlock_timeout` value sets the delay before this log message.

### max_locks_per_transaction

- Default : `64`. Set only at server start.
- Sizes the shared lock table : `max_locks_per_transaction` distinct lockable
  objects (tables, etc.) per backend. Not a limit on locked rows, which is
  unlimited.

## Diagnostic views and functions

| Object | Use |
|--------|-----|
| `pg_blocking_pids(int)` | returns `integer[]` of PIDs whose locks block the given PID |
| `pg_stat_activity` | running sessions : `pid`, `state`, `wait_event_type`, `wait_event`, `query`, `xact_start` |
| `pg_locks` | every held and awaited lock : `pid`, `locktype`, `mode`, `granted`, `relation` |

A lock cycle shows up as PID X in `pg_blocking_pids(Y)` while PID Y is in
`pg_blocking_pids(X)`. `wait_event_type = 'Lock'` in `pg_stat_activity` marks a
backend blocked on a lock.

## Distinguishing deadlock from related conditions

| Symptom | It is | Action |
|---------|-------|--------|
| `ERROR: deadlock detected` (`40P01`) | a true lock cycle, victim aborted | retry whole transaction |
| `ERROR: could not serialize ...` (`40001`) | isolation conflict | retry whole transaction |
| `ERROR: could not obtain lock ...` (`55P03`) | `NOWAIT` fast-fail | retry only if waiting is acceptable |
| Query just hangs, no error | a lock WAIT, no cycle | find blocker; it clears when the holder commits |

Only a genuine cycle is a deadlock. A single transaction blocked behind a
long-running one is an ordinary wait and resolves itself.
