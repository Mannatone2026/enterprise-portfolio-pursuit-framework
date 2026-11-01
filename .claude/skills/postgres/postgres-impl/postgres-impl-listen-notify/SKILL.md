---
name: postgres-impl-listen-notify
description: >
  Use when building cache invalidation, job-queue wakeups, or real-time change notifications between PostgreSQL clients without polling.
  Prevents lost notifications through a transaction-mode PgBouncer pool, relying on NOTIFY for guaranteed delivery (no replay), and payloads over the 8000-byte limit.
  Covers LISTEN / NOTIFY / pg_notify, UNLISTEN, commit-time delivery semantics, payload size limit, session-bound no-replay behavior, trigger-driven notification pattern, PgBouncer pooling incompatibility, client-side notification handling.
  Keywords: LISTEN, NOTIFY, pg_notify, UNLISTEN, pub sub, notification, real-time, cache invalidation, job queue wakeup, PgBouncer, notifications not arriving, listen notify not working with pgbouncer, payload too long, how to push changes to clients
license: MIT
compatibility: "Designed for Claude Code. Requires PostgreSQL 15, 16, or 17."
metadata:
  author: OpenAEC-Foundation
  version: "1.0"
---

# postgres-impl-listen-notify

## Quick Reference :

`LISTEN` / `NOTIFY` is PostgreSQL's built-in publish-subscribe channel. A
session runs `LISTEN channel` to subscribe; any session runs `NOTIFY
channel, 'payload'` (or `pg_notify('channel', 'payload')`) to broadcast. The
server delivers the payload asynchronously to every listening session.

It is a signalling mechanism, not a message queue. Three facts decide every
design :

1. **Delivery is at COMMIT.** A `NOTIFY` inside a transaction is queued and
   sent only when that transaction commits; a rollback sends nothing.
2. **There is no replay.** A `LISTEN` is per-connection. A client that is
   not connected and listening at the moment of the `NOTIFY` misses the
   event forever. Notifications are never stored or re-sent.
3. **The payload is small.** A payload must be a string literal under
   8000 bytes. Send a row id, not the row.

Core rules :

- ALWAYS use a dedicated, direct (non-pooled, or session-mode) connection
  for the listening session : transaction-mode PgBouncer breaks `LISTEN`.
- ALWAYS treat `NOTIFY` as a wakeup hint; if delivery must be guaranteed,
  back it with a durable queue table the listener reads after each wakeup.
- ALWAYS send a key/id in the payload and let the listener fetch the data.
- NEVER `LISTEN` and then sit in a long-running transaction : it blocks the
  server's notification-queue cleanup.

## When To Use This Skill :

ALWAYS use this skill when :
- Building cache invalidation, real-time UI updates, or job-queue wakeups
  that should not poll the database on a timer.
- Writing an `AFTER INSERT/UPDATE/DELETE` trigger that broadcasts changes.
- Notifications "are not arriving" or stop working after adding PgBouncer.
- A `NOTIFY` fails because the payload is too large.

NEVER use this skill for :
- Guaranteed, durable, replayable delivery : use a queue table (with
  `SELECT ... FOR UPDATE SKIP LOCKED`) or an external broker.
- Logical change streaming to other databases : use logical replication
  (postgres-impl-logical-replication).
- Cross-process locking / coordination : use advisory locks.

## Decision Trees :

### LISTEN/NOTIFY or a durable queue? :

```
Must every consumer receive every event, even after a disconnect?
├── YES -> NOTIFY alone is wrong (no replay).
│         Use a durable queue table; NOTIFY only as a low-latency wakeup
│         so workers poll the table immediately instead of on a timer.
└── NO  -> A missed event is acceptable (cache invalidation, UI refresh).
          Plain LISTEN/NOTIFY is enough. On (re)connect, the listener
          first reads current state, then relies on notifications.
```

### Connecting through a pooler :

```
Does the app connect through PgBouncer (or another transaction pooler)?
├── NO  -> direct connection: LISTEN works normally.
├── YES, pool mode = session  -> LISTEN works (connection is stable).
└── YES, pool mode = transaction or statement
        -> LISTEN BREAKS: the server connection is returned to the pool
           at COMMIT, so the listener loses its registration.
        -> Open a SEPARATE direct connection (bypass the pooler) for the
           listening session. NOTIFY senders may still use the pool.
```

### Where does the NOTIFY belong? :

```
What triggers the notification?
├── A specific application action -> NOTIFY / pg_notify in application code,
│      in the same transaction as the change, so it commits atomically.
└── Any write to a table, regardless of source
       -> AFTER INSERT/UPDATE/DELETE trigger calling pg_notify().
          Catches changes from every client, including manual SQL.
```

## Patterns :

### Pattern 1 : Basic LISTEN and NOTIFY

ALWAYS : subscribe with `LISTEN` on the connection that will consume events,
and keep that connection open.
NEVER : expect a notification sent before the `LISTEN` committed to arrive.

```sql
-- Consumer session:
LISTEN cache_invalidation;

-- Producer session (any connection):
NOTIFY cache_invalidation, 'users';

-- Stop listening:
UNLISTEN cache_invalidation;   -- one channel
UNLISTEN *;                    -- all channels
```

WHY : `LISTEN` registers the current session as a listener and takes effect
at transaction commit; the registration is cleared automatically when the
session ends. A session must already be listening when the `NOTIFY` commits,
because past notifications are never replayed.

### Pattern 2 : pg_notify for dynamic channels

