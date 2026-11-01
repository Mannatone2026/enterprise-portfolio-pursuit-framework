# methods : LISTEN / NOTIFY / pg_notify

API surface for postgres-impl-listen-notify. Verified against PostgreSQL 17
official documentation (sql-notify.html, sql-listen.html, sql-unlisten.html).
Behaviour is identical on 15, 16, and 17.

## NOTIFY command

```
NOTIFY channel [ , payload ]
```

| Argument | Rule |
|----------|------|
| `channel` | Any identifier. The notification channel name. Quoted and unquoted identifier rules apply. |
| `payload` | Optional. Must be a **simple string literal**. In the default configuration it must be shorter than **8000 bytes**. |

`NOTIFY` sends an asynchronous notification, with the optional payload, to
every session currently listening on `channel`. No channel registration is
required to send.

## LISTEN command

```
LISTEN channel
```

- Registers the **current session** as a listener on `channel`.
- A session listens until it issues `UNLISTEN` or the session ends.
- Listen registrations are cleared automatically when the session ends.
- `LISTEN` takes effect at transaction commit. If `LISTEN` or `UNLISTEN`
  runs in a transaction that later rolls back, the set of listened channels
  is unchanged.
- There is no replay : a session receives only notifications committed
  after it is listening. Past notifications are never re-delivered.

## UNLISTEN command

```
UNLISTEN channel      -- stop listening on one channel
UNLISTEN *            -- stop listening on all channels
```

`UNLISTEN` of a channel the session is not listening on is harmless.

## pg_notify function

```
pg_notify(text, text)   ->   void
```

| Position | Meaning |
|----------|---------|
| 1st argument | channel name |
| 2nd argument | payload |

`pg_notify` is the function-call equivalent of the `NOTIFY` command. Use it
when the channel name or payload is **not a constant string literal**, for
example inside a PL/pgSQL trigger or function, where `NOTIFY`'s
literal-only arguments cannot be used.

```sql
SELECT pg_notify('foo', 'payload');
```

## pg_notification_queue_usage function

```
pg_notification_queue_usage()   ->   double precision
```

Returns the fraction (0 to 1) of the asynchronous notification queue
currently occupied by pending notifications. Use it to monitor for a
listener that is blocking queue cleanup.

## Delivery semantics

| Property | Behaviour |
|----------|-----------|
| Commit-bound | A `NOTIFY` inside a transaction is queued; the events are delivered only if and when the transaction commits. |
| Rollback | If the transaction aborts, all its commands have had no effect, including `NOTIFY` : nothing is sent. |
| Listener mid-transaction | If a listening session is itself inside a transaction when a signal arrives, the event is not delivered to its client until just after that transaction completes (commit or abort). |
| Self-notification | A session that runs `NOTIFY` while listening on the same channel receives its own notification, like any other listener. Filter by comparing the notifying backend PID (carried in the event) with the local backend PID. |
| Deduplication | If the same channel is signaled multiple times with **identical** payload strings **within the same transaction**, only one event is delivered. Distinct payloads are always delivered separately. Notifications from different transactions are never folded. |
| Ordering (same transaction) | Notifications from one transaction are delivered in the order they were sent. |
| Ordering (across transactions) | Notifications from different transactions are delivered in the order the transactions committed. |

## Notification queue limits

- The server holds sent-but-not-yet-processed notifications in a queue,
  about **8 GB** in a standard installation.
- If the queue becomes full, transactions that call `NOTIFY` **fail at
  commit**.
- A session that runs `LISTEN` and then stays in a very long transaction
  prevents queue cleanup. Once the queue is half full, the server logs
  warnings naming the session that blocks cleanup.
- A transaction that has executed `NOTIFY` **cannot be prepared** for
  two-phase commit (`PREPARE TRANSACTION`).

## Client-side reception

The mechanism depends on the client interface :

- **libpq** : the application issues `LISTEN` as an ordinary SQL command,
  then periodically calls `PQnotifies` to collect received notification
  events. Reception is poll-based on the socket; the events themselves are
  pushed asynchronously by the server.
- **Higher-level drivers** wrap this : node-postgres emits a `notification`
  event on the `Client`; psycopg exposes received notifications through a
  generator / list on the connection. Driver APIs evolve, so confirm the
  exact call against the installed driver version.

Each notification event carries : the channel name, the payload, and the
PID of the notifying server process.

## Sources

- https://www.postgresql.org/docs/17/sql-notify.html
- https://www.postgresql.org/docs/17/sql-listen.html
- https://www.postgresql.org/docs/17/sql-unlisten.html
