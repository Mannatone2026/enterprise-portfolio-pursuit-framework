# postgres-errors-serialization : methods

API surface, error codes, and parameter tables for transaction isolation and
serialization failures. Verified against PostgreSQL 17 documentation
(transaction-iso.html, sql-set-transaction.html, errcodes-appendix.html).

## Isolation levels :

PostgreSQL implements three distinct levels. `READ UNCOMMITTED` is parsed but
silently mapped to `READ COMMITTED`. There is no dirty-read behavior in any
version. All four behaviors below are identical across v15, v16, and v17.

| Level | Snapshot taken | Dirty read | Nonrepeatable read | Phantom read | Serialization anomaly (write skew) |
|-------|----------------|------------|--------------------|--------------|-----------------------------------|
| `READ COMMITTED` | per statement | no | possible | possible | possible |
| `REPEATABLE READ` | per transaction | no | no | no | possible |
| `SERIALIZABLE` | per transaction + SSI | no | no | no | no |

- `READ COMMITTED` : each statement sees rows committed before that statement
  began. Two `SELECT`s in one transaction can return different data.
- `REPEATABLE READ` : the snapshot is taken at the first non-transaction-control
  statement and frozen for the whole transaction. PostgreSQL's implementation
  also blocks phantom reads, which is stricter than the SQL standard requires.
- `SERIALIZABLE` : `REPEATABLE READ` plus Serializable Snapshot Isolation (SSI),
  which additionally prevents write-skew serialization anomalies.

## SQLSTATE codes :

| SQLSTATE | Condition name | Retryable | Meaning |
|----------|----------------|-----------|---------|
| `40001` | `serialization_failure` | YES, retry whole transaction | snapshot conflict under REPEATABLE READ / SERIALIZABLE |
| `40P01` | `deadlock_detected` | YES, retry whole transaction | lock cycle (see postgres-errors-deadlocks) |
| `40000` | `transaction_rollback` | class code | parent class of the `40xxx` rollback family |
| `40002` | `transaction_integrity_constraint_violation` | depends | deferred constraint failed at commit |
| `40003` | `statement_completion_unknown` | no | outcome of statement not known |
| `23xxx` | constraint violations | NO | data is wrong; fix it, do not retry |

Both 40001 messages share the SAME SQLSTATE. Branch on SQLSTATE, not on
message text :

```
ERROR:  could not serialize access due to concurrent update
```
A `REPEATABLE READ` or `SERIALIZABLE` transaction tried to `UPDATE`, `DELETE`,
`SELECT FOR UPDATE`, or `SELECT FOR SHARE` a row that a concurrent transaction
modified after this transaction's snapshot was taken.

```
ERROR:  could not serialize access due to read/write dependencies among transactions
```
SSI detected a dangerous read/write dependency cycle under `SERIALIZABLE`. May
be a false positive. Retry regardless.

## SET TRANSACTION grammar :

Verified synopsis from sql-set-transaction.html :

```
SET TRANSACTION transaction_mode [, ...]
SET TRANSACTION SNAPSHOT snapshot_id
SET SESSION CHARACTERISTICS AS TRANSACTION transaction_mode [, ...]

where transaction_mode is one of:
    ISOLATION LEVEL { SERIALIZABLE | REPEATABLE READ | READ COMMITTED | READ UNCOMMITTED }
    READ WRITE | READ ONLY
    [ NOT ] DEFERRABLE
```

`BEGIN` and `START TRANSACTION` accept the same `transaction_mode` list inline :

```
BEGIN [ ISOLATION LEVEL { SERIALIZABLE | REPEATABLE READ | READ COMMITTED | READ UNCOMMITTED } ]
      [ READ WRITE | READ ONLY ] [ [ NOT ] DEFERRABLE ]
```

Timing rule : `SET TRANSACTION` must be issued before the first `SELECT`,
`INSERT`, `UPDATE`, `DELETE`, `MERGE`, `FETCH`, or `COPY` of the transaction.
Issued later it raises no error but has no effect on the already-fixed snapshot.
Issued with no open transaction it emits a warning and does nothing.

## Setting the level : four scopes

| Scope | Statement | Effect |
|-------|-----------|--------|
| One transaction | `BEGIN ISOLATION LEVEL SERIALIZABLE;` | this transaction only |
| One transaction | `SET TRANSACTION ISOLATION LEVEL ...;` | this transaction, must be first command |
| Session | `SET SESSION CHARACTERISTICS AS TRANSACTION ISOLATION LEVEL ...;` | every later transaction in this session |
| Database / role | `ALTER DATABASE d SET default_transaction_isolation = '...';` | every new session on that DB / role |

## Configuration parameters :

| Parameter | Default | Purpose |
|-----------|---------|---------|
| `default_transaction_isolation` | `read committed` | isolation level for transactions that do not set one |
| `default_transaction_read_only` | `off` | default `READ ONLY` mode |
| `default_transaction_deferrable` | `off` | default `DEFERRABLE` mode |
| `transaction_isolation` | (current) | read/set the running transaction's level |
| `transaction_read_only` | (current) | read/set the running transaction's read-only flag |
| `transaction_deferrable` | (current) | read/set the running transaction's deferrable flag |

## DEFERRABLE :

`DEFERRABLE` has effect ONLY when the transaction is both `SERIALIZABLE` and
`READ ONLY`. Such a transaction blocks at startup until it can acquire a
snapshot guaranteed free of serialization anomalies, then runs with no SSI
overhead and CANNOT abort with 40001. This is the only case where a
`SERIALIZABLE` transaction blocks while a `REPEATABLE READ` one would not. It
suits long-running reports and backups. On any non-`SERIALIZABLE` or
non-`READ ONLY` transaction the flag is ignored.

## SSI predicate locks :

- SSI keeps predicate locks to detect when a write would have changed the
  result of a prior read in a concurrent transaction.
- Predicate locks appear in `pg_locks` with `mode = 'SIReadLock'`.
- They do NOT block other transactions and CANNOT participate in a deadlock.
  Their only effect is enabling a 40001 abort.
- They are based on data actually accessed. A sequential scan forces a
  relation-level predicate lock (coarse, more false positives); an index scan
  produces finer-grained locks.
- A `READ ONLY` transaction can often release SIRead locks early, or avoid
  taking any, once it proves no anomaly can still occur.

Inspect active predicate locks :

```sql
SELECT pid, relation::regclass, mode
FROM pg_locks
WHERE mode = 'SIReadLock';
```

## SSI tuning parameters (raise only if 40001 rate climbs from memory limits) :

| Parameter | Default | Purpose |
|-----------|---------|---------|
| `max_pred_locks_per_transaction` | 64 | predicate locks budget per transaction; exceeding forces lock escalation to coarser granularity |
| `max_pred_locks_per_relation` | -2 | page locks per relation before escalating to a relation lock |
| `max_pred_locks_per_page` | 2 | tuple locks per page before escalating to a page lock |

Escalation to coarser locks increases false-positive 40001 aborts. If
serialization failures rise under memory pressure, raise these limits. To
reduce sequential scans (which take relation-level predicate locks), tune the
planner via `random_page_cost` / `cpu_tuple_cost` and ensure useful indexes.
