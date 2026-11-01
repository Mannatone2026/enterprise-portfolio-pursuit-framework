# Examples : Advisory Locks

Working coordination snippets. All behave identically on PostgreSQL 15, 16, and 17.

## 1. Cron job at most once across N instances :

```sql
-- every application instance runs this at the scheduled tick
SELECT pg_try_advisory_lock(42, 1001) AS got_lock;
```

```sql
-- the instance that got got_lock = true runs the job, then releases:
-- ... run the nightly job ...
SELECT pg_advisory_unlock(42, 1001);
```

Instances that get `false` simply skip this tick. classid `42` namespaces "nightly
jobs"; objid `1001` identifies this specific job.

## 2. Same, but self-releasing (transaction-level) :

```sql
BEGIN;
SELECT pg_try_advisory_xact_lock(42, 1001) AS got_lock;
-- if got_lock = true: run the job inside this transaction
-- ... job work ...
COMMIT;   -- lock released automatically, nothing to unlock, cannot leak
```

Prefer this when the whole job fits in one transaction: a crash mid-job still releases
the lock.

## 3. Distributed mutex around a critical section :

```sql
BEGIN;
-- hashtext turns a readable name into the integer key
SELECT pg_advisory_xact_lock(hashtext('rebuild-search-index'));
-- only one session at a time is past this line
-- ... rebuild work ...
COMMIT;
```

`hashtext('rebuild-search-index')` returns a stable `integer`; for a `bigint` key use
`hashtextextended('rebuild-search-index', 0)`.

## 4. Non-blocking mutex: skip if busy :

```sql
SELECT pg_try_advisory_xact_lock(hashtext('warm-cache')) AS got;
-- got = true  -> this session warms the cache
-- got = false -> another session is already doing it; do nothing
```

No `BEGIN` shown means it runs in its own implicit transaction; the lock is released
the moment that statement's transaction ends. Wrap in `BEGIN; ... COMMIT;` to hold it
across the critical section.

## 5. Session-level lock spanning multiple transactions :

```sql
-- transaction 1
SELECT pg_try_advisory_lock(771) AS got;   -- got = true: proceed

BEGIN;
-- ... step 1 ...
COMMIT;

BEGIN;
-- ... step 2 ...
COMMIT;

-- release ONLY after all transactions are done, on every path
SELECT pg_advisory_unlock(771);
```

A session-level lock is the right tool ONLY because the work spans more than one
transaction. If it fit in one, Pattern 2 would be safer.

## 6. Two-key namespacing across subsystems :

```sql
-- billing subsystem: classid 100
SELECT pg_advisory_xact_lock(100, 5);   -- lock invoice 5

-- reporting subsystem: classid 200
SELECT pg_advisory_xact_lock(200, 5);   -- lock report 5
```

Both use resource id `5` and do not collide, because `classid` differs. A single-key
`pg_advisory_xact_lock(5)` would also not collide with either: the key spaces are
separate.

## 7. Shared advisory lock: many readers, one maintainer :

```sql
-- readers take a shared lock; they do not block each other
BEGIN;
SELECT pg_advisory_xact_lock_shared(hashtext('config-table'));
-- ... read config ...
COMMIT;
```

```sql
-- the maintainer takes the exclusive lock; it waits for all readers to finish
-- and blocks new readers until it is done
BEGIN;
SELECT pg_advisory_xact_lock(hashtext('config-table'));
-- ... rewrite config ...
COMMIT;
```

## 8. Queue workers with SKIP LOCKED (the row-lock alternative) :

```sql
BEGIN;
SELECT id, payload
FROM job_queue
WHERE state = 'pending'
ORDER BY id
FOR UPDATE SKIP LOCKED
LIMIT 1;
-- process the row, then:
UPDATE job_queue SET state = 'done' WHERE id = :claimed_id;
COMMIT;
```

Each worker claims a different unlocked row. Use an advisory lock here only when the
unit of work is not a queue row, for example `pg_try_advisory_xact_lock(300, tenant_id)`
to ensure one worker per tenant.

## 9. Inspecting held advisory locks :

```sql
SELECT pl.pid,
       pl.classid,
       pl.objid,
       pl.objsubid,        -- 1 = single bigint key, 2 = two-int key
       pl.mode,            -- ExclusiveLock or ShareLock
       pl.granted,         -- false = this backend is waiting
       psa.application_name,
       psa.state,
       now() - psa.query_start AS age
FROM pg_locks pl
JOIN pg_stat_activity psa ON psa.pid = pl.pid
WHERE pl.locktype = 'advisory'
ORDER BY pl.granted, age DESC;
```

A `granted = false` row paired with a long-idle holder is a leaked session lock; see
references/anti-patterns.md AP-1.

## 10. Releasing a leaked session lock :

```sql
-- release one specific key on the current session
SELECT pg_advisory_unlock(771);

-- release every session-level advisory lock this session holds
SELECT pg_advisory_unlock_all();
```

If the leaking session is a different backend, terminate it:
`SELECT pg_terminate_backend(<pid>);` ends the session, which releases all its
advisory locks.

## 11. Safe vs unsafe locking over a row set :

```sql
-- UNSAFE: LIMIT is not guaranteed to apply before pg_advisory_lock runs
SELECT pg_advisory_lock(id) FROM foo WHERE id > 12345 LIMIT 100;

-- SAFE: lock a single known key
SELECT pg_advisory_lock(id) FROM foo WHERE id = 12345;

-- SAFE: force the LIMIT to resolve first via a subquery
SELECT pg_advisory_lock(q.id)
FROM (SELECT id FROM foo WHERE id > 12345 LIMIT 100) q;
```
