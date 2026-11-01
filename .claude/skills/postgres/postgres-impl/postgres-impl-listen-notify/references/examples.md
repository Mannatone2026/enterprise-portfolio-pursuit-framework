# examples : LISTEN / NOTIFY

Working code for postgres-impl-listen-notify. SQL is verified against
PostgreSQL 17 documentation; behaviour is identical on 15, 16, and 17.
Client-driver snippets are illustrative : confirm the exact API against the
installed driver version.

## 1. Basic LISTEN / NOTIFY (v15+)

```sql
-- Consumer session: subscribe and keep the connection open.
LISTEN cache_invalidation;

-- Producer session: broadcast on the channel.
NOTIFY cache_invalidation, 'users';
NOTIFY cache_invalidation;            -- payload is optional

-- Stop:
UNLISTEN cache_invalidation;
UNLISTEN *;                           -- unsubscribe from every channel
```

## 2. pg_notify for dynamic channel or payload (v15+)

```sql
-- NOTIFY arguments must be literals. pg_notify accepts expressions.
SELECT pg_notify('row_changed_' || 'orders', (42)::text);

-- Inside a function the channel/payload are variables, so pg_notify
-- is the only option.
CREATE FUNCTION announce(tbl text, key bigint) RETURNS void AS $$
BEGIN
  PERFORM pg_notify('change_' || tbl, key::text);
END $$ LANGUAGE plpgsql;
```

## 3. Trigger-driven change notification (v15+)

```sql
CREATE FUNCTION notify_order_change() RETURNS trigger AS $$
BEGIN
  -- Send only the key; the listener fetches the current row.
  PERFORM pg_notify('order_changed', COALESCE(NEW.id, OLD.id)::text);
  RETURN NULL;                        -- return value ignored for AFTER triggers
END $$ LANGUAGE plpgsql;

CREATE TRIGGER trg_notify_order_change
  AFTER INSERT OR UPDATE OR DELETE ON orders
  FOR EACH ROW EXECUTE FUNCTION notify_order_change();
```

The trigger runs inside the writing transaction. The notification is queued
and delivered only when that transaction commits; a rollback sends nothing.

## 4. NOTIFY as a wakeup for a durable queue (v15+)

When delivery must be guaranteed, store work in a table and use `NOTIFY`
only to wake workers immediately instead of polling on a timer.

```sql
-- Durable queue table.
CREATE TABLE job_queue (
  id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  payload     jsonb NOT NULL,
  created_at  timestamptz NOT NULL DEFAULT now()
);

-- Enqueue + wake workers, atomically in one transaction.
CREATE FUNCTION enqueue_job(p jsonb) RETURNS void AS $$
BEGIN
  INSERT INTO job_queue (payload) VALUES (p);
  PERFORM pg_notify('job_queue', 'new');
END $$ LANGUAGE plpgsql;

-- Worker claims a job (skips rows another worker already holds).
-- Run this after every 'job_queue' notification AND once on startup,
-- because a notification sent while the worker was offline is lost.
DELETE FROM job_queue
WHERE id = (
  SELECT id FROM job_queue
  ORDER BY id
  FOR UPDATE SKIP LOCKED
  LIMIT 1
)
RETURNING id, payload;
```

## 5. Consume notifications : node-postgres (illustrative)

```javascript
const { Client } = require('pg');

// A dedicated direct Client, NOT a pooled connection.
const client = new Client({ connectionString: process.env.DIRECT_DB_URL });

await client.connect();
client.on('notification', async (msg) => {
  // msg.channel === 'order_changed', msg.payload === the id
  const { rows } = await client.query(
    'SELECT * FROM orders WHERE id = $1', [msg.payload]);
  refreshCache(rows[0]);
});
await client.query('LISTEN order_changed');
// Keep the process and connection alive.
```

## 6. Consume notifications : psycopg 3 (illustrative)

```python
import psycopg

# Dedicated direct connection; autocommit so LISTEN takes effect at once.
with psycopg.connect(DIRECT_DSN, autocommit=True) as conn:
    conn.execute("LISTEN order_changed")
    # notifies() blocks and yields notifications as they arrive.
    for note in conn.notifies():
        # note.channel, note.payload (the id), note.pid
        handle_change(note.payload)
```

For libpq directly : issue `LISTEN` as an ordinary SQL command, then poll
the socket and call `PQnotifies` to collect events.

## 7. Filter out self-notifications (v15+)

```sql
-- The notification event carries the notifying backend PID.
-- Compare it client-side with this session's PID:
SELECT pg_backend_pid();
-- If the event PID equals this value, the notification is your own
-- write echoing back and can be ignored.
```

## 8. Monitor the notification queue (v15+)

```sql
-- Fraction (0..1) of the async notification queue in use.
-- A rising value points to a listener stuck in a long transaction.
SELECT pg_notification_queue_usage();
```

## Sources

- https://www.postgresql.org/docs/17/sql-notify.html
- https://www.postgresql.org/docs/17/sql-listen.html
- https://www.postgresql.org/docs/17/sql-unlisten.html
