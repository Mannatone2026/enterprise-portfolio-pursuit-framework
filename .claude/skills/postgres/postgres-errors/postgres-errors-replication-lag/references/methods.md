# postgres-errors-replication-lag : methods

Views, functions, and parameters for diagnosing replication lag and slot bloat.
Verified against PostgreSQL 17 documentation (monitoring-stats.html,
view-pg-replication-slots.html, warm-standby.html, logical-replication.html).

## pg_stat_replication (query on the PRIMARY) :

One row per WAL sender, i.e. per connected standby. A standby with no row here
is NOT connected.

| Column | Type | Meaning |
|--------|------|---------|
| `pid` | integer | process ID of the WAL sender |
| `client_addr` | inet | IP of the standby; NULL means Unix-socket local |
| `state` | text | `startup`, `catchup`, `streaming`, `backup`, `stopping`. Healthy steady state is `streaming` |
| `sent_lsn` | pg_lsn | last WAL location sent on this connection |
| `write_lsn` | pg_lsn | last WAL location written to disk by the standby |
| `flush_lsn` | pg_lsn | last WAL location flushed to disk by the standby |
| `replay_lsn` | pg_lsn | last WAL location replayed into the database on the standby |
| `write_lag` | interval | time between local flush and the standby reporting it WRITTEN. Gauges `synchronous_commit = remote_write` delay |
| `flush_lag` | interval | time between local flush and the standby reporting it WRITTEN and FLUSHED. Gauges `synchronous_commit = on` delay |
| `replay_lag` | interval | time between local flush and the standby reporting it WRITTEN, FLUSHED and APPLIED. Gauges `synchronous_commit = remote_apply` delay |
| `sync_state` | text | `async`, `potential`, `sync`, `quorum` |

`replay_lag` is the staleness a read replica actually exposes to queries. If
`write_lag`/`flush_lag` stay small while `replay_lag` grows, WAL arrives fine
and REPLAY is the bottleneck (slow standby disk, or a long query on the standby
blocking recovery).

## Lag in bytes :

The lag columns are time intervals. To measure outstanding WAL volume, diff
LSNs with `pg_wal_lsn_diff(lsn1, lsn2)` which returns bytes :

```sql
-- on the PRIMARY, bytes the standby still has to replay
SELECT client_addr,
       pg_wal_lsn_diff(pg_current_wal_lsn(), replay_lsn) AS replay_bytes
FROM pg_stat_replication;
```

## Standby-side functions (query on the STANDBY) :

| Function | Returns | Use |
|----------|---------|-----|
| `pg_last_wal_receive_lsn()` | pg_lsn | last WAL location received and synced from the primary |
| `pg_last_wal_replay_lsn()` | pg_lsn | last WAL location replayed during recovery |
| `pg_last_xact_replay_timestamp()` | timestamptz | commit timestamp of the last transaction replayed |
| `pg_is_in_recovery()` | bool | true while the server is a standby |

```sql
-- staleness of the replica as a wall-clock age
SELECT now() - pg_last_xact_replay_timestamp() AS replication_delay;
```

## Primary-side WAL functions :

| Function | Returns | Use |
|----------|---------|-----|
| `pg_current_wal_lsn()` | pg_lsn | current WAL write location on the primary |
| `pg_create_physical_replication_slot(name)` | record | create a physical slot |
| `pg_drop_replication_slot(name)` | void | drop a slot (physical or logical) |

## pg_replication_slots (query on the PRIMARY) :

Configuration and state of every slot. A slot pins WAL from `restart_lsn`
forward, so an inactive slot is the classic cause of unbounded `pg_wal` growth.

