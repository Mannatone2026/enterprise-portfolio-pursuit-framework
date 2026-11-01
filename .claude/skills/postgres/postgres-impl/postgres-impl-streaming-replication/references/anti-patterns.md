# postgres-impl-streaming-replication : Anti-Patterns

Each anti-pattern : cause, symptom, fix. Verified against postgresql.org/docs/17 (Last Verified : 2026-05-19).

## 1. Physical slot with no max_slot_wal_keep_size

CAUSE : a physical replication slot exists but `max_slot_wal_keep_size` is left at the default `-1` (unlimited).
SYMPTOM : the standby disconnects (crash, network, decommission), the slot keeps pinning WAL, `pg_wal` grows until the primary's disk fills and the primary stops , a full outage.
SQLSTATE : none on the slot itself ; the primary fails with disk-full errors.
FIX : always set `max_slot_wal_keep_size` so an over-cap slot is invalidated (`wal_status = 'lost'`) and WAL is freed. Drop slots for standbys that are gone for good.

```sql
ALTER SYSTEM SET max_slot_wal_keep_size = '10GB';
SELECT pg_reload_conf();
SELECT pg_drop_replication_slot('dead_standby_slot');
```

## 2. synchronous_standby_names pointing at a single standby

CAUSE : `synchronous_standby_names = 'standby1'` , exactly one standby, no spare.
SYMPTOM : when `standby1` goes down, every `COMMIT` on the primary blocks indefinitely waiting for a confirmation that will never come. The primary looks hung.
SQLSTATE : none , commits hang rather than error.
FIX : list more standbys than the required count, using `ANY n` or `FIRST n`.

```ini
synchronous_standby_names = 'ANY 1 (standby1, standby2)'
```

## 3. synchronous_commit = remote_apply with a single standby

CAUSE : the strongest durability mode is used, but there is only one synchronous standby.
SYMPTOM : every commit waits for that standby to fully REPLAY the change. Standby replay lag (slow disk, long query, conflict) directly becomes primary commit latency.
SQLSTATE : none.
FIX : reserve `remote_apply` for transactions that truly need read-your-write on the standby (use `SET LOCAL`), keep multiple sync standbys, or step down to `on` / `remote_write`.

## 4. Long query on a default-tuned hot standby

CAUSE : an analytic or reporting query runs on a standby while WAL replay hits a conflicting change (VACUUM cleanup, `ACCESS EXCLUSIVE` lock).
SYMPTOM : the query is killed mid-flight , "canceling statement due to conflict with recovery".
SQLSTATE : `40001` (`serialization_failure`).
FIX : set `hot_standby_feedback = on` so the primary keeps the tuples the standby needs, or raise `max_standby_streaming_delay` to allow the query more time (at the cost of replay lag).

## 5. Treating hot_standby_feedback as free

CAUSE : `hot_standby_feedback = on` enabled everywhere to stop query cancellations, without watching the primary.
SYMPTOM : a long-running standby query holds back the primary's VACUUM cleanup horizon , the primary bloats and table/index sizes grow.
SQLSTATE : none.
FIX : enable `hot_standby_feedback` only where long standby queries are expected, monitor the primary's oldest `xmin` and bloat, and keep standby queries bounded.

## 6. Raw cp of a running primary's data directory

CAUSE : the standby data directory was created with `cp -r` / `rsync` of a live primary instead of `pg_basebackup`.
SYMPTOM : the standby fails to start or comes up corrupt , the copy spans an inconsistent set of pages and a moving WAL position.
SQLSTATE : startup failure, not a query error.
FIX : always clone with `pg_basebackup` , it takes a consistent online base backup and (with `-X stream`) bundles the WAL needed for consistency.

## 7. No host replication line in pg_hba.conf

CAUSE : `pg_hba.conf` has `host all all ...` but no line with the literal `replication` keyword.
SYMPTOM : `pg_basebackup` and the standby connection are rejected , "no pg_hba.conf entry for replication connection".
SQLSTATE : `28000` (`invalid_authorization_specification`) class on the connection.
FIX : add an explicit replication line ; `replication` is a pseudo-database that `host all` does not cover.

```
host    replication    repuser    10.0.0.0/24    scram-sha-256
```

## 8. Promoting a standby without fencing the old primary

CAUSE : a standby is promoted while the old primary is still running and reachable by clients.
SYMPTOM : split brain , two writable primaries accept divergent writes on different timelines ; data must later be reconciled or lost.
SQLSTATE : none , a data-integrity disaster, not an error.
FIX : fence the old primary (stop it, block its network, STONITH) BEFORE calling `pg_promote()`. Use a proven failover manager for automated failover.

## 9. Expecting promote_trigger_file to work

CAUSE : a v15-era runbook drops a trigger file to promote a standby.
SYMPTOM : nothing happens , `promote_trigger_file` was removed in PostgreSQL 16.
SQLSTATE : none.
FIX : promote with `SELECT pg_promote();` or `pg_ctl promote`.

## 10. Monitoring connection state but not replay lag

CAUSE : monitoring only checks that the standby appears in `pg_stat_replication`.
SYMPTOM : the standby is "connected" and `streaming`, yet `replay_lsn` trails `sent_lsn` by a long way , reads return badly stale data and a failover would lose committed transactions.
SQLSTATE : none.
FIX : alarm on `replay_lag` / `pg_wal_lsn_diff(sent_lsn, replay_lsn)` and on `pg_last_xact_replay_timestamp()`, not just on the row's existence.

## 11. pg_dump against a busy hot standby

CAUSE : a logical backup (`pg_dump`) is run on a standby that is also replaying heavy WAL.
SYMPTOM : the dump's long-lived snapshot conflicts with replay and is cancelled partway , an incomplete dump.
SQLSTATE : `40001` (`serialization_failure`).
FIX : run `pg_dump` against the primary, or set `hot_standby_feedback = on` / raise `max_standby_streaming_delay` on the standby for the duration of the dump.

## Source URLs

- https://www.postgresql.org/docs/17/warm-standby.html
- https://www.postgresql.org/docs/17/hot-standby.html
- https://www.postgresql.org/docs/17/app-pgbasebackup.html
- https://www.postgresql.org/docs/17/errcodes-appendix.html
