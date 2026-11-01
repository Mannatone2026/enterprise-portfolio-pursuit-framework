# anti-patterns : LISTEN / NOTIFY

Each entry : cause, symptom, fix-pattern. Verified against PostgreSQL 17
documentation. Behaviour is identical on 15, 16, and 17.

## AP-1 : LISTEN through a transaction-mode PgBouncer pool

CAUSE : the listening session connects through PgBouncer (or any pooler) in
transaction mode or statement mode. That mode returns the server connection
to the pool at every `COMMIT`/`ROLLBACK`.

SYMPTOM : `LISTEN` appears to succeed, but notifications never arrive, or
arrive only sometimes. `LISTEN` is session state bound to one physical
server connection; after the next transaction the listener is silently
attached to a different server connection that never ran `LISTEN`.

FIX-PATTERN : run the listening session on a **dedicated connection that
bypasses the transaction pooler** : either a direct connection to
PostgreSQL, or a connection through a session-mode pool (which holds one
server connection per client for the whole session). `NOTIFY` senders may
keep using the transaction-mode pool.

## AP-2 : Treating NOTIFY as guaranteed delivery

CAUSE : assuming every consumer will receive every `NOTIFY`, the way a
durable message queue guarantees delivery.

SYMPTOM : a consumer that was disconnected, restarting, or not yet listening
when the `NOTIFY` committed never learns about that event. Notifications are
not stored and are never replayed.

FIX-PATTERN : if missed events are unacceptable, store the work or change in
a durable table and use `NOTIFY` only as a low-latency wakeup. On every
wakeup, and once on startup, the consumer reads the table (for example with
`SELECT ... FOR UPDATE SKIP LOCKED`) so it cannot miss a row even if it
missed the notification.

## AP-3 : Payload larger than 8000 bytes

CAUSE : packing row data, a JSON document, or a large blob into the `NOTIFY`
payload.

SYMPTOM : the `NOTIFY` fails because the payload exceeds the 8000-byte limit
of the default configuration.

FIX-PATTERN : send only a short key or id in the payload and let the
listener query the current row. This also avoids delivering stale data : by
the time the listener reacts, the row may have changed again.

## AP-4 : Expecting a notification from a rolled-back transaction

CAUSE : running `NOTIFY` in a transaction and assuming the signal goes out
immediately, regardless of the transaction outcome.

SYMPTOM : when the transaction rolls back, no notification is sent, and the
listener never learns about a change that indeed did not happen. When it
commits, the notification is delayed until commit, not sent at the `NOTIFY`
statement.

FIX-PATTERN : this is correct, intended behaviour : delivery is bound to
commit, so listeners only ever see committed changes. Design around it :
keep transactions that issue `NOTIFY` short so the commit-time delivery is
prompt, and never rely on the notification firing before commit.

## AP-5 : A LISTEN session stuck in a long-running transaction

CAUSE : a session runs `LISTEN` and then holds a transaction open for a long
time (a slow batch job, an idle-in-transaction connection).

SYMPTOM : the server cannot clean up the shared notification queue past that
session's position. Once the queue is half full the server logs warnings
naming the session; once it is full, every transaction that calls `NOTIFY`
**fails at commit** for the whole cluster.

FIX-PATTERN : keep the listening connection's transactions short : after
`LISTEN`, do not hold a long transaction on that connection. Use autocommit
on the listener, end idle-in-transaction sessions, and monitor with
`pg_notification_queue_usage()`.

## AP-6 : NOTIFY in a transaction that must be prepared (2PC)

CAUSE : a transaction calls `NOTIFY` (directly or through a trigger) and is
then prepared with `PREPARE TRANSACTION` for two-phase commit.

SYMPTOM : `PREPARE TRANSACTION` fails : a transaction that has executed
`NOTIFY` cannot be prepared.

FIX-PATTERN : do not emit notifications inside a transaction that will be
distributed via two-phase commit. Send the `NOTIFY` from a separate, locally
committed transaction after the prepared transaction is fully committed.

## AP-7 : Sharing the listener connection with normal query traffic

CAUSE : reusing the connection that holds the `LISTEN` to also run
application queries and transactions.

SYMPTOM : notifications that arrive while that connection is mid-transaction
are not delivered to the client until the transaction completes, so events
are delayed or batched unpredictably; a borrowed connection may also be
handed elsewhere, dropping the registration.

FIX-PATTERN : dedicate one long-lived connection to listening only. Run all
other queries on separate connections (the pool). On each notification, the
listener may open its own short transaction to fetch state, then return to
waiting.

## Sources

- https://www.postgresql.org/docs/17/sql-notify.html
- https://www.postgresql.org/docs/17/sql-listen.html