| Column | Type | Meaning |
|--------|------|---------|
| `slot_name` | name | cluster-wide slot identifier |
| `slot_type` | text | `physical` or `logical` |
| `active` | bool | true if the slot is currently being streamed |
| `active_pid` | integer | PID of the session using the slot, else NULL |
| `restart_lsn` | pg_lsn | oldest WAL the consumer might still need; WAL below this is NOT removed at checkpoint unless it falls more than `max_slot_wal_keep_size` behind |
| `confirmed_flush_lsn` | pg_lsn | LSN the logical consumer confirmed receiving; NULL for physical slots |
| `wal_status` | text | `reserved`, `extended`, `unreserved`, `lost` (see below) |
| `safe_wal_size` | int8 | bytes that can still be written before the slot risks `lost`; NULL for lost slots and when `max_slot_wal_keep_size = -1` |
| `two_phase` | bool | true if the slot decodes prepared transactions |
| `inactive_since` | timestamptz | when the slot became inactive (v17+) |
| `invalidation_reason` | text | why the slot was invalidated, if it was (v17+) |

### wal_status values :

| Value | Meaning |
|-------|---------|
| `reserved` | claimed WAL is within `max_wal_size`. Healthy. |
| `extended` | `max_wal_size` exceeded but WAL still retained by the slot or `wal_keep_size`. |
| `unreserved` | the slot no longer retains the required WAL; some will be removed at the next checkpoint. On the edge. |
| `lost` | the slot is no longer usable; its consumer can never resume from it. |

## pg_stat_subscription (query on the SUBSCRIBER) :

| Column | Type | Meaning |
|--------|------|---------|
| `subid` | oid | subscription OID |
| `subname` | name | subscription name |
| `worker_type` | text | `apply`, `parallel apply`, `table synchronization` (v16+) |
| `pid` | integer | PID of the subscription worker process |
| `leader_pid` | integer | leader apply worker PID for a parallel apply worker, else NULL |
| `received_lsn` | pg_lsn | last WAL location received |
| `last_msg_send_time` | timestamptz | send time of the last message from the publisher |
| `last_msg_receipt_time` | timestamptz | receipt time of the last message |
| `latest_end_lsn` | pg_lsn | last WAL location reported back to the publisher |
| `latest_end_time` | timestamptz | time of that last report |

A stale `latest_end_time` with a live `pid` means the worker is slow. No `pid`
plus a repeating `ERROR` in the subscriber log means the subscription is stuck.
`pg_stat_replication_slots` (a separate view) holds spill/stream byte counters
for logical decoding tuning.

## Stuck-subscription recovery commands :

| Command | Version | Use |
|---------|---------|-----|
| `ALTER SUBSCRIPTION s DISABLE;` | v10+ | stop the apply worker |
| `ALTER SUBSCRIPTION s ENABLE;` | v10+ | resume after fixing the conflict |
| `ALTER SUBSCRIPTION s SKIP (LSN = '0/...');` | v15+ | discard the whole transaction at that finish LSN |
| `pg_replication_origin_advance(node_name, lsn)` | v9.5+ | low-level skip: advance the origin past the conflict |
| `CREATE SUBSCRIPTION ... WITH (disable_on_error = true)` | v15+ | auto-disable on a conflict instead of looping |

The finish LSN and the replication origin name both appear in the error's
`CONTEXT` line (`replication origin "pg_NNNNN"`, `finished at 0/...`).
`pg_replication_origin_advance` takes the NEXT LSN (finish LSN + 1).

## Configuration parameters :

| Parameter | Default | Scope | Purpose |
|-----------|---------|-------|---------|
| `max_slot_wal_keep_size` | `-1` (unlimited) | primary | cap on WAL retained for slots; a slot exceeding it goes `lost` (v13+) |
| `wal_keep_size` | `0` | primary | minimum WAL kept for standbys without a slot |
| `hot_standby_feedback` | `off` | standby | standby reports its xmin to the primary so VACUUM does not remove rows a standby query still needs |
| `max_standby_streaming_delay` | `30s` | standby | how long recovery waits for a conflicting query before cancelling it |
| `max_wal_senders` | `10` | primary | maximum concurrent WAL sender processes |
| `logical_decoding_work_mem` | `64MB` | primary | memory before logical decoding spills to disk |

`hot_standby_feedback = on` prevents `canceling statement due to conflict with
recovery` on the standby, at the cost of table bloat on the primary, because
the primary's VACUUM must keep rows the standby's long query still references.
