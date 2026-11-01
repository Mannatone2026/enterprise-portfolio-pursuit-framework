---
name: postgres-impl-advisory-locks
description: >
  Use when coordinating across application instances, ensuring a scheduled job runs at most once, or building a distributed mutex with PostgreSQL.
  Prevents session-level lock leaks (held until disconnect), key collisions between unrelated subsystems, and advisory locks misbehaving through a transaction-mode connection pooler.
  Covers session-level vs transaction-level advisory locks, pg_advisory_lock / pg_advisory_xact_lock / pg_try_advisory_lock, shared variants, single-key vs two-key namespacing, pg_locks inspection, cron-at-most-once and distributed-mutex patterns.
  Keywords: advisory lock, pg_advisory_lock, pg_advisory_xact_lock, pg_try_advisory_lock, distributed lock, mutex, cron at most once, job coordination, SKIP LOCKED, advisory lock leak, run job once across instances, application lock, pg_locks advisory
license: MIT
compatibility: "Designed for Claude Code. Requires PostgreSQL 15, 16, or 17."
metadata:
  author: OpenAEC-Foundation
  version: "1.0"
---

# postgres-impl-advisory-locks

## Quick Reference :

An advisory lock is a lock with an application-defined meaning. PostgreSQL does NOT
enforce it: it locks an integer key, not a row or table, and it is up to the
application to agree on what that key represents. The server only guarantees that two
sessions cannot hold the same exclusive key at once.

Use advisory locks for coordination that does not map onto a row: "only one worker runs
this cron job", "only one instance rebuilds this cache", "serialize this external API
call". They are faster than a flag column, cause no table bloat, and are cleaned up
automatically at session end.

The behaviour is identical across PostgreSQL 15, 16, and 17.

```
need a lock?
  ├─ protecting a specific table ROW         → SELECT ... FOR UPDATE  (NOT advisory)
  ├─ coordinating something with no row      → advisory lock
  │     ├─ release should be automatic       → pg_advisory_xact_lock*   (transaction)
  │     └─ must outlive a transaction        → pg_advisory_lock*        (session)
  └─ must not wait if already locked         → pg_try_advisory_*  (returns boolean)
```

The single most important rule : PREFER transaction-level locks
(`pg_advisory_xact_lock`). They auto-release at transaction end, including on crash or
rollback, so they cannot leak. Session-level locks need a disciplined `unlock` and leak
until the connection closes if you forget.

## When To Use This Skill :

ALWAYS use this skill when :
- A scheduled job must run at most once across multiple application instances
- Building a distributed mutex or critical section coordinated through PostgreSQL
- Coordinating queue workers so two workers never process the same logical unit
- Serializing an expensive or external operation that has no natural row to lock
- A `pg_advisory_lock` appears stuck, leaked, or behaves wrongly behind a pooler

NEVER use this skill for :
- Locking an actual table row before update (use `SELECT ... FOR UPDATE`)
- Deadlock diagnosis on regular locks (use postgres-errors-deadlocks)
- Reading or interpreting `EXPLAIN` plans and lock waits (use postgres-impl-query-performance-toolkit)
- Scheduling the job itself (use the pg_cron skill or an external scheduler)

## Decision Trees :

### Session-level vs transaction-level :

```
how should the lock be released?
  ├─ at the end of the current transaction, automatically
  │     → pg_advisory_xact_lock(key)        no unlock call, cannot leak
  │
  └─ it must survive across transactions, released by explicit call
        → pg_advisory_lock(key)             MUST pair with pg_advisory_unlock(key)
        → risk: forget the unlock = lock held until the session disconnects

default choice: transaction-level. Pick session-level only when the protected
work genuinely spans multiple transactions.
```

### Blocking vs non-blocking :

