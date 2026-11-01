---
name: postgres-errors-replication-lag
description: >
  Use when a replica is behind, a replication slot is bloating WAL, or a logical subscription is stuck.
  Prevents primary disk filling from an inactive slot, discovering lag only at failover time, and ignoring a stuck subscription that never self-heals after a hard conflict.
  Covers streaming lag monitoring (pg_stat_replication write/flush/replay lag, LSN diff), replication slot bloat (pg_replication_slots), max_slot_wal_keep_size safety cap, logical subscription lag + stuck-subscription recovery (conflict resolution, SKIP), lag root causes.
  Keywords: replication lag, pg_stat_replication, replay_lag, replication slot, pg_replication_slots, max_slot_wal_keep_size, could not start WAL streaming, subscription stuck, pg_stat_subscription, replica is behind, WAL not being removed, slot bloat, standby lag, logical replication stuck
license: MIT
compatibility: "Designed for Claude Code. Requires PostgreSQL 15, 16, or 17."
metadata:
  author: OpenAEC-Foundation
  version: "1.0"
---

# postgres-errors-replication-lag

## Quick Reference :

Three replication problems, three different views to inspect :

| Problem | Where to look | Key signal |
|---------|---------------|------------|
| Streaming replica behind | `pg_stat_replication` (on PRIMARY) | `replay_lag`, byte diff vs `replay_lsn` |
| Slot retaining too much WAL | `pg_replication_slots` (on PRIMARY) | `active = false`, `wal_status` not `reserved` |
| Logical subscription behind / stuck | `pg_stat_subscription` (on SUBSCRIBER) | stale `latest_end_time`, errors in subscriber log |

Two failures cause real outages :

1. **Inactive slot fills the primary disk.** A replication slot pins WAL from
   `restart_lsn` forward. If its consumer stops connecting, `restart_lsn` never
   advances and `pg_wal` grows without bound until the primary crashes on a
   full disk. ALWAYS set `max_slot_wal_keep_size` as a safety cap and ALWAYS
   alarm on inactive slots.

2. **A stuck logical subscription never self-heals.** A hard conflict (most
   often a duplicate key on the subscriber) stops the apply worker. It does NOT
   retry past the error. The same error repeats in the subscriber log forever
   until a human resolves the conflict or skips the transaction.

Lag is a time interval AND a byte distance. `replay_lag` (an `interval`) tells
you how stale the replica's data is. `pg_wal_lsn_diff(pg_current_wal_lsn(),
replay_lsn)` (bytes) tells you how much WAL the replica still has to apply.
Monitor both, BEFORE a failover, never after.

## When To Use This Skill :

ALWAYS use this skill when :
- A read replica returns stale data, or a standby is reported "behind"
- The primary's `pg_wal` directory is growing and disk is filling
- A standby logs `could not start WAL streaming` or cannot reconnect
- A logical subscription stopped applying changes
- The subscriber log shows a repeating `ERROR` from the apply worker
- Designing replication monitoring and alerting thresholds

NEVER use this skill for :
- Setting up replication from scratch (publications, base backup, slots)
- Connection or authentication failures unrelated to replication
  (use postgres-errors-connection-auth)
- WAL volume from normal write load with healthy slots (a capacity question,
  not a lag fault)

## Decision Trees :

### A replica is behind, where do I look :

```
Which kind of replication?
├── Streaming (physical standby)
│   │  Run on the PRIMARY:
│   │  SELECT * FROM pg_stat_replication;
│   ├── No row for the standby
│   │      -> standby is not connected. Check standby log for
│   │         "could not start WAL streaming". Slot dropped or WAL recycled.
│   ├── Row present, replay_lag growing, write_lag/flush_lag small
│   │      -> WAL arrives fine; REPLAY is slow. Slow standby disk, or a
│   │         long query on the standby blocking replay.
│   └── Row present, write_lag/flush_lag also growing
│          -> network or primary-side bottleneck shipping WAL.
│
└── Logical (publication / subscription)
       Run on the SUBSCRIBER:
       SELECT * FROM pg_stat_subscription;
       SELECT subname, subenabled FROM pg_subscription;
    ├── latest_end_time stale, worker pid present
    │      -> apply worker alive but slow. Check subscriber write capacity.
    └── No worker pid, or repeating ERROR in subscriber log
           -> subscription STUCK on a conflict. See recovery tree below.
```

### Slot is retaining WAL, is it dangerous :

```
On the PRIMARY: SELECT slot_name, active, wal_status, safe_wal_size
                FROM pg_replication_slots;
├── active = true,  wal_status = 'reserved'
│      -> healthy. Consumer connected, WAL within max_wal_size.
├── active = false, wal_status = 'reserved' or 'extended'
│      -> consumer is GONE but WAL is still being pinned. Disk at risk.
│         Reconnect the consumer, or drop the slot (see Pattern 3).
├── wal_status = 'unreserved'
│      -> required WAL is about to be removed at the next checkpoint.
│         The slot is on the edge. Act now.
└── wal_status = 'lost'
       -> the slot is dead. Its consumer can never resume from it.
          Drop the slot; the standby/subscriber must be rebuilt.
```

### Logical subscription stuck on a conflict :

```
Subscriber log shows a repeating ERROR (e.g. duplicate key).
├── The conflicting row on the subscriber is wrong / a duplicate
│      -> FIX THE DATA on the subscriber so apply can succeed,
│         then the worker retries automatically. PREFERRED.
└── The conflicting transaction must be abandoned
       -> read the finish LSN from the error CONTEXT line, then
          ALTER SUBSCRIPTION s SKIP (LSN = '<finish_lsn>');
          WARNING: SKIP drops the WHOLE transaction, including changes
          that did not conflict. The subscriber can become inconsistent.
