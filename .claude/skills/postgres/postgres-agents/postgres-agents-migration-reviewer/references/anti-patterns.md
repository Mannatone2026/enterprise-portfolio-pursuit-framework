# postgres-agents-migration-reviewer : anti-patterns

Anti-patterns of the REVIEWER, the failure modes of this skill itself. Each
entry : cause, symptom, fix. Verified against PostgreSQL 17 (sql-altertable.html,
sql-createindex.html).

## 1. Lock-blind approval

- **Cause** : the reviewer reads the migration as plain SQL, confirms the
  syntax is valid, and approves it without asking what lock each statement
  takes or how long it holds it.
- **Symptom** : a migration marked SAFE ships, then a plain `CREATE INDEX` or
  an `ALTER COLUMN TYPE` freezes all writes (or all traffic) to a hot table for
  the duration of the build or rewrite. The outage was fully predictable.
- **Fix** : never grade a migration without grading every lock in it. For each
  statement, name the lock level, name what it blocks (writes, or reads and
  writes), and estimate how long it is held. Run all 14 checks; M-1, M-2, M-10
  exist exactly to catch this.

## 2. Small-table fallacy

- **Cause** : the reviewer sees the target table has few rows and concludes the
  locks are harmless, so M-1 / M-2 / M-5 are downgraded or skipped.
- **Symptom** : a "tiny" table that happens to be on the hot path (a sessions
  table, a feature-flag table, a counter) is locked against writes, and every
  request that touches it stalls. Row count was low; request rate was not.
- **Fix** : lock danger is a function of CONCURRENT TRAFFIC, not row count. A
  500-row table read or written on every request is more lock-sensitive than a
  billion-row archive table nothing touches. Grade the lock by what traffic it
  blocks, never by `SELECT count(*)`.

## 3. Deploy-order blindness

- **Cause** : the reviewer checks a `DROP COLUMN` or `RENAME` only for locking
  cost and misses that the running application still uses the old name.
- **Symptom** : the migration commits cleanly and fast, then the old (still
  deployed) app code throws `column does not exist` on every request until the
  new code rolls out. The schema and the code disagreed for the deploy window.
- **Fix** : treat M-7 (DROP / RENAME) as a deploy-sequencing problem, not a
  lock problem. A breaking name change is only safe inside an expand-contract
  sequence where schema and app deploys are ordered so neither side ever sees a
  name the other lacks. Flag it CRITICAL until that sequence is shown.

## 4. Severity inflation and deflation

- **Cause** : the reviewer grades by gut feel, marking a missing down path
  CRITICAL because it "feels risky", or a full table rewrite INFO because the
  SQL is one short line.
- **Symptom** : the verdict loses meaning. A migration is blocked over a
  hygiene note, or an outage-causing rewrite is waved through as a style
  comment. Authors stop trusting the review.
- **Fix** : grade strictly by the rubric. CRITICAL means blocks traffic,
  rewrites a table, loses data, breaks the app, or errors out. WARNING means a
  foot-gun at scale. INFO means process hygiene. A table rewrite is always
  CRITICAL; a missing down path is always INFO. The line length is irrelevant.

## 5. Citing nothing

- **Cause** : the reviewer reports a finding ("this locks the table") but does
  not point to the skill that contains the safe rewrite.
- **Symptom** : the author knows the migration is unsafe but not how to fix it,
  so they either guess or ship it anyway. The review found the problem and
  stopped one step short of being useful.
- **Fix** : every finding cites its sibling skill. Locking and rewrite findings
  cite postgres-impl-zero-downtime-migrations; timeout findings cite
  postgres-errors-statement-timeouts; partitioned-table findings cite
  postgres-impl-partitioning. A finding without a cite is incomplete.

## 6. Approving without a rehearsal recommendation

- **Cause** : the reviewer concludes a locking migration is "probably fine"
  from reading it, and never tells the author to measure the real lock window.
- **Symptom** : the migration's lock is held far longer than anyone guessed,
  because the table was bigger, or a long concurrent query delayed lock
  acquisition, or `CONCURRENTLY` took two slow scans. The estimate was a guess.
- **Fix** : for any CRITICAL or WARNING about locking or rewrites, always add a
  rehearsal line: run the migration against a production-sized copy under
  synthetic concurrent write load and measure how long the lock is actually
  held. A migration is safe because its lock window was measured, not because
  the SQL looked short.
