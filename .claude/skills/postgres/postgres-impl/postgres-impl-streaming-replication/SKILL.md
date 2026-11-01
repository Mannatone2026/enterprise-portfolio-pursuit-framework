---
name: postgres-impl-streaming-replication
description: >
  Use when setting up a hot-standby replica, choosing synchronous_commit durability, or debugging replica lag and query cancellation.
  Prevents disk filling from a physical slot without max_slot_wal_keep_size, primary stalls when a synchronous standby goes down, and standby query cancellation from conflicting vacuum.
  Covers pg_basebackup clone, primary_conninfo + standby.signal, hot_standby, physical replication slots, WAL archiving, synchronous_commit modes, synchronous_standby_names quorum, pg_promote, cascading replication, pg_stat_replication monitoring, hot_standby_feedback.
  Keywords: streaming replication, hot standby, pg_basebackup, replication slot, synchronous_commit, synchronous_standby_names, pg_promote, standby.signal, pg_stat_replication, replica lag, max_slot_wal_keep_size, query canceled on standby, disk full replication, how to set up a replica, failover
license: MIT
compatibility: "Designed for Claude Code. Requires PostgreSQL 15, 16, or 17."
metadata:
  author: OpenAEC-Foundation
  version: "1.0"
---

# postgres-impl-streaming-replication

## Quick Reference :

Streaming (physical) replication ships raw WAL records over TCP from a primary to one or more standbys. The standby is a **byte-identical copy of the whole cluster** , every database, role, and table , and replays WAL continuously. It is not selective ; for row-level, cross-version replication use logical replication instead.

Setup is three steps : (1) prepare the primary (`wal_level = replica`, a `REPLICATION` role, a `pg_hba.conf` replication line), (2) clone the data directory with `pg_basebackup ... -R`, (3) start the standby , the `standby.signal` file puts it in standby mode and it streams from `primary_conninfo`.

A standby with `hot_standby = on` (the default) serves read-only queries. A **physical replication slot** stops the primary from recycling WAL the standby has not consumed yet , but a slot whose consumer is gone grows `pg_wal` without bound. ALWAYS set `max_slot_wal_keep_size` (v13+) so a dead slot is invalidated instead of filling the disk.

`synchronous_commit` trades durability against commit latency. The default `on` waits for the local WAL flush ; with `synchronous_standby_names` set it also waits for the standby. NEVER point `synchronous_standby_names` at a single standby with no quorum , if that standby goes down, every commit on the primary stalls.

## When To Use This Skill :

ALWAYS use this skill when :
- Setting up a hot-standby read replica or a high-availability failover pair
- Choosing a `synchronous_commit` durability level
- A replica is lagging, or a standby query is being canceled
- `pg_wal` is filling up because of a replication slot
- Planning promotion / failover or cascading replication

NEVER use this skill for :
- Row-level or cross-major-version replication, publications/subscriptions : that is logical replication , see `postgres-impl-logical-replication`
- Point-in-time recovery and `pg_basebackup` for backup (not replica) : see `postgres-impl-backup-restore`
- WAL fundamentals and the durability rule : see `postgres-core-architecture`

## Decision Trees :

### Physical streaming or logical replication? :

```
What do you need to replicate?
├── The entire cluster, byte-identical, same major version, for HA / read scaling :
│       PHYSICAL streaming replication  (this skill)
├── Specific tables, or across major versions / platforms, or filtered rows :
│       LOGICAL replication  (see postgres-impl-logical-replication)
└── A one-off restorable snapshot, not a live follower :
        pg_basebackup as backup  (see postgres-impl-backup-restore)
```

### synchronous_commit durability ladder :

```
How much data loss is acceptable on a crash?
├── Throughput matters more than the last few commits (bulk load, cache) :
│       off          , no wait at all ; recent commits lost on crash
├── Survive a primary OS crash, standby state irrelevant (DEFAULT) :
│       on           , wait for local WAL fsync (+ standby fsync if sync standby set)
├── Standby may lose commits only on a standby OS crash :
│       remote_write , wait for standby OS write, not its fsync
└── Zero data loss AND the commit must be visible on the standby :
        remote_apply , wait for the standby to REPLAY the commit
                       (highest latency ; needs synchronous_standby_names)
```

