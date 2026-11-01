# postgres-errors-replication-lag : examples

Working, version-annotated queries and recovery procedures. Verified against
PostgreSQL 17 (monitoring-stats.html, view-pg-replication-slots.html,
warm-standby.html, logical-replication.html). Behavior is the same on v15, v16,
v17 unless a version note says otherwise.

## Example 1 : full streaming-lag dashboard query (PRIMARY) : v15+

```sql
-- Run on the PRIMARY. One row per connected standby.
SELECT
  application_name,
  client_addr,
  state,                                         -- want 'streaming'
  sync_state,
  write_lag,
  flush_lag,
  replay_lag,
  pg_size_pretty(
    pg_wal_lsn_diff(pg_current_wal_lsn(), sent_lsn))   AS pending_send,
  pg_size_pretty(
    pg_wal_lsn_diff(pg_current_wal_lsn(), replay_lsn)) AS pending_replay
FROM pg_stat_replication
ORDER BY replay_lag DESC NULLS LAST;
```

Read it as : `pending_replay` is how much WAL the standby still owes. A growing
`replay_lag` with small `write_lag`/`flush_lag` means WAL arrives fine but
REPLAY is slow.

## Example 2 : replica staleness from the standby side : v15+

```sql
-- Run on the STANDBY.
SELECT
  pg_is_in_recovery()                              AS in_recovery,
  pg_last_wal_receive_lsn()                        AS received,
  pg_last_wal_replay_lsn()                         AS replayed,
  pg_last_xact_replay_timestamp()                  AS last_xact,
  now() - pg_last_xact_replay_timestamp()          AS staleness;
```

If `received` advances but `replayed` does not, recovery is blocked, commonly
by a long-running query on the standby holding back replay. Alert when
`staleness` exceeds the application's tolerance.

## Example 3 : diagnose a slot that is bloating WAL (PRIMARY) : v15+

```sql
SELECT
  slot_name,
  slot_type,
  active,
  active_pid,
  wal_status,                                      -- reserved/extended/unreserved/lost
  pg_size_pretty(safe_wal_size)                    AS headroom,
  pg_size_pretty(
    pg_wal_lsn_diff(pg_current_wal_lsn(), restart_lsn)) AS wal_pinned
FROM pg_replication_slots
ORDER BY pg_wal_lsn_diff(pg_current_wal_lsn(), restart_lsn) DESC;
```

```sql
-- v17+: when did the slot go idle, and why was it invalidated?
SELECT slot_name, active, inactive_since, invalidation_reason
FROM pg_replication_slots
WHERE active = false;
```

`active = false` with a large `wal_pinned` is the disk-fill risk. A `wal_status`
of `unreserved` means WAL is about to be dropped at the next checkpoint.

## Example 4 : set the WAL-retention safety cap : v15+

```sql
-- max_slot_wal_keep_size is v13+. Cap WAL retained for slots so a dead
-- slot loses its claim instead of filling pg_wal.
ALTER SYSTEM SET max_slot_wal_keep_size = '64GB';
SELECT pg_reload_conf();                           -- no restart needed

-- verify
SHOW max_slot_wal_keep_size;
```

After this, a slot that falls more than 64GB behind flips `wal_status` to
`lost`. That replica must be rebuilt, but the primary disk is safe.

## Example 5 : drop an abandoned slot (PRIMARY) : v15+

```sql
-- 1. Confirm the slot is truly abandoned.
SELECT slot_name, active, active_pid, wal_status,
       pg_size_pretty(
         pg_wal_lsn_diff(pg_current_wal_lsn(), restart_lsn)) AS wal_pinned
FROM pg_replication_slots
WHERE slot_name = 'standby2_slot';
-- active must be false and the standby genuinely decommissioned.

-- 2. Drop it. WAL recycles at the next checkpoint.
SELECT pg_drop_replication_slot('standby2_slot');

-- 3. (optional) force WAL cleanup immediately.
CHECKPOINT;
```

NEVER drop a slot whose `active` is `true`, or whose standby/subscriber you
still intend to keep. The standby cannot reconnect to a dropped slot and the
standby log will show `could not start WAL streaming` with
`replication slot "standby2_slot" does not exist`.

## Example 6 : recover a stuck logical subscription (SUBSCRIBER) : v15+

The subscriber log shows the apply worker failing on the same transaction
repeatedly :

```
ERROR:  duplicate key value violates unique constraint "test_pkey"
DETAIL:  Key (c)=(1) already exists.
CONTEXT: processing remote data for replication origin "pg_16395" during
         "INSERT" for replication target relation "public.test"
         in transaction 725 finished at 0/14C0378
```

Option A, PREFERRED, resolve the conflict so apply succeeds :

```sql
-- on the SUBSCRIBER: remove the row blocking the incoming INSERT
DELETE FROM public.test WHERE c = 1;
-- the apply worker retries on its own; replication catches up cleanly
```

Option B, abandon the whole conflicting transaction with SKIP :

```sql
-- finish LSN is the last value on the CONTEXT line: 0/14C0378
ALTER SUBSCRIPTION my_sub SKIP (LSN = '0/14C0378');     -- v15+
```

Option C, low-level skip via the replication origin :

```sql
ALTER SUBSCRIPTION my_sub DISABLE;
-- origin name "pg_16395" from the error; advance to finish LSN + 1
SELECT pg_replication_origin_advance('pg_16395', '0/14C0379');
ALTER SUBSCRIPTION my_sub ENABLE;
```

SKIP and origin-advance discard the ENTIRE transaction at that LSN, including
rows that did not conflict. Use them only when the transaction can truly be
abandoned; otherwise the subscriber silently diverges from the publisher.

## Example 7 : auto-disable on conflict instead of looping : v15+

```sql
-- a conflict disables the subscription cleanly rather than repeating
-- the error forever; you re-enable it after resolving the conflict
CREATE SUBSCRIPTION my_sub
  CONNECTION 'host=publisher dbname=app'
  PUBLICATION my_pub
  WITH (disable_on_error = true);

-- on an existing subscription
ALTER SUBSCRIPTION my_sub SET (disable_on_error = true);
```

## Example 8 : monitor logical subscription lag (SUBSCRIBER) : v15+

```sql
SELECT
  s.subname,
  st.worker_type,                                  -- v16+
  st.pid,
  st.received_lsn,
  st.latest_end_lsn,
  now() - st.latest_end_time                       AS report_age
FROM pg_subscription s
LEFT JOIN pg_stat_subscription st ON st.subid = s.oid;
```

A NULL `pid` means no worker is running : the subscription is disabled or
stuck. A growing `report_age` with a live `pid` means the worker is slow,
check subscriber write capacity and indexes on the target tables.
