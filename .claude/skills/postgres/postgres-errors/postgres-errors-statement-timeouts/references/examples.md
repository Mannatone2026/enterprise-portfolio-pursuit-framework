# Examples : Statement and Session Timeouts

Working, verified examples for bounding query and session duration.
Version annotations : `v15+` works on 15, 16, 17 ; `v14+` / `v17+` mark
features added in those versions.

## Example 1 : Per-role timeout profile for a web app (v15+)

```sql
-- web role : fail fast, catch leaked transactions
ALTER ROLE app_web SET statement_timeout = '8s';
ALTER ROLE app_web SET lock_timeout = '2s';
ALTER ROLE app_web SET idle_in_transaction_session_timeout = '10s';

-- batch role : long statements allowed, leaks still caught
ALTER ROLE app_batch SET statement_timeout = '30min';
ALTER ROLE app_batch SET idle_in_transaction_session_timeout = '60s';

-- migration role : DDL fails fast instead of freezing the table
ALTER ROLE app_migrate SET lock_timeout = '3s';
ALTER ROLE app_migrate SET statement_timeout = '15min';
```

Settings apply at connection time. Verify with `SHOW statement_timeout;` after
connecting as each role.

## Example 2 : transaction_timeout hard cap, v17+ only (v17+)

```sql
-- absolute ceiling on transaction wall-clock time
ALTER ROLE app_web SET transaction_timeout = '30s';
```

Keep `transaction_timeout` LONGER than `statement_timeout` and
`idle_in_transaction_session_timeout` on the same role. The docs : if
`transaction_timeout` is shorter than or equal to either, the longer one is
ignored. Correct ordering : `lock_timeout` < `statement_timeout` <
`transaction_timeout`.

## Example 3 : idle_session_timeout for connection hygiene (v14+)

```sql
-- close connections idle (no open transaction) for too long
ALTER ROLE app_web SET idle_session_timeout = '5min';
```

This is lower priority than `idle_in_transaction_session_timeout` : an idle
session with no transaction "imposes no large costs on the server". Use it to
trim stale pooled connections, not as a correctness safeguard.

## Example 4 : Safe migration wrapper with lock_timeout (v15+)

```sql
BEGIN;
SET LOCAL lock_timeout = '3s';          -- fail fast if the table is busy
SET LOCAL statement_timeout = '0';      -- the DDL itself may legitimately run long
ALTER TABLE orders ADD COLUMN shipped_at timestamptz;
COMMIT;
```

`SET LOCAL` auto-reverts at `COMMIT`/`ROLLBACK`, so the timeouts never leak into
later statements on the same pooled connection. If the `ALTER` cannot get its
`ACCESS EXCLUSIVE` lock within 3s it fails with `55P03` ; retry when quiet.

## Example 5 : Override the role default for one long batch statement (v15+)

```sql
BEGIN;
SET LOCAL statement_timeout = '2h';     -- this report is known to run long
INSERT INTO monthly_rollup
SELECT account_id, date_trunc('month', ts), sum(amount)
FROM transactions
GROUP BY 1, 2;
COMMIT;
```

`SET LOCAL` raises the limit for exactly one transaction without touching the
role default. Outside this transaction the role's normal `statement_timeout`
still applies.

## Example 6 : Migration retry loop on 55P03 (psycopg 3, Python)

```python
import time, psycopg
from psycopg import errors

def run_migration(conn, ddl: str, attempts: int = 5):
    for i in range(attempts):
        try:
            with conn.transaction():
                conn.execute("SET LOCAL lock_timeout = '3s'")
                conn.execute(ddl)
            return
        except errors.LockNotAvailable:        # 55P03 : transient, retry
            wait = 2 ** i
            print(f"lock busy, retry in {wait}s ({i+1}/{attempts})")
            time.sleep(wait)
    raise RuntimeError("migration could not acquire its lock")
```

`55P03` from `lock_timeout` is transient : the blocking query may finish soon.
Retrying with backoff is correct. Contrast Example 7.

## Example 7 : Do NOT auto-retry a statement_timeout (psycopg 3, Python)

```python
from psycopg import errors

try:
    cur.execute("SELECT ... slow query ...")
except errors.QueryCanceled as e:           # 57014
    msg = (e.diag.message_primary or "")
    if "statement timeout" in msg:
        # the query is genuinely too slow. Retrying wastes the same time
        # and the same locks. Log it, alert, fix the query or add an index.
        raise RuntimeError(f"query exceeded statement_timeout: {msg}")
    if "user request" in msg:
        # someone or something canceled it on purpose
        raise RuntimeError(f"query canceled by request: {msg}")
    raise
```

A `57014` whose message says "statement timeout" will time out again on retry.
Fix the query ; do not loop.

## Example 8 : Find and end leaked idle-in-transaction sessions (v15+)

```sql
-- diagnose : sessions sitting in an open transaction
SELECT pid, usename, application_name,
       now() - state_change AS idle_for,
       left(query, 80) AS last_query
FROM pg_stat_activity
WHERE state = 'idle in transaction'
  AND now() - state_change > interval '1 minute'
ORDER BY idle_for DESC;

-- emergency : terminate one leaked session by pid
SELECT pg_terminate_backend(12345);
```

The permanent fix is `ALTER ROLE ... SET idle_in_transaction_session_timeout`,
not manual termination. Manual `pg_terminate_backend` is only for an incident
in progress.

## Example 9 : node-postgres timeout handling (JavaScript)

```javascript
// per-connection statement_timeout via the pool config
const pool = new Pool({
  statement_timeout: 8000,                  // ms, applied to every connection
  idle_in_transaction_session_timeout: 10000,
});

try {
  await pool.query('SELECT ... slow ...');
} catch (err) {
  if (err.code === '57014') {
    throw new Error(`query timed out: ${err.message}`);   // do not retry
  }
  if (err.code === '55P03') {
    // lock wait timed out: retrying with backoff is reasonable
    throw new Error(`lock wait timed out: ${err.message}`);
  }
  throw err;
}
```

## Example 10 : Verify effective timeouts after connecting (v15+)

```sql
SELECT name, setting, unit, source
FROM pg_settings
WHERE name IN ('statement_timeout', 'lock_timeout',
               'idle_in_transaction_session_timeout',
               'idle_session_timeout', 'transaction_timeout')
ORDER BY name;
-- source column shows where the value came from:
--   'default'           -> still 0, nothing set
--   'role' / 'database' -> an ALTER ROLE / ALTER DATABASE override
--   'session'           -> a SET in this session
```

`transaction_timeout` returns no row on v15/v16 : it exists only on v17+.
