---
name: postgres-errors-statement-timeouts
description: >
  Use when queries are canceled by timeout, sessions hold locks while idle, or you need to bound query and transaction duration safely.
  Prevents a forgotten open transaction blocking vacuum forever (set idle_in_transaction_session_timeout), blindly retrying a statement_timeout caused by a missing index, and migrations stalling all traffic without lock_timeout.
  Covers statement_timeout (57014), lock_timeout (55P03), idle_in_transaction_session_timeout (25P03), idle_session_timeout (v14+), transaction_timeout (v17+), per-role/per-database scoping, fail-fast vs retry decision.
  Keywords: statement_timeout, lock_timeout, idle_in_transaction_session_timeout, idle_session_timeout, transaction_timeout, 57014, query_canceled, 55P03, 25P03, query canceled due to statement timeout, idle in transaction, query takes too long, how to limit query time, canceling statement
license: MIT
compatibility: "Designed for Claude Code. Requires PostgreSQL 15, 16, or 17 (transaction_timeout is v17+, idle_session_timeout is v14+)."
metadata:
  author: OpenAEC-Foundation
  version: "1.0"
---

# postgres-errors-statement-timeouts

## Quick Reference :

PostgreSQL has five timeout GUCs. Each one is a safety bound, and each one is
`0` (disabled) by default. A production database with all five at `0` has no
defense against a runaway query, a leaked transaction, or a migration that
queues behind a slow `SELECT`.

```
ERROR:  canceling statement due to statement timeout      -- SQLSTATE 57014
ERROR:  canceling statement due to lock timeout           -- SQLSTATE 55P03
FATAL:  terminating connection due to idle-in-transaction timeout  -- 25P03
```

The five timeouts, what they bound, and what they do when hit :

| GUC | Bounds | On limit | Default | Since |
|-----|--------|----------|---------|-------|
| `statement_timeout` | total run time of one statement | aborts the statement (`57014`) | `0` | all |
| `lock_timeout` | time spent WAITING for a lock | aborts the statement (`55P03`) | `0` | all |
| `idle_in_transaction_session_timeout` | idle time inside an open transaction | terminates the session (`25P03`) | `0` | all |
| `idle_session_timeout` | idle time with NO open transaction | terminates the session (class `08`) | `0` | v14+ |
| `transaction_timeout` | total duration of a transaction | terminates the session | `0` | v17+ |

ALWAYS set `idle_in_transaction_session_timeout` in production : a forgotten
open transaction holds locks and blocks `VACUUM` from cleaning dead tuples.
ALWAYS set a `lock_timeout` before any migration. NEVER blindly retry a `57014`
from `statement_timeout` : a genuinely slow query needs an index, not a retry.

## When To Use This Skill :

ALWAYS use this skill when :
- A query fails with "canceling statement due to statement timeout" (`57014`)
- A statement fails with "canceling statement due to lock timeout" (`55P03`)
- A session is dropped with "idle-in-transaction timeout" (`25P03`)
- You are choosing timeout values for a web role, a batch role, or a migration
- A migration `ALTER TABLE` stalled all traffic on the table
- `VACUUM` cannot clean a table and old `idle in transaction` sessions are suspected

NEVER use this skill for :
- `40P01 deadlock_detected` (use postgres-errors-deadlocks)
- `40001 serialization_failure` retry logic (use postgres-errors-serialization)
- Connection / authentication failures unrelated to idle timeouts (use postgres-errors-connection-auth)
- Designing the migration DDL itself (use postgres-impl-zero-downtime-migrations)

## Decision Trees :

### Which timeout fired, and do I retry :