```
what if the lock is already held by someone else?
  ├─ wait for it (block until acquired)
  │     → pg_advisory_lock(key) / pg_advisory_xact_lock(key)
  │
  └─ do not wait, decide immediately from the result
        → pg_try_advisory_lock(key) / pg_try_advisory_xact_lock(key)
        → returns true  = you got it, proceed
        → returns false = someone else has it, skip / retry later
```

Cron-at-most-once and "skip if busy" ALWAYS use the `try` variant: a blocking lock
would just queue every instance and run the job N times in sequence.

### Single key vs two-key namespace :

```
is there any chance another subsystem picks the same integer?
  ├─ no, the key space is fully yours    → pg_advisory_lock(bigint_key)
  └─ yes, multiple subsystems share a DB  → pg_advisory_lock(classid, objid)
        classid = a constant per subsystem (e.g. 42 = "nightly-jobs")
        objid   = the specific resource id within that subsystem

the single-bigint key space and the two-int4 key space NEVER overlap, so a
two-key lock can never collide with a one-key lock.
```

### Advisory lock vs row lock :

```
is there a real table row that represents the thing you are protecting?
  ├─ YES → SELECT ... FOR UPDATE on that row. The lock follows the data,
  │        survives correctly, and is visible to anyone reading the row.
  └─ NO  → advisory lock. There is nothing else to lock onto.

NEVER use an advisory lock as a substitute for a row lock: the advisory key is
invisible to code that only looks at the row, so the two guards do not see
each other.
```

## Patterns :

### Pattern 1 : Cron job at most once across instances

ALWAYS guard a scheduled job with `pg_try_advisory_lock` (or the xact variant) so that
only one of N application instances actually runs it.
NEVER use a blocking `pg_advisory_lock` here: every instance would queue and the job
would run N times back to back.

```sql
-- each instance runs this; only the first to arrive gets true
SELECT pg_try_advisory_lock(42, 1001) AS got_lock;
-- got_lock = true  -> run the job, then pg_advisory_unlock(42, 1001)
-- got_lock = false -> another instance is running it, skip this tick
```

WHY : `pg_try_advisory_lock` returns immediately with a boolean. The instance that gets
`true` runs the job; the others see `false` and skip. The job runs exactly once per
tick regardless of how many instances are scheduled.

### Pattern 2 : Distributed mutex with automatic release

ALWAYS prefer the transaction-level lock for a critical section that fits inside one
transaction; it releases automatically even if the transaction aborts.
NEVER leave a session-level mutex unreleased on an error path.

```sql
BEGIN;
SELECT pg_advisory_xact_lock(hashtext('rebuild-search-index'));
-- critical section: only one session at a time reaches here
-- ... do the work ...
COMMIT;   -- lock released here, automatically, even on ROLLBACK or crash
```

WHY : `pg_advisory_xact_lock` is bound to the transaction. The end of the transaction,
by `COMMIT`, `ROLLBACK`, or backend crash, releases it. There is no unlock call to
forget and therefore no leak path. `hashtext(text)` turns a readable name into the
required integer key.

### Pattern 3 : Queue workers with SKIP LOCKED

ALWAYS combine `SELECT ... FOR UPDATE SKIP LOCKED` for claiming queue rows; reach for
an advisory lock only when the unit of work has no row.
NEVER spin two guards on the same queue: pick row locks OR advisory locks per unit.

```sql
-- claim one job row without blocking on rows other workers hold
BEGIN;
SELECT id, payload FROM job_queue
WHERE state = 'pending'
ORDER BY id
FOR UPDATE SKIP LOCKED
LIMIT 1;
-- process it, UPDATE state = 'done', COMMIT
```

WHY : `SKIP LOCKED` lets each worker grab a different unclaimed row with no contention.
Use a transaction-level advisory lock instead only when the work is keyed by something
with no row, for example "process tenant 1234" where tenants are not a queue table.

### Pattern 4 : Two-key namespacing to avoid collisions

ALWAYS use the two-integer form `(classid, objid)` when more than one subsystem takes
advisory locks in the same database.
NEVER let two unrelated subsystems pick raw integers from the same single-key space.

