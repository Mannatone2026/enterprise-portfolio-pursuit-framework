# Methods : Advisory Locks

Complete advisory lock function surface, verified against PostgreSQL 17 official
documentation. The behaviour and signatures are identical in PostgreSQL 15, 16, and 17.

## Key forms :

Every advisory lock function accepts the key in one of two forms :

| Form | Signature shape | Use |
|------|-----------------|-----|
| Single key | `(key bigint)` | One subsystem owns the whole key space. |
| Two-key | `(key1 integer, key2 integer)` | `key1` = subsystem namespace, `key2` = resource id. |

The single-`bigint` key space and the two-`integer` key space NEVER overlap: a lock on
`(bigint) 5` and a lock on `(int, int) 0, 5` are different locks. Keys are raw
integers; turn a readable name into a key with `hashtext('name')` (returns `integer`)
or `hashtextextended('name', 0)` (returns `bigint`).

## Session-level functions :

A session-level lock is held until explicitly released OR the session ends. It does
NOT honour transaction semantics: a lock taken inside a transaction that later rolls
back is still held, and an unlock stays effective even if its transaction later fails.

| Function | Key forms | Returns | Description |
|----------|-----------|---------|-------------|
| `pg_advisory_lock(key)` | bigint / int,int | `void` | Obtains an exclusive session-level lock, waiting if necessary. |
| `pg_advisory_lock_shared(key)` | bigint / int,int | `void` | Obtains a shared session-level lock, waiting if necessary. |
| `pg_try_advisory_lock(key)` | bigint / int,int | `boolean` | Exclusive session-level lock if free; returns `true`/`false` immediately, never waits. |
| `pg_try_advisory_lock_shared(key)` | bigint / int,int | `boolean` | Shared session-level lock if free; returns immediately. |
| `pg_advisory_unlock(key)` | bigint / int,int | `boolean` | Releases an exclusive session-level lock. `true` on success; `false` + SQL warning if the lock was not held. |
| `pg_advisory_unlock_shared(key)` | bigint / int,int | `boolean` | Releases a shared session-level lock. Same return semantics as above. |
| `pg_advisory_unlock_all()` | none | `void` | Releases all session-level advisory locks held by the current session. Invoked implicitly at session end, even on ungraceful disconnect. |

## Transaction-level functions :

A transaction-level lock is released automatically at the end of the current
transaction (`COMMIT`, `ROLLBACK`, or backend exit). There is NO unlock function and
no way to release it earlier. This is the safer default for short-term locking.

| Function | Key forms | Returns | Description |
|----------|-----------|---------|-------------|
| `pg_advisory_xact_lock(key)` | bigint / int,int | `void` | Obtains an exclusive transaction-level lock, waiting if necessary. |
| `pg_advisory_xact_lock_shared(key)` | bigint / int,int | `void` | Obtains a shared transaction-level lock, waiting if necessary. |
| `pg_try_advisory_xact_lock(key)` | bigint / int,int | `boolean` | Exclusive transaction-level lock if free; returns immediately, never waits. |
| `pg_try_advisory_xact_lock_shared(key)` | bigint / int,int | `boolean` | Shared transaction-level lock if free; returns immediately. |

There is no `pg_advisory_xact_unlock`. To release a transaction-level lock you end the
transaction.

## Lock semantics :

### Exclusive vs shared :

- An EXCLUSIVE lock conflicts with every other lock (exclusive or shared) on the same
  key.
- A SHARED lock conflicts only with an exclusive lock on the same key. Multiple shared
  holders of one key coexist.
- Use shared for "many readers, one exclusive maintainer" coordination; use exclusive
  (the default `pg_advisory_lock`) for a plain mutex.

### Stacking (re-entrancy) :

A session-level lock can be acquired multiple times by the same owning process. Each
successful acquire must be matched by one unlock before the lock is actually released.
If a session already holds a key, further requests by that same session always succeed
immediately, even while other sessions wait, and regardless of whether the existing
hold and the new request are session-level or transaction-level.

### Shared lock-memory pool :

Advisory locks and regular locks share one shared-memory pool sized by
`max_locks_per_transaction` x (`max_connections` + prepared transactions). Exhausting
it makes the server unable to grant ANY lock. The practical ceiling on concurrent
advisory locks is in the tens to hundreds of thousands.

## Inspecting advisory locks in pg_locks :

Advisory locks appear in `pg_locks` with `locktype = 'advisory'`.

| Column | Meaning for an advisory lock |
|--------|------------------------------|
| `locktype` | Always `'advisory'` for these rows. |
| `classid` | High-order half of the key (or `key1` of a two-key lock). |
| `objid` | Low-order half of the key (or `key2` of a two-key lock). |
| `objsubid` | `1` = single-`bigint` key; `2` = two-`integer` key. |
| `pid` | Backend process holding or awaiting the lock. |
| `mode` | `ExclusiveLock` or `ShareLock`. |
| `granted` | `true` = held; `false` = the backend is waiting for it. |

Join `pg_locks` to `pg_stat_activity` on `pid` to see which session holds or waits and
what it is doing.

## The LIMIT evaluation hazard :

The order in which SQL evaluates expressions is not guaranteed to apply `LIMIT` before
a locking function in the target list. This is unsafe:

```sql
SELECT pg_advisory_lock(id) FROM foo WHERE id > 12345 LIMIT 100;  -- danger
```

Locks may be taken on rows beyond the first 100 and then dangle. The safe forms lock a
single known key, or wrap the row set in a subquery:

```sql
SELECT pg_advisory_lock(id) FROM foo WHERE id = 12345;            -- ok
SELECT pg_advisory_lock(q.id)
FROM (SELECT id FROM foo WHERE id > 12345 LIMIT 100) q;           -- ok
```

## Sources :

- https://www.postgresql.org/docs/17/explicit-locking.html (13.3.5 Advisory Locks)
- https://www.postgresql.org/docs/17/functions-admin.html (9.28.10 Advisory Lock Functions)
- https://www.postgresql.org/docs/17/view-pg-locks.html
