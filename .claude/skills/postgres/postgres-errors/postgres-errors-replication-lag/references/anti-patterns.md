# postgres-errors-replication-lag : anti-patterns

Each entry : cause, symptom, fix. Verified against PostgreSQL 17
(warm-standby.html, logical-replication.html, monitoring-stats.html,
view-pg-replication-slots.html).

## 1. Inactive replication slot left in place

- **Cause** : a standby or logical subscriber is decommissioned, paused, or
  crashes, but its replication slot is never dropped. The slot keeps pinning
  WAL from its `restart_lsn` forward.
- **Symptom** : the primary's `pg_wal` directory grows without bound;
  eventually the primary crashes or refuses writes on a full disk. Adding disk
  or running `CHECKPOINT` does not help, the WAL keeps growing.
- **Fix** : the slot is the root cause, not the disk. Find it with
  `SELECT slot_name, active, wal_status FROM pg_replication_slots WHERE
  active = false`. Confirm the consumer is genuinely gone, then
  `SELECT pg_drop_replication_slot('<name>')`. WAL recycles at the next
  checkpoint. Rebuild that replica if it is still wanted.

## 2. No alert on replay_lag

- **Cause** : replication is set up but nothing monitors `replay_lag` or
  `pg_last_xact_replay_timestamp()`. Lag is assumed to be zero.
- **Symptom** : a read replica quietly serves stale data for hours; or a
  failover promotes a standby that was far behind, losing recent transactions.
  The lag is discovered only after users see wrong data.
- **Fix** : alert continuously, from the PRIMARY, on
  `pg_stat_replication.replay_lag` and on the byte diff
  `pg_wal_lsn_diff(pg_current_wal_lsn(), replay_lsn)`. On the STANDBY alert on
  `now() - pg_last_xact_replay_timestamp()`. A standby with NO row in
  `pg_stat_replication` is not connected at all, alert on that too.

## 3. max_slot_wal_keep_size left at the -1 default

- **Cause** : `max_slot_wal_keep_size` is never configured, so it stays at
  `-1`, which means slots may retain unlimited WAL.
- **Symptom** : there is no safety net. A single stuck slot (anti-pattern 1)
  can fill the disk and take the entire primary down, instead of just
  invalidating one slot.
- **Fix** : set `max_slot_wal_keep_size` to a value the `pg_wal` volume can
  absorb (for example `'64GB'`). A slot that exceeds it flips `wal_status` to
  `lost`: that one replica must be rebuilt, but the primary survives. Losing
  one replica is always better than losing the primary. Parameter is v13+.

## 4. Ignoring a stuck subscription

- **Cause** : a logical subscription hits a hard conflict (most often a
  duplicate key on the subscriber) and stops. The team assumes it will catch
  up by itself, as a slow-but-healthy subscription would.
- **Symptom** : the subscriber falls permanently behind; the SAME `ERROR`
  repeats in the subscriber log indefinitely; `pg_stat_subscription` shows no
  worker `pid` for that subscription.
- **Fix** : a conflict NEVER self-heals. Resolve it. Read the conflict from the
  subscriber log, then either fix the data on the subscriber so the apply
  succeeds (preferred, the worker resumes automatically) or `ALTER SUBSCRIPTION
  s SKIP (LSN = '<finish_lsn>')`. Create subscriptions
  `WITH (disable_on_error = true)` so a conflict disables them cleanly instead
  of looping.

## 5. Using SKIP as a first reflex

- **Cause** : on any stuck subscription the operator immediately runs
  `ALTER SUBSCRIPTION ... SKIP` to clear the error, without inspecting the
  conflict or trying to fix the data.
- **Symptom** : the error clears, but the subscriber is now missing rows. SKIP
  discards the WHOLE transaction at that finish LSN, including changes that did
  not conflict. The subscriber silently diverges from the publisher and later
  shows different query results.
- **Fix** : prefer resolving the conflict on the subscriber (delete the
  duplicate, fix permissions) so the original transaction applies intact. Use
  SKIP only when the transaction can genuinely be abandoned. After any SKIP,
  reconcile the affected tables, the divergence will not show up as an error.

## 6. hot_standby_feedback on without understanding the tradeoff

- **Cause** : `hot_standby_feedback = on` is enabled on a standby to stop
  `canceling statement due to conflict with recovery` errors, with no
  awareness of its cost on the primary.
- **Symptom** : query cancellations on the standby stop, but the PRIMARY starts
  bloating. Long-running standby queries hold back the primary's VACUUM, dead
  tuples accumulate, tables and indexes grow.
- **Fix** : understand the tradeoff before enabling it. `hot_standby_feedback`
  trades standby query cancellation for primary bloat. If standby queries are
  short, leave it off and tune `max_standby_streaming_delay`. If long analytic
  queries on the standby are essential, enable it but monitor primary bloat and
  keep those queries bounded. See postgres-impl-vacuum-bloat for the bloat side.