### WAL retention for a standby :

```
How does the primary keep WAL the standby still needs?
├── Replication slot (RECOMMENDED) :
│       retains exactly what the standby has not consumed.
│       ALWAYS pair with max_slot_wal_keep_size so a dead slot
│       is invalidated instead of filling pg_wal.
├── wal_keep_size :
│       retains a fixed amount of recent WAL ; simple, but a slow
│       standby beyond the window must re-base or use archive.
└── archive_command + restore_command :
        the standby fetches missed WAL from the archive ;
        the durable fallback, combine with a slot or wal_keep_size.
```

## Patterns :

### Pattern 1 : Prepare the primary

ALWAYS create a dedicated `REPLICATION`-attribute role and add a `replication` line to `pg_hba.conf`.
NEVER reuse a superuser for the replication connection , the role needs `REPLICATION` and `LOGIN`, nothing more.

```sql
-- On the primary
CREATE ROLE repuser WITH REPLICATION LOGIN PASSWORD 'strong-secret';
```

```ini
# postgresql.conf  (wal_level=replica and max_wal_senders=10 are defaults since v10)
wal_level = replica
max_wal_senders = 10
max_replication_slots = 10

# pg_hba.conf  , "replication" is a literal keyword, not a database name
host    replication    repuser    10.0.0.0/24    scram-sha-256
```

WHY : `wal_level = replica` (the default) logs enough for WAL shipping. The `replication` keyword in `pg_hba.conf` is a special pseudo-database that ONLY matches replication connections , a normal `host all` line does not grant replication. Use `scram-sha-256`, never `trust` or `md5`, for the replication role. Source : postgresql.org/docs/17/warm-standby.html.

### Pattern 2 : Clone the standby with pg_basebackup

ALWAYS use `pg_basebackup -R` , it writes `standby.signal` and the connection settings for you.
NEVER hand-copy the primary's data directory while it is running , `pg_basebackup` takes a consistent online base backup; a raw `cp` does not.

```bash
pg_basebackup \
  --host=primary.internal --username=repuser \
  --pgdata=/var/lib/postgresql/17/standby \
  --wal-method=stream \
  --write-recovery-conf \
  --progress
```

WHY : `--wal-method=stream` (`-X stream`) streams WAL during the backup so the result is immediately consistent. `--write-recovery-conf` (`-R`) creates an empty `standby.signal` file in the new data directory and appends `primary_conninfo` (and `primary_slot_name`, if `-S` was given) to `postgresql.auto.conf`. Start the cluster on that data directory and it comes up as a streaming standby. Source : postgresql.org/docs/17/app-pgbasebackup.html.

### Pattern 3 : Physical replication slot with a size cap

ALWAYS create a physical slot AND set `max_slot_wal_keep_size` together.
NEVER run a slot uncapped , if the standby disconnects, the slot pins WAL forever and fills `pg_wal` until the primary crashes with a full disk.

```sql
-- On the primary : create the slot
SELECT pg_create_physical_replication_slot('standby1_slot');
```

```ini
# Primary postgresql.conf : cap how much WAL a slot may pin
max_slot_wal_keep_size = 10GB

# Standby postgresql.auto.conf (written by pg_basebackup -S, or set manually)
primary_slot_name = 'standby1_slot'
```

WHY : a slot makes the primary retain exactly the WAL the standby has not confirmed , better than `wal_keep_size` guesswork. The danger is the inverse : a slot whose consumer never returns retains WAL without limit. `max_slot_wal_keep_size` (v13+) caps that , once a slot would exceed the cap it is **invalidated** (`pg_replication_slots.wal_status = 'lost'`) and WAL is freed. The standby then needs a re-base or the archive. A bounded outage beats a full disk. Source : postgresql.org/docs/17/warm-standby.html.