ALWAYS : use `pg_notify(channel, payload)` when the channel name or payload
is not a fixed literal, for example inside a trigger or a function.
NEVER : try to interpolate a variable into the `NOTIFY` command string.

```sql
-- NOTIFY needs literal arguments; pg_notify takes ordinary expressions.
SELECT pg_notify('row_changed_' || 'orders', '42');
```

WHY : `NOTIFY` requires its channel and payload to be string literals.
`pg_notify(text, text)` is the function equivalent and accepts any
expression, which is what makes it usable from PL/pgSQL triggers.

### Pattern 3 : Trigger-driven change notifications

ALWAYS : broadcast row changes with an `AFTER` row-level trigger that calls
`pg_notify`, so changes from every client are caught.
NEVER : put the `pg_notify` in a `BEFORE` trigger or rely only on
application code (it misses manual SQL and other services).

```sql
CREATE FUNCTION notify_order_change() RETURNS trigger AS $$
BEGIN
  -- Send only the id; the listener fetches the row itself.
  PERFORM pg_notify('order_changed',
                    COALESCE(NEW.id, OLD.id)::text);
  RETURN NULL;                    -- AFTER trigger ignores the return value
END $$ LANGUAGE plpgsql;

CREATE TRIGGER trg_notify_order_change
  AFTER INSERT OR UPDATE OR DELETE ON orders
  FOR EACH ROW EXECUTE FUNCTION notify_order_change();
```

WHY : the trigger fires inside the writing transaction, so the notification
is queued and delivered exactly when (and only if) that transaction commits.
A rolled-back change sends nothing, which keeps listeners consistent.

### Pattern 4 : Send a key, never the data

ALWAYS : put a short identifier in the payload and let the listener query
the current row.
NEVER : pack row data or JSON documents into the payload : the limit is
8000 bytes and a `NOTIFY` over it fails.

```sql
-- Good: a key well under the limit.
SELECT pg_notify('order_changed', '42');

-- Bad: a full document risks exceeding 8000 bytes and is stale on arrival.
-- SELECT pg_notify('order_changed', row_to_json(o)::text);
```

WHY : the payload must be a string literal shorter than 8000 bytes in the
default configuration. Sending only the key also avoids delivering stale
data : by the time the listener reacts, the row may have changed again, so
it should read the latest state itself.

### Pattern 5 : Dedicated listener connection

ALWAYS : give the listening session its own long-lived connection, direct or
through a session-mode pool.
NEVER : run `LISTEN` on a connection borrowed from a transaction-mode pool.

```text
Producers (web requests, jobs) ---> PgBouncer (transaction mode) ---> Postgres
Listener process              ---> direct connection (no pooler) ---> Postgres
                                   LISTEN order_changed;  (held open)
```

WHY : a transaction-mode pooler returns the server connection to the pool at
every `COMMIT`/`ROLLBACK`. `LISTEN` is session state bound to one physical
server connection, so after the next transaction the listener is silently no
longer registered. A direct or session-mode connection stays bound, so the
registration survives.

### Pattern 6 : Consume notifications in the client

ALWAYS : keep one connection open and let the driver hand you notifications
asynchronously or via a blocking wait; on each wakeup, read current state.
NEVER : busy-poll the connection in a tight loop.

```javascript
// node-postgres: a dedicated Client (not a pooled connection).
const client = new Client({ /* direct connection */ });
await client.connect();
client.on('notification', (msg) => {
  // msg.channel, msg.payload -> fetch the row, refresh cache, wake a worker.
});
await client.query('LISTEN order_changed');
```

WHY : the server pushes notifications, but client libraries surface them
either as an event or through a blocking wait on the socket. The connection
must stay open and dedicated to listening; sharing it with normal query
traffic risks transactions that delay or hide notifications. See
references/examples.md for psycopg and libpq equivalents.

## Anti-Patterns :

(Full cause + symptom + fix in references/anti-patterns.md)

- `LISTEN` through a transaction-mode PgBouncer pool : registration lost,
  notifications silently never arrive.
- Treating `NOTIFY` as guaranteed delivery : no replay, a disconnected
  client misses events permanently.
- Payload over 8000 bytes : the `NOTIFY` fails.
- Expecting a notification from a rolled-back transaction : nothing is sent.
- A `LISTEN` session held inside a long-running transaction : blocks
  notification-queue cleanup; once full, every `NOTIFY` fails at commit.
- `NOTIFY` in a transaction that is then prepared for two-phase commit :
  the transaction cannot be prepared.

## Reference Links :

- [references/methods.md](references/methods.md) : LISTEN / NOTIFY / UNLISTEN
  / pg_notify syntax, delivery + ordering semantics, queue limits.
- [references/examples.md](references/examples.md) : Working SQL and
  client-side samples (node-postgres, psycopg, libpq), version-annotated.
- [references/anti-patterns.md](references/anti-patterns.md) : Anti-patterns
  with cause, symptom, fix-pattern.

## See Also :

- postgres-impl-logical-replication : durable, replayable change streaming
  when missed events are unacceptable.
- postgres-impl-advisory-locks : cross-process coordination primitives.
- postgres-impl-vacuum-bloat : long-running transactions also starve VACUUM,
  the same root cause as a blocked notification queue.
- Vooronderzoek section : §21 (PROPOSE-ADD postgres-impl-listen-notify),
  §13 (PgBouncer transaction-mode session-state incompatibility).
- Official docs : https://www.postgresql.org/docs/17/sql-notify.html ,
  https://www.postgresql.org/docs/17/sql-listen.html
