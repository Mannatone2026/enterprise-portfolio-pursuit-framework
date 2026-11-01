# postgres-errors-serialization : anti-patterns

Each entry : cause, symptom, SQLSTATE, fix. Verified against PostgreSQL 17
(transaction-iso.html, sql-set-transaction.html).

## 1. Treating 40001 as a fatal error

- **Cause** : application catches `serialization_failure` in a generic error
  handler and surfaces it to the user as "database error" or 500.
- **Symptom** : intermittent user-facing failures under load; logs full of
  `could not serialize access ...` with no retry.
- **SQLSTATE** : `40001`.
- **Fix** : 40001 is expected, not a bug. Catch it specifically and retry the
  WHOLE transaction with backoff (SKILL.md Pattern 1). Only surface it after the
  retry budget is exhausted.

## 2. Retrying only the failed statement

- **Cause** : the error fires on one `UPDATE`, so the handler re-runs just that
  `UPDATE` while the transaction is still notionally "open".
- **Symptom** : `current transaction is aborted, commands ignored until end of
  transaction block` (SQLSTATE `25P02`) on the retried statement, or silently
  wrong data because earlier reads in the transaction were never re-run.
- **SQLSTATE** : `40001` followed by `25P02`.
- **Fix** : a 40001 rolls back the ENTIRE transaction. Retry from `BEGIN`,
  re-running every read and every write. The transaction body must be a
  re-runnable unit (a callback / closure), not a single statement.

## 3. Running SERIALIZABLE without a retry loop

- **Cause** : `default_transaction_isolation` set to `serializable`, or
  `BEGIN ISOLATION LEVEL SERIALIZABLE` used, but no application retry logic.
- **Symptom** : transactions fail unpredictably whenever concurrency rises;
  failures look random because the official docs note it is hard to predict
  which transactions become victims.
- **SQLSTATE** : `40001`.
- **Fix** : `SERIALIZABLE` REQUIRES a generalized retry mechanism. Never enable
  it, per-transaction or as a default, without wrapping every transaction in
  the Pattern 1 loop. Branch on SQLSTATE `40001`, not on message text.

## 4. Assuming REPEATABLE READ prevents write skew

- **Cause** : developer picks `REPEATABLE READ` believing a transaction-wide
  snapshot prevents all anomalies, then writes check-then-act logic across rows.
- **Symptom** : invariant silently violated under concurrency (two doctors both
  go off-call, an account overdrawn by parallel withdrawals, a room
  double-booked). No error is raised, both transactions commit.
- **SQLSTATE** : none at failure time. That is the danger. The bug surfaces as
  corrupt data later.
- **Fix** : `REPEATABLE READ` prevents dirty, nonrepeatable, and phantom reads
  but ALLOWS serialization anomalies (write skew). Only `SERIALIZABLE` prevents
  write skew. Use `SERIALIZABLE` for cross-row invariants, or express the
  invariant as a single `UNIQUE`/`EXCLUDE` constraint or a `SELECT FOR UPDATE`
  that locks the rows the decision depends on.

## 5. Unbounded retry loop

- **Cause** : retry loop has no attempt cap, it retries 40001 forever.
- **Symptom** : a contended transaction spins indefinitely, burning CPU and
  connections; a genuine hotspot is masked instead of fixed; latency spikes
  with no error logged.
- **SQLSTATE** : `40001` (repeatedly, never surfaced).
- **Fix** : cap retries (e.g. 5 attempts) with exponential backoff + jitter.
  After the cap, let the error propagate. Sustained 40001 means a real
  contention point to redesign (shorter transactions, finer locking, a
  different access pattern), not to retry harder.

## 6. SET TRANSACTION issued after the first query

- **Cause** : `SET TRANSACTION ISOLATION LEVEL SERIALIZABLE` placed after a
  `SELECT` has already run in the transaction.
- **Symptom** : no error, but the transaction silently runs at the OLD level
  (usually `READ COMMITTED`); expected serialization protection never applies.
- **SQLSTATE** : none, silent. A standalone `SET TRANSACTION` with no open
  transaction emits a warning only.
- **Fix** : set the level on `BEGIN` (`BEGIN ISOLATION LEVEL SERIALIZABLE`), or
  make `SET TRANSACTION` the very first command after `BEGIN`. The level is
  fixed at the first `SELECT`/`INSERT`/`UPDATE`/`DELETE`/`MERGE`/`FETCH`/`COPY`.

## 7. Long SERIALIZABLE report without READ ONLY DEFERRABLE

- **Cause** : a multi-minute or multi-hour `SERIALIZABLE` aggregation runs as a
  normal read-write transaction.
- **Symptom** : the report aborts with 40001 after running a long time, wasting
  the whole computation; predicate locks accumulate for its full duration.
- **SQLSTATE** : `40001`.
- **Fix** : declare it `BEGIN ISOLATION LEVEL SERIALIZABLE READ ONLY
  DEFERRABLE`. It blocks briefly at startup for a conflict-free snapshot, then
  cannot abort with 40001 and takes no SSI overhead.

## 8. Keeping SELECT FOR UPDATE under SERIALIZABLE

- **Cause** : code carries explicit `SELECT FOR UPDATE` / `SELECT FOR SHARE`
  locking that was needed under `READ COMMITTED`, and is left in place after the
  transaction is switched to `SERIALIZABLE`.
- **Symptom** : extra lock contention and lock waits on top of SSI's own
  protection; throughput drops with no correctness benefit.
- **SQLSTATE** : none, a performance regression, not an error.
- **Fix** : under `SERIALIZABLE`, SSI already detects the read/write conflicts
  those explicit locks were defending. Remove the unnecessary `FOR UPDATE` /
  `FOR SHARE` clauses. Keep explicit row locks only where the transaction runs
  at `READ COMMITTED` or `REPEATABLE READ`.

## 9. Branching retry logic on the error message string

- **Cause** : handler matches the text `"concurrent update"` to decide whether
  to retry, missing the `"read/write dependencies"` variant.
- **Symptom** : SSI-cycle aborts under `SERIALIZABLE` are not retried; they leak
  out as fatal errors while concurrent-update aborts are handled.
- **SQLSTATE** : `40001` (both messages share it).
- **Fix** : both 40001 messages carry SQLSTATE `40001`. Always branch on the
  SQLSTATE code, never on human-readable message text. Messages are
  locale- and version-sensitive, the code is stable.
