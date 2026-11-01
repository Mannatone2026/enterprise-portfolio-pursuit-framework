# Anti-Patterns : Advisory Locks

Each entry : cause, symptom, and fix-pattern. Advisory locks raise almost no errors;
misuse surfaces as a leaked lock, a job that runs the wrong number of times, or a
deadlock with regular locks.

## AP-1 : Session-level lock never released

CAUSE : `pg_advisory_lock(key)` or `pg_try_advisory_lock(key)` acquired, but the
matching `pg_advisory_unlock(key)` is missing on some code path, typically the error
path or an early `return`.

SYMPTOM : the key stays locked long after the work finished. Other sessions block on it
forever, or `pg_try_advisory_lock` keeps returning `false`. `pg_locks` shows the
`advisory` row held by a `pid` whose `pg_stat_activity.state` is `idle`.

FIX : prefer the transaction-level lock (`pg_advisory_xact_lock`) so release is
automatic. If a session-level lock is genuinely required, pair every acquire with
exactly one `pg_advisory_unlock` on EVERY exit path, or call `pg_advisory_unlock_all()`
in a cleanup step. A leaked lock is cleared by ending its session
(`pg_terminate_backend(pid)`).

## AP-2 : Advisory lock through a transaction-mode connection pooler

CAUSE : a session-level advisory lock acquired over a connection from a pooler running
in transaction-pooling mode (for example PgBouncer `pool_mode = transaction`).

SYMPTOM : the lock seems to vanish, or an unlock reports `false` with a warning. In
transaction mode the pooler hands each transaction a possibly different backend, so
`pg_advisory_lock` runs on one backend and the later `pg_advisory_unlock` runs on
another, which never held it.

FIX : a session-level advisory lock requires a stable session. Either use a SESSION-mode
pooled connection (or a direct, un-pooled connection) for the whole acquire-work-release
span, OR switch to a transaction-level lock (`pg_advisory_xact_lock`) and keep the lock
and the work inside one transaction, which transaction-mode pooling preserves.

## AP-3 : Key collision between unrelated subsystems

CAUSE : two independent subsystems both pick raw integers from the single-`bigint` key
space and happen to choose the same value.

SYMPTOM : one subsystem mysteriously blocks on, or skips work because of, a lock the
other subsystem holds. The two have no logical relationship, so the contention looks
inexplicable.

FIX : use the two-integer form `pg_advisory_lock(classid, objid)`. Assign each
subsystem a distinct constant `classid` namespace; `objid` is the resource id within
it. Two subsystems can then reuse the same resource ids without colliding. Document the
`classid` registry so new subsystems pick a fresh namespace.

## AP-4 : Advisory lock used where a row lock is correct

CAUSE : reaching for `pg_advisory_lock` to protect a specific table row instead of
`SELECT ... FOR UPDATE`.

SYMPTOM : code that updates the row through the normal path ignores the advisory key
entirely, because the key is invisible to anyone who only reads the row. The two guards
do not see each other and the row is updated concurrently anyway.

FIX : when a real row represents the protected thing, lock the row :
`SELECT ... FROM t WHERE id = :id FOR UPDATE`. The lock travels with the data, is
visible to every reader of that row, and releases with the transaction. Reserve
advisory locks for coordination that has NO row to lock onto.

## AP-5 : Blocking pg_advisory_lock for cron-at-most-once

CAUSE : guarding a scheduled job with the blocking `pg_advisory_lock` instead of the
non-blocking `pg_try_advisory_lock`.

SYMPTOM : the job is meant to run once per tick, but every instance that calls
`pg_advisory_lock` simply QUEUES behind the holder. When the first finishes, the second
acquires and runs the job again, then the third, and so on. The job runs N times in
sequence instead of once.

FIX : use `pg_try_advisory_lock` (or `pg_try_advisory_xact_lock`). It returns `false`
immediately when the lock is taken, so the other instances skip the tick instead of
queueing. The blocking variant is for "serialize, all must eventually run", not for
"run at most once".

## AP-6 : Acquiring locks inside SELECT ... LIMIT

CAUSE : `SELECT pg_advisory_lock(id) FROM foo WHERE id > 12345 LIMIT 100`, expecting
exactly 100 locks.

SYMPTOM : locks are acquired on rows beyond the intended 100. SQL does not guarantee
`LIMIT` is applied before the locking function in the target list, so extra locks are
taken and then dangle, held until the session ends, visible in `pg_locks` with no
matching unlock.

FIX : lock single, known keys (`WHERE id = 12345`), or force the row set to resolve
first by wrapping it in a subquery :
`SELECT pg_advisory_lock(q.id) FROM (SELECT id FROM foo WHERE id > 12345 LIMIT 100) q`.

## AP-7 : Assuming ROLLBACK releases a session-level lock

CAUSE : believing a session-level advisory lock taken inside a transaction is undone
when that transaction rolls back.

SYMPTOM : after a `ROLLBACK`, the application assumes the lock is gone, but `pg_locks`
still shows it held. The next acquire attempt blocks or returns `false` unexpectedly.

FIX : session-level advisory locks do NOT honour transaction semantics. A lock acquired
in a transaction that later rolls back stays held; an unlock stays effective even if
its transaction fails. Release session-level locks with an explicit
`pg_advisory_unlock`, independent of transaction outcome. If you want
transaction-bound release, use `pg_advisory_xact_lock` instead.