### Pattern 4 : Synchronous replication with a quorum

ALWAYS use `ANY n (...)` or `FIRST n (...)` with more standbys than `n`, so one standby can fail without stalling commits.
NEVER set `synchronous_standby_names` to a single standby name , losing that one standby blocks every commit on the primary.

```ini
# Primary postgresql.conf
synchronous_commit = on

# Quorum : wait for ANY 1 of these 2 standbys to confirm
synchronous_standby_names = 'ANY 1 (standby1, standby2)'

# Priority : wait for the FIRST available of the list
# synchronous_standby_names = 'FIRST 1 (standby1, standby2)'
```

WHY : with `synchronous_standby_names` set, `synchronous_commit = on` makes a commit wait for the named standbys to flush WAL to disk. `ANY n (list)` is quorum-based , any `n` of the listed standbys satisfy it. `FIRST n (list)` is priority-based , the first `n` available in list order. A bare list `'s1, s2'` is the legacy form, equal to `FIRST 1`. Always list MORE standbys than `n` so a single failure is absorbed. The standby names match `application_name` in each standby's `primary_conninfo`. Source : postgresql.org/docs/17/warm-standby.html.

### Pattern 5 : Hot standby reads and recovery-conflict tuning

ALWAYS decide between `hot_standby_feedback` (no cancellations, primary bloat risk) and `max_standby_streaming_delay` (bounded lag, queries cancelled).
NEVER run long analytic queries on a default-tuned standby and expect them to finish , WAL replay cancels conflicting queries after 30 seconds.

```ini
# Standby postgresql.conf
hot_standby = on                      # default ; serves read-only queries

# Option A : let standby queries finish, tell the primary to hold tuples
hot_standby_feedback = on

# Option B : bound replay lag instead, accept query cancellation
max_standby_streaming_delay = 30s      # default
```

WHY : a hot standby replays WAL while serving reads. When replay hits a change that conflicts with a running query (a VACUUM removing tuples the query still sees, an `ACCESS EXCLUSIVE` lock), PostgreSQL cancels the query after `max_standby_streaming_delay` , SQLSTATE `40001`, "canceling statement due to conflict with recovery". `hot_standby_feedback = on` sends the standby's oldest `xmin` upstream so the primary's VACUUM keeps those tuples , no cancellation, but the primary bloats if a standby query runs long. Pick one tradeoff per workload. Source : postgresql.org/docs/17/hot-standby.html.

### Pattern 6 : Promotion and failover

ALWAYS promote with `pg_promote()` or `pg_ctl promote`.
NEVER expect a `promote_trigger_file` , that parameter was removed in v16.

```sql
-- On the standby : promote it to a read-write primary
SELECT pg_promote(wait => true, wait_seconds => 60);

-- Verify : in_recovery is now false
SELECT pg_is_in_recovery();
```

```bash
# Equivalent from the shell
pg_ctl promote -D /var/lib/postgresql/17/standby
```

WHY : promotion ends recovery , the standby finishes replaying any WAL already available, then switches to normal read-write operation on a new timeline. It does NOT contact the old primary, so the old primary MUST be fenced off first to avoid a split brain. Other standbys following the promoted node need `recovery_target_timeline = 'latest'` (the default) to follow the timeline switch. Source : postgresql.org/docs/17/warm-standby.html.

### Pattern 7 : Cascading replication

ALWAYS enable `max_wal_senders` and `hot_standby` on a standby that will feed other standbys.
NEVER expect synchronous replication through a cascade , cascaded streaming is asynchronous only.

```ini
# Cascading (intermediate) standby postgresql.conf
hot_standby = on
max_wal_senders = 10

# Downstream standby : point primary_conninfo at the cascading standby
primary_conninfo = 'host=cascade.internal user=repuser application_name=leaf1'
```