```
Statement or session ended unexpectedly. Read the message and SQLSTATE :
├── "canceling statement due to statement timeout"  (57014)
│      -> the statement ran longer than statement_timeout.
│         Retry will NOT help: the query is genuinely slow.
│         Fix the query or add an index. Retry only if a transient
│         load spike is the proven cause.
├── "canceling statement due to lock timeout"  (55P03)
│      -> the statement waited too long to ACQUIRE a lock.
│         Often transient. Retry with backoff is reasonable.
├── "canceling statement due to user request"  (57014)
│      -> NOT a timeout: pg_cancel_backend() or a client cancel.
│         Investigate who canceled it; do not auto-retry.
├── "terminating connection due to idle-in-transaction timeout"  (25P03)
│      -> the app left a transaction open and idle. The session is gone.
│         Fix the app: commit/rollback promptly, shorten transactions.
└── connection dropped, no statement running (class 08)
       -> idle_session_timeout or transaction_timeout terminated the
          session. Reconnect; review whether the cap is too tight.
```

### Which timeout to set for which workload :

```
What kind of database role is this?
├── Web / API request role
│      -> statement_timeout short (e.g. '5s' to '15s')
│         idle_in_transaction_session_timeout '10s'
│         transaction_timeout (v17+) as a hard cap, e.g. '30s'
├── Batch / ETL / reporting role
│      -> statement_timeout long or 0, set deliberately per job
│         idle_in_transaction_session_timeout still set ('60s'+)
├── Migration / DDL role
│      -> lock_timeout REQUIRED (e.g. '3s') so DDL fails fast
│         instead of queueing behind a long query and blocking all traffic
│      -> statement_timeout sized to the operation, not the web default
└── Every role, always
       -> idle_in_transaction_session_timeout > 0 to catch leaked transactions
```

### statement_timeout vs lock_timeout interaction :

```
Setting both on the same role?
├── lock_timeout < statement_timeout
│      -> CORRECT. A lock wait fails fast (55P03) before the
│         statement budget (57014) is spent.
└── lock_timeout >= statement_timeout (both nonzero)
       -> POINTLESS. statement_timeout always triggers first;
          lock_timeout can never fire. Lower lock_timeout.
```

## Patterns :

### Pattern 1 : Always set idle_in_transaction_session_timeout

ALWAYS set `idle_in_transaction_session_timeout` to a nonzero value in
production. NEVER leave it at the `0` default.

```sql
-- global floor in postgresql.conf, or per role :
ALTER ROLE app_web SET idle_in_transaction_session_timeout = '10s';
```

WHY : the docs state an open transaction "prevents vacuuming away
recently-dead tuples that may be visible only to this transaction; so remaining
idle for a long time can contribute to table bloat". A connection-pool client
that opens a transaction and then stalls (slow app code, a crashed worker) holds
locks and pins the xmin horizon indefinitely. This timeout terminates that
session with `25P03`, releasing the locks and unblocking `VACUUM`.

### Pattern 2 : lock_timeout before every migration

ALWAYS set a small `lock_timeout` for the duration of a migration. NEVER run an
`ALTER TABLE` against a busy table without one.

```sql
BEGIN;
SET LOCAL lock_timeout = '3s';
ALTER TABLE orders ADD COLUMN note text;   -- needs ACCESS EXCLUSIVE
COMMIT;
```

WHY : `ALTER TABLE` needs an `ACCESS EXCLUSIVE` lock. If a long-running query
holds even a weak lock, the `ALTER` waits in the lock queue, and every NEW query
queues behind the `ALTER`. The whole table freezes. With `lock_timeout` the
`ALTER` fails fast with `55P03` instead, leaving traffic untouched. Retry the
migration when the table is quiet.

### Pattern 3 : Scope timeouts per role, not globally

ALWAYS scope timeouts to the role or database that needs them with `ALTER ROLE
... SET` / `ALTER DATABASE ... SET`. NEVER force one global `statement_timeout`
on every workload.

```sql
ALTER ROLE app_web   SET statement_timeout = '10s';
ALTER ROLE app_batch SET statement_timeout = '30min';
ALTER DATABASE analytics SET statement_timeout = '5min';
```

WHY : a web request that runs 10s is a bug ; a batch job that runs 10s is
normal. A single global value is either too tight for batch or too loose for
web. Role-scoped settings apply at connection time and need no application
change. `SET LOCAL` inside a transaction overrides them for one unit of work.