```

## Patterns :

### Pattern 1 : Monitor streaming lag on the primary

ALWAYS : measure lag from the PRIMARY with `pg_stat_replication`, in both time
and bytes. NEVER : wait for a failover to discover a replica was stale.

```sql
-- Run on the PRIMARY. One row per connected standby.
SELECT
  client_addr,
  state,                              -- want 'streaming'
  replay_lag,                         -- interval: how stale the replica is
  pg_wal_lsn_diff(pg_current_wal_lsn(), replay_lsn) AS replay_bytes,
  sync_state
FROM pg_stat_replication;
```

```sql
-- Run on the STANDBY: age of the last replayed transaction.
SELECT now() - pg_last_xact_replay_timestamp() AS replication_delay;
```

WHY : `write_lag`, `flush_lag`, and `replay_lag` are time intervals for three
stages, WAL written, flushed, and applied on the standby. `replay_lag` is the
one that matters for read-replica staleness. A standby with no row in
`pg_stat_replication` is not connected at all. See references/methods.md for
every column.

### Pattern 2 : Cap WAL retained for slots

ALWAYS : set `max_slot_wal_keep_size` so a dead slot loses its WAL claim
instead of filling the disk. NEVER : leave it at the `-1` default in
production.

```sql
-- A slot that falls further behind than this loses its reservation:
-- wal_status flips to 'lost' and the disk is protected.
ALTER SYSTEM SET max_slot_wal_keep_size = '64GB';
SELECT pg_reload_conf();

-- Watch headroom per slot (bytes before it risks 'lost'):
SELECT slot_name, active, wal_status, safe_wal_size
FROM pg_replication_slots;
```

WHY : without a cap, an inactive slot pins WAL forever and the primary crashes
on a full `pg_wal`. With a cap, the slot becomes `lost` (recoverable: rebuild
that one replica) instead of taking the whole primary down. Losing one replica
beats losing the primary. `max_slot_wal_keep_size` is v13+.

### Pattern 3 : Drop an abandoned slot

ALWAYS : drop a slot whose consumer is permanently gone, after confirming it is
inactive. NEVER : drop a slot that is `active = true` or still has a real
consumer.

```sql
-- 1. Confirm the slot is abandoned (not active).
SELECT slot_name, slot_type, active, active_pid, restart_lsn, wal_status
FROM pg_replication_slots
WHERE slot_name = 'old_standby_slot';

-- 2. Only if active is false and the consumer is truly gone:
SELECT pg_drop_replication_slot('old_standby_slot');
```

WHY : the slot is the thing pinning WAL. Adding disk or running `CHECKPOINT`
does nothing while the slot holds `restart_lsn` back. Dropping the abandoned
slot is the root-cause fix; WAL is recycled at the next checkpoint. A standby
that still needs that slot must be re-cloned afterwards.

### Pattern 4 : Recover a stuck logical subscription

ALWAYS : resolve the conflict on the subscriber so the worker resumes on its
own. Use `SKIP` only when the transaction must be abandoned.

```
ERROR:  duplicate key value violates unique constraint "test_pkey"
DETAIL:  Key (c)=(1) already exists.
CONTEXT: processing remote data for replication origin "pg_16395" during
         "INSERT" for replication target relation "public.test"
         in transaction 725 finished at 0/14C0378
```

```sql
-- Option A (PREFERRED): fix the data, worker retries automatically.
DELETE FROM public.test WHERE c = 1;   -- remove the blocking duplicate

-- Option B: abandon the whole conflicting transaction.
-- finish LSN is the last value in the CONTEXT line: 0/14C0378
ALTER SUBSCRIPTION my_sub SKIP (LSN = '0/14C0378');   -- v15+
```

WHY : a conflict stops the apply worker and it does NOT self-heal, the error
repeats forever. Fixing the data lets apply succeed and replication catches up
cleanly. `SKIP` discards the ENTIRE transaction at that LSN, including
non-conflicting rows, so the subscriber can silently diverge. Create
subscriptions `WITH (disable_on_error = true)` so a conflict disables the
subscription cleanly instead of looping. See references/examples.md.

## Anti-Patterns :

(Full cause + symptom + fix in references/anti-patterns.md)

- Inactive replication slot left in place : unbounded WAL, primary disk full
- No alert on `replay_lag` : stale data discovered only at failover
- `max_slot_wal_keep_size` left at `-1` : no safety cap against slot bloat
- Ignoring a stuck subscription : it never self-heals after a hard conflict
- Using `SKIP` as a first reflex : silently makes the subscriber inconsistent
- `hot_standby_feedback = on` without understanding the bloat tradeoff

## Reference Links :

- [references/methods.md](references/methods.md) : pg_stat_replication, pg_replication_slots, pg_stat_subscription columns, lag functions, parameters
- [references/examples.md](references/examples.md) : Monitoring queries, slot diagnosis, stuck-subscription recovery, version-annotated
- [references/anti-patterns.md](references/anti-patterns.md) : Anti-patterns with cause, symptom, fix

## See Also :

- postgres-errors-connection-auth : standby cannot connect for non-lag reasons
- postgres-impl-vacuum-bloat : the bloat side of `hot_standby_feedback`
- postgres-core-architecture : WAL, LSN, and the replication substrate
- Vooronderzoek section : §9 (Replication)
- Official docs : https://www.postgresql.org/docs/17/warm-standby.html
- Official docs : https://www.postgresql.org/docs/17/logical-replication.html
- Official docs : https://www.postgresql.org/docs/17/monitoring-stats.html