```sql
-- subsystem A: classid 100 = "billing"
SELECT pg_advisory_xact_lock(100, invoice_id);
-- subsystem B: classid 200 = "reports"
SELECT pg_advisory_xact_lock(200, report_id);
-- invoice_id and report_id may be equal; the classid keeps them distinct
```

WHY : the first integer is a constant namespace per subsystem, the second is the
resource id within it. Two subsystems can use overlapping resource ids without
colliding. The two-key space is also fully separate from the single-bigint space.

### Pattern 5 : Inspecting held advisory locks

ALWAYS query `pg_locks` with `locktype = 'advisory'` to see which keys are held and by
which backend when debugging a stuck coordination path.

```sql
SELECT pl.pid, pl.classid, pl.objid, pl.objsubid, pl.mode, pl.granted,
       psa.state, psa.application_name, psa.query_start
FROM pg_locks pl
JOIN pg_stat_activity psa ON psa.pid = pl.pid
WHERE pl.locktype = 'advisory';
```

WHY : every advisory lock is visible in `pg_locks`. `objsubid = 1` means a single-key
`bigint` lock (split across `classid`/`objid`), `objsubid = 2` means a two-int4 lock.
`granted = false` rows are sessions waiting. Joining `pg_stat_activity` shows who holds
or waits and whether that backend is still doing useful work.

### Pattern 6 : Non-blocking acquire with explicit release

ALWAYS pair every session-level `pg_advisory_lock` / `pg_try_advisory_lock` with exactly
one `pg_advisory_unlock` on every code path, including error paths.
NEVER acquire a session lock inside a `SELECT ... LIMIT` over multiple rows.

```sql
SELECT pg_try_advisory_lock(771) AS got;
-- if got = true:
--   ... do the multi-transaction work ...
SELECT pg_advisory_unlock(771);   -- release on success AND on every failure path
```

WHY : a session lock is held until unlocked or the connection ends. Acquiring it inside
`SELECT pg_advisory_lock(id) FROM foo WHERE id > 1 LIMIT 100` is unsafe: `LIMIT` is not
guaranteed to apply before the locking function runs, so locks on unexpected rows are
acquired and then dangle. Lock single, known keys; wrap row sets in a subquery.

## Anti-Patterns :

(Cause, symptom, and fix for each in references/anti-patterns.md)

- AP-1 : Session-level lock never unlocked. Leaks, held until the connection closes.
- AP-2 : Advisory lock through a transaction-mode pooler. Session locks bind to the wrong backend.
- AP-3 : Key collision between unrelated subsystems sharing the single-key space.
- AP-4 : Advisory lock used where a row lock (`SELECT FOR UPDATE`) is correct.
- AP-5 : Blocking `pg_advisory_lock` for cron-at-most-once. Job runs N times in sequence.
- AP-6 : Acquiring locks inside `SELECT ... LIMIT`. Dangling locks on unexpected rows.
- AP-7 : Assuming `ROLLBACK` releases a session-level lock. It does not.

## Reference Links :

- [references/methods.md](references/methods.md) : Every advisory lock function with signature, return type, and key forms; pg_locks advisory columns
- [references/examples.md](references/examples.md) : Working coordination snippets, version-annotated
- [references/anti-patterns.md](references/anti-patterns.md) : Anti-patterns with cause, symptom, and fix-pattern

## See Also :

- postgres-errors-deadlocks : when regular locks (not advisory) deadlock, SQLSTATE 40P01
- postgres-impl-query-performance-toolkit : pg_locks + pg_blocking_pids for general lock waits
- postgres-core-configuration : `max_locks_per_transaction` sizing for the shared lock pool
- Vooronderzoek section : §21 (PROPOSE-ADD advisory-locks)
- Official docs : https://www.postgresql.org/docs/17/explicit-locking.html#ADVISORY-LOCKS