### Pattern 4 : transaction_timeout as a hard cap (v17+)

ALWAYS add `transaction_timeout` on v17+ for web roles as an absolute ceiling on
transaction duration. NEVER rely on it as a substitute for the other timeouts.

```sql
-- v17+ : caps total transaction time regardless of activity
ALTER ROLE app_web SET transaction_timeout = '30s';
```

WHY : `statement_timeout` bounds one statement and
`idle_in_transaction_session_timeout` bounds idle gaps, but a transaction can
run for hours by chaining many short, active statements. `transaction_timeout`
"terminates any session that spans longer than the specified amount of time in
a transaction". The docs note : if `transaction_timeout` is shorter than or
equal to `idle_in_transaction_session_timeout` or `statement_timeout`, the
longer timeout is ignored. Prepared transactions are not subject to it.

### Pattern 5 : Fail fast for web requests, never disable globally

ALWAYS give web-facing roles a short `statement_timeout`. NEVER set
`statement_timeout = 0` globally in production.

```sql
ALTER ROLE app_web SET statement_timeout = '8s';
```

WHY : with `statement_timeout = 0` a single pathological query runs until it
finishes or the connection dies, holding locks and a buffer-pool footprint the
whole time. A short timeout converts an unbounded hang into a fast, catchable
`57014` the application can turn into an error response. Disable the timeout
only deliberately and narrowly (a known-long batch statement via `SET LOCAL`).

### Pattern 6 : Distinguish a timeout cancel from a manual cancel

ALWAYS read the error message text, not just the SQLSTATE, to tell timeout
cancels apart. NEVER assume every `57014` is `statement_timeout`.

```
57014 query_canceled has several causes, distinguished by message :
  "... due to statement timeout"        -> statement_timeout fired
  "... due to user request"             -> pg_cancel_backend() or client cancel
  "... due to conflict with recovery"   -> a hot-standby replay conflict
55P03 lock_not_available with message :
  "... due to lock timeout"             -> lock_timeout fired
  (also raised by NOWAIT clauses)
```

WHY : a `57014` from `statement_timeout` means "the query is too slow, fix it" ;
a `57014` from "user request" means a human or a watchdog killed it. Branching
retry logic on SQLSTATE alone treats these identical, and auto-retries a manual
cancel or a genuinely slow query. The message text carries the actual cause.

## Anti-Patterns :

(Full cause + symptom + SQLSTATE + fix in references/anti-patterns.md)

- No `idle_in_transaction_session_timeout` : leaked transactions block `VACUUM` forever
- `statement_timeout = 0` globally in production : one runaway query has no bound
- Blindly retrying a `57014` from `statement_timeout` : a missing index is not fixed by retrying
- Migration `ALTER` with no `lock_timeout` : the DDL queues and freezes all table traffic
- `lock_timeout >= statement_timeout` (both nonzero) : `lock_timeout` can never fire
- One global `statement_timeout` for web and batch : too tight for one, too loose for the other

## Reference Links :

- [references/methods.md](references/methods.md) : All five timeout GUCs, exact semantics, scoping commands, SQLSTATE table
- [references/examples.md](references/examples.md) : Role and database scoping, migration wrapper, retry logic, diagnostic queries
- [references/anti-patterns.md](references/anti-patterns.md) : Anti-patterns with cause, symptom, SQLSTATE, and fix

## See Also :

- postgres-errors-deadlocks : `NOWAIT` / `SKIP LOCKED` also raise `55P03`, lock-wait diagnosis
- postgres-errors-serialization : `40001` retry logic, contrasted with non-retryable `57014`
- postgres-impl-zero-downtime-migrations : `lock_timeout` + retry as a migration safety pattern
- postgres-errors-connection-auth : class `08` connection errors from terminated sessions
- Vooronderzoek section : §21 (timeout GUCs), §16 (migration safety)
- Official docs : https://www.postgresql.org/docs/17/runtime-config-client.html
- Official docs : https://www.postgresql.org/docs/17/errcodes-appendix.html
