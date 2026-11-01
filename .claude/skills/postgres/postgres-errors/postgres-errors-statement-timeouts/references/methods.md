# Methods : Statement and Session Timeouts

API surface for the five PostgreSQL timeout GUCs and the related SQLSTATE
codes. Verified against PostgreSQL 17 official documentation.

## Timeout GUC parameters

Source : https://www.postgresql.org/docs/17/runtime-config-client.html

All five accept a time value ; a bare number is milliseconds. `0` disables.
All five default to `0`.

### statement_timeout

> "Abort any statement that takes more than the specified amount of time. ... A
> value of zero (the default) disables the timeout."

> "The timeout is measured from the time a command arrives at the server until
> it is completed by the server. If multiple SQL statements appear in a single
> simple-query message, the timeout is applied to each statement separately."

- Bounds : total execution time of one statement (waiting + running).
- On limit : the statement is aborted. SQLSTATE `57014 query_canceled`,
  message "canceling statement due to statement timeout".
- Available : all supported versions.

### lock_timeout

> "Abort any statement that waits longer than the specified amount of time
> while attempting to acquire a lock on a table, index, row, or other database
> object. The time limit applies separately to each lock acquisition attempt.
> ... A value of zero (the default) disables the timeout."

> "Unlike statement_timeout, this timeout can only occur while waiting for
> locks. ... if statement_timeout is nonzero, it is rather pointless to set
> lock_timeout to the same or larger value, since the statement timeout would
> always trigger first."

- Bounds : time spent WAITING for a lock, per acquisition attempt. Does NOT
  bound execution.
- On limit : the statement is aborted. SQLSTATE `55P03 lock_not_available`,
  message "canceling statement due to lock timeout".
- Applies to explicit (`LOCK TABLE`, `SELECT FOR UPDATE`) and implicit locks.
- Available : all supported versions.

### idle_in_transaction_session_timeout

> "Terminate any session that has been idle (that is, waiting for a client
> query) within an open transaction for longer than the specified amount of
> time. ... A value of zero (the default) disables the timeout."

> "This option can be used to ensure that idle sessions do not hold locks for
> an unreasonable amount of time. Even when no significant locks are held, an
> open transaction prevents vacuuming away recently-dead tuples ... so remaining
> idle for a long time can contribute to table bloat."

- Bounds : idle time while a transaction is open.
- On limit : the SESSION is terminated. SQLSTATE `25P03
  idle_in_transaction_session_timeout`, message "terminating connection due to
  idle-in-transaction timeout".
- Available : all supported versions.

### idle_session_timeout (v14+)

> "Terminate any session that has been idle (that is, waiting for a client
> query), but not within an open transaction, for longer than the specified
> amount of time. ... A value of zero (the default) disables the timeout."

> "Unlike the case with an open transaction, an idle session without a
> transaction imposes no large costs on the server, so there is less need to
> enable this timeout than idle_in_transaction_session_timeout."

- Bounds : idle time when NO transaction is open.
- On limit : the SESSION is terminated ; the client observes a class `08`
  connection error on its next use of the connection.
- Available : v14+.

### transaction_timeout (v17+)

> "Terminate any session that spans longer than the specified amount of time in
> a transaction. The limit applies both to explicit transactions (started with
> BEGIN) and to an implicitly started transaction corresponding to a single
> statement. ... A value of zero (the default) disables the timeout."

> "If transaction_timeout is shorter or equal to
> idle_in_transaction_session_timeout or statement_timeout then the longer
> timeout is ignored."

> "Prepared transactions are not subject to this timeout."

- Bounds : total wall-clock duration of a transaction, active or idle.
- On limit : the SESSION is terminated.
- Available : v17+ only.

## Precedence rules

| Combination | Result |
|-------------|--------|
| `lock_timeout` >= `statement_timeout`, both nonzero | `lock_timeout` never fires ; `statement_timeout` always wins |
| `transaction_timeout` <= `idle_in_transaction_session_timeout` | the longer `idle_in_transaction_session_timeout` is ignored |
| `transaction_timeout` <= `statement_timeout` | the longer `statement_timeout` is ignored |

Practical ordering for a web role : `lock_timeout` < `statement_timeout` <
`transaction_timeout`.

## Related SQLSTATE codes

Source : https://www.postgresql.org/docs/17/errcodes-appendix.html

| SQLSTATE | Class | Condition name | Raised by |
|----------|-------|----------------|-----------|
| `57014` | 57 Operator Intervention | `query_canceled` | `statement_timeout`, `pg_cancel_backend()`, client cancel, recovery conflict |
| `55P03` | 55 Object Not In Prerequisite State | `lock_not_available` | `lock_timeout`, `NOWAIT` clause |
| `25P03` | 25 Invalid Transaction State | `idle_in_transaction_session_timeout` | `idle_in_transaction_session_timeout` |
| `08006` | 08 Connection Exception | `connection_failure` | session terminated (idle / transaction timeout) |
| `08000` | 08 Connection Exception | `connection_exception` | generic connection-class error |
| `08003` | 08 Connection Exception | `connection_does_not_exist` | use of an already-closed connection |

`57014` is shared by several causes. Distinguish them by the message text :
- "canceling statement due to statement timeout" : `statement_timeout`
- "canceling statement due to user request" : `pg_cancel_backend()` or client
- "canceling statement due to conflict with recovery" : hot-standby replay

## Scoping a timeout

Timeouts can be set at four scopes, narrowest wins :

| Command | Scope | Applies |
|---------|-------|---------|
| `SET LOCAL name = value` | current transaction | until `COMMIT` / `ROLLBACK` |
| `SET name = value` | current session | until session ends or reset |
| `ALTER ROLE r SET name = value` | role | at every connection by role `r` |
| `ALTER DATABASE d SET name = value` | database | at every connection to `d` |
| `postgresql.conf` + reload | cluster-wide | all sessions, as the global default |

`ALTER ROLE` and `ALTER DATABASE` settings take effect at connection time and
need no application code change. `SET LOCAL` is the safe choice inside a
migration or batch transaction because it auto-reverts at transaction end.

## Inspection commands

```sql
-- current effective value and where it came from
SHOW statement_timeout;
SELECT name, setting, unit, source
FROM pg_settings
WHERE name IN ('statement_timeout', 'lock_timeout',
               'idle_in_transaction_session_timeout',
               'idle_session_timeout', 'transaction_timeout');

-- per-role and per-database overrides currently configured
SELECT rolname, rolconfig FROM pg_roles WHERE rolconfig IS NOT NULL;
SELECT datname, datconfig FROM pg_database WHERE datconfig IS NOT NULL;

-- sessions currently idle in a transaction (the leak this skill targets)
SELECT pid, usename, state,
       now() - state_change AS idle_for,
       left(query, 60) AS last_query
FROM pg_stat_activity
WHERE state = 'idle in transaction'
ORDER BY idle_for DESC;
```
