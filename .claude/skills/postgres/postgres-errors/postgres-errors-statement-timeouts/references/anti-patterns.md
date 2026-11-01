# Anti-Patterns : Statement and Session Timeouts

Each entry : the anti-pattern, its root cause, the observable symptom, the
SQLSTATE involved, and the fix pattern. Verified against PostgreSQL 17 docs.

## AP-1 : No idle_in_transaction_session_timeout

**Anti-pattern** : leaving `idle_in_transaction_session_timeout` at its `0`
default in production.

**Cause** : an application or pooled client opens a transaction (`BEGIN` or the
first statement), then stalls : slow app code, a crashed worker, a debugger
breakpoint, a hung HTTP call inside the transaction. The transaction stays open
and idle. Nothing terminates it.

**Symptom** : `VACUUM` and autovacuum cannot remove dead tuples ; tables bloat ;
the `xmin` horizon stops advancing ; locks held by the idle transaction block
other writers ; `pg_stat_activity` shows sessions stuck in `idle in
transaction` for minutes or hours.

**Fix** : set the timeout so abandoned transactions are terminated with `25P03`.

```sql
ALTER ROLE app_web SET idle_in_transaction_session_timeout = '10s';
```

The docs : an open transaction "prevents vacuuming away recently-dead tuples ...
so remaining idle for a long time can contribute to table bloat". Matches
vooronderzoek §21 (timeout GUCs).

## AP-2 : statement_timeout = 0 globally in production

**Anti-pattern** : leaving `statement_timeout` at `0` (disabled) for every role,
or explicitly setting it to `0` cluster-wide.

**Cause** : with no statement timeout, a single pathological query (a missing
`WHERE`, a cartesian join, a bad plan after stale statistics) runs until it
finishes or the connection dies.

**Symptom** : one runaway query pins CPU, buffer-pool space, and locks for
minutes ; connection-pool slots exhaust as requests pile up behind it ; the app
appears hung with no error to catch.

**Fix** : give every workload a deliberate, scoped `statement_timeout`.

```sql
ALTER ROLE app_web   SET statement_timeout = '8s';
ALTER ROLE app_batch SET statement_timeout = '30min';
```

Disable the timeout only narrowly and on purpose, via `SET LOCAL
statement_timeout = 0` around one known-long batch statement.

## AP-3 : Blindly retrying a 57014 from statement_timeout

**Anti-pattern** : catching `57014 query_canceled` and re-running the same
statement in a retry loop.

**Cause** : misreading `statement_timeout`'s `57014` as a transient failure. A
query that exceeded `statement_timeout` is genuinely too slow : a missing index,
a bad plan, or too much data. Re-running it takes the same time and times out
again.

**Symptom** : the retry loop burns the full timeout budget on every attempt ;
total latency multiplies ; the same locks and buffers are consumed N times ; the
underlying slow query is never diagnosed.

**Fix** : do not retry. Branch on the message text and treat a statement-timeout
`57014` as a fix-the-query signal.

```python
except errors.QueryCanceled as e:
    if "statement timeout" in (e.diag.message_primary or ""):
        raise RuntimeError("query too slow, add an index / fix the plan")
    raise
```

Use `EXPLAIN (ANALYZE, BUFFERS)` (with a generous `SET LOCAL statement_timeout`)
to find the cause. A transient load spike is the only case where retry helps,
and only once that cause is proven.

## AP-4 : Migration ALTER without lock_timeout

**Anti-pattern** : running `ALTER TABLE` (or any DDL needing `ACCESS EXCLUSIVE`)
against a busy table with `lock_timeout` at `0`.

**Cause** : the `ALTER` needs `ACCESS EXCLUSIVE`. If any query holds even a weak
lock on the table, the `ALTER` enters the lock queue and waits. Every NEW query
that arrives queues BEHIND the waiting `ALTER`.

**Symptom** : the whole table freezes ; reads and writes that would normally
take milliseconds stall for as long as the original blocking query runs ; a
small schema change causes a site-wide outage.

**Fix** : bound the lock wait so the DDL fails fast instead of queueing.

```sql
BEGIN;
SET LOCAL lock_timeout = '3s';
ALTER TABLE orders ADD COLUMN note text;
COMMIT;
```

On `55P03` the table was never frozen ; retry the migration when traffic is
quiet. Matches vooronderzoek §16 (migration safety).

## AP-5 : lock_timeout >= statement_timeout

**Anti-pattern** : setting `lock_timeout` equal to or greater than a nonzero
`statement_timeout` on the same role.

**Cause** : `statement_timeout` counts total time including lock waits.
`lock_timeout` counts only lock-wait time. If `lock_timeout` is not strictly
smaller, the statement budget is exhausted first.

**Symptom** : `lock_timeout` never fires ; lock waits always surface as `57014`
(statement timeout) instead of the intended `55P03` ; retry logic that keys on
`55P03` is dead code.

**Fix** : the docs are explicit. "if statement_timeout is nonzero, it is rather
pointless to set lock_timeout to the same or larger value". Set `lock_timeout`
strictly below `statement_timeout`.

```sql
ALTER ROLE app_web SET lock_timeout = '2s';      -- below ...
ALTER ROLE app_web SET statement_timeout = '8s'; -- ... this
```

## AP-6 : One global statement_timeout for every workload

**Anti-pattern** : a single `statement_timeout` in `postgresql.conf` applied to
web requests, batch jobs, and reporting alike.

**Cause** : web and batch have opposite needs. A web request running 10s is a
bug ; a batch aggregation running 10 minutes is normal. One value cannot serve
both.

**Symptom** : either the value is web-sized and every batch job dies with
`57014`, or it is batch-sized and runaway web queries run for minutes
unbounded.

**Fix** : scope per role or per database ; keep the global default conservative
or unset and override deliberately.

```sql
ALTER ROLE app_web      SET statement_timeout = '8s';
ALTER ROLE app_batch    SET statement_timeout = '30min';
ALTER DATABASE analytics SET statement_timeout = '5min';
```

## AP-7 : transaction_timeout shorter than the other timeouts

**Anti-pattern** : on v17+, setting `transaction_timeout` shorter than or equal
to `statement_timeout` or `idle_in_transaction_session_timeout`.

**Cause** : the docs state that when `transaction_timeout` is shorter than or
equal to either of those, "the longer timeout is ignored". A too-short
`transaction_timeout` silently disables the timeout you meant to keep.

**Symptom** : a `statement_timeout` or `idle_in_transaction_session_timeout` you
configured appears to do nothing ; the wrong timeout fires ; behaviour is
confusing because no setting was removed.

**Fix** : order them so `transaction_timeout` is the longest.

```sql
ALTER ROLE app_web SET lock_timeout = '2s';
ALTER ROLE app_web SET statement_timeout = '8s';
ALTER ROLE app_web SET idle_in_transaction_session_timeout = '10s';
ALTER ROLE app_web SET transaction_timeout = '30s';   -- longest, v17+
```