WHY : a cascading standby relays WAL downstream, cutting the number of direct streaming connections to the primary. It forwards both WAL streamed from the primary and WAL restored from archive, and keeps feeding downstream servers even if its own upstream link drops. Cascaded links are always asynchronous , `synchronous_standby_names` on the primary has no effect on standbys behind a cascade. Source : postgresql.org/docs/17/warm-standby.html.

### Pattern 8 : Monitor lag and slot health

ALWAYS alarm on both replay lag (`pg_stat_replication`) and slot retention (`pg_replication_slots`).
NEVER assume a connected standby is current , a standby can stream WAL yet fall far behind on replay.

```sql
-- On the primary : per-standby lag (LSN positions + time intervals)
SELECT application_name, state,
       sent_lsn, write_lsn, flush_lsn, replay_lsn,
       write_lag, flush_lag, replay_lag
FROM pg_stat_replication;

-- On the primary : WAL pinned by each slot ; watch wal_status
SELECT slot_name, active, wal_status,
       pg_size_pretty(pg_wal_lsn_diff(pg_current_wal_lsn(), restart_lsn)) AS retained
FROM pg_replication_slots;

-- On the standby : how far behind replay is, in time
SELECT now() - pg_last_xact_replay_timestamp() AS replay_delay;
```

WHY : `replay_lsn` trailing `sent_lsn` means WAL has arrived but is not applied yet , reads on the standby see stale data. `pg_replication_slots.wal_status` of `extended` or `unreserved` warns that a slot is approaching `max_slot_wal_keep_size` ; `lost` means the slot was already invalidated. Monitoring both is the only early warning before a disk fills or a failover loses data. Source : postgresql.org/docs/17/warm-standby.html.

## Anti-Patterns :

(One-liners ; full diagnosis + fix in `references/anti-patterns.md`.)

- Physical slot with no `max_slot_wal_keep_size` , a disconnected standby pins WAL until `pg_wal` fills the disk and the primary crashes.
- `synchronous_standby_names` naming a single standby , that standby going down stalls every commit on the primary.
- `synchronous_commit = remote_apply` with one standby , primary commit latency is hostage to standby replay.
- Long query on a default-tuned hot standby , canceled after `max_standby_streaming_delay` : SQLSTATE `40001`.
- `hot_standby_feedback = on` ignored as a bloat source , long standby queries hold back the primary's VACUUM.
- Raw `cp` of a running primary's data directory , inconsistent copy ; use `pg_basebackup`.
- `pg_hba.conf` has `host all` but no `host replication` line , the replication connection is rejected.
- Promoting a standby without fencing the old primary , split brain, divergent timelines.
- Expecting `promote_trigger_file` to work , removed in v16 ; use `pg_promote()`.
- Monitoring connection state but not `replay_lsn` , a "connected" standby can still serve badly stale reads.
- `pg_dump` run against a hot standby during heavy replay , conflicts can cancel the dump : SQLSTATE `40001`.

## Reference Links :

- [references/methods.md](references/methods.md) : Configuration parameters, `pg_basebackup` options, replication functions, `pg_stat_replication` / `pg_replication_slots` columns
- [references/examples.md](references/examples.md) : End-to-end standby setup, slot creation, synchronous config, promotion, cascading, monitoring queries
- [references/anti-patterns.md](references/anti-patterns.md) : Replication anti-patterns with cause, symptom, SQLSTATE (where applicable), and fix

## See Also :

- `postgres-impl-logical-replication` : row-level, selective, cross-version replication via publications and subscriptions
- `postgres-impl-backup-restore` : `pg_basebackup` and WAL archiving for point-in-time recovery rather than a live replica
- `postgres-core-architecture` : the WAL durability rule that streaming replication is built on
- `postgres-errors-connection-auth` : `pg_hba.conf` matching and SCRAM authentication for the replication role
- Vooronderzoek section : §9 (Replication)
- Official docs : https://www.postgresql.org/docs/17/warm-standby.html, https://www.postgresql.org/docs/17/hot-standby.html, https://www.postgresql.org/docs/17/app-pgbasebackup.html
