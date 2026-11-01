# postgres-impl-streaming-replication : Examples

Verified, version-annotated streaming-replication examples. Verified against postgresql.org/docs/17 (Last Verified : 2026-05-19). All steps work on PostgreSQL 15, 16, 17 unless a version tag says otherwise.

## End-to-end : set up a hot-standby replica

### Step 1 , prepare the primary

```sql
-- v15+ : a dedicated replication role, no superuser
CREATE ROLE repuser WITH REPLICATION LOGIN PASSWORD 'strong-secret';
```

```ini
# v15+ : primary postgresql.conf (these are also the defaults since v10)
wal_level = replica
max_wal_senders = 10
max_replication_slots = 10
max_slot_wal_keep_size = 10GB        # v13+ : cap WAL pinned by any slot
```

```
# v15+ : primary pg_hba.conf , "replication" is a literal keyword
host    replication    repuser    10.0.0.0/24    scram-sha-256
```

```sql
-- v15+ : reload so pg_hba.conf and reloadable GUCs take effect
SELECT pg_reload_conf();
```

### Step 2 , create a physical replication slot on the primary

```sql
-- v15+
SELECT pg_create_physical_replication_slot('standby1_slot');
SELECT slot_name, slot_type, active FROM pg_replication_slots;
```

### Step 3 , clone the data directory with pg_basebackup

```bash
# v15+ : run on the standby host. -R writes standby.signal + primary_conninfo.
# -S + -C uses (and could create) the slot.
pg_basebackup \
  --host=primary.internal --username=repuser \
  --pgdata=/var/lib/postgresql/17/standby \
  --wal-method=stream \
  --slot=standby1_slot \
  --write-recovery-conf \
  --checkpoint=fast \
  --progress
```

### Step 4 , set the application_name and start the standby

```ini
# v15+ : standby postgresql.auto.conf , pg_basebackup -R already wrote
# primary_conninfo and primary_slot_name ; add application_name for sync naming
primary_conninfo = 'host=primary.internal user=repuser application_name=standby1'
primary_slot_name = 'standby1_slot'
hot_standby = on
```

```bash
# v15+ : start ; standby.signal puts it in standby mode
pg_ctl start -D /var/lib/postgresql/17/standby
```

### Step 5 , verify

```sql
-- v15+ : on the standby , expect true
SELECT pg_is_in_recovery();

-- v15+ : on the primary , expect a row for standby1 in state 'streaming'
SELECT application_name, state, sync_state FROM pg_stat_replication;
```

## Synchronous replication

```ini
# v15+ : primary postgresql.conf
synchronous_commit = on

# Quorum : commit waits for ANY 1 of the 2 standbys
synchronous_standby_names = 'ANY 1 (standby1, standby2)'

# Priority alternative : first available of the list
# synchronous_standby_names = 'FIRST 1 (standby1, standby2)'
```

```sql
-- v15+ : apply without restart
SELECT pg_reload_conf();

-- v15+ : confirm sync_state is 'sync' or 'quorum'
SELECT application_name, sync_state FROM pg_stat_replication;
```

## Highest-durability commit (zero loss, visible on standby)

```sql
-- v15+ : per-transaction override , this transaction waits for standby replay
BEGIN;
SET LOCAL synchronous_commit = remote_apply;
INSERT INTO ledger (account, amount) VALUES ($1, $2);
COMMIT;
```

## Hot-standby query-conflict tuning

```ini
# v15+ : standby postgresql.conf

# Option A , standby queries always finish ; primary holds tuples (bloat risk)
hot_standby_feedback = on

# Option B , bound replay lag ; conflicting queries are cancelled
max_standby_streaming_delay = 30s
```

## Promotion / failover

```sql
-- v15+ : on the standby , promote to a read-write primary
SELECT pg_promote(wait => true, wait_seconds => 60);

-- v15+ : confirm , expect false
SELECT pg_is_in_recovery();
```

```bash
# v15+ : equivalent from the shell
pg_ctl promote -D /var/lib/postgresql/17/standby
```

Fence the old primary BEFORE promotion to avoid a split brain. `promote_trigger_file` was removed in v16 , use the methods above.

## Cascading replication

```ini
# v15+ : cascading (intermediate) standby postgresql.conf
hot_standby = on
max_wal_senders = 10
```

```ini
# v15+ : downstream leaf standby , point at the cascading standby
primary_conninfo = 'host=cascade.internal user=repuser application_name=leaf1'
```

Cascaded streaming is asynchronous , `synchronous_standby_names` on the primary does not reach standbys behind a cascade.

## Monitoring queries

### Replication lag (run on the primary)

```sql
-- v15+
SELECT application_name,
       state,
       pg_wal_lsn_diff(pg_current_wal_lsn(), sent_lsn)   AS pending_send_bytes,
       pg_wal_lsn_diff(sent_lsn, replay_lsn)             AS unreplayed_bytes,
       write_lag, flush_lag, replay_lag
FROM pg_stat_replication;
```

### Slot retention and health (run on the primary)

```sql
-- v15+ : watch wal_status , 'lost' means the slot was invalidated
SELECT slot_name,
       active,
       wal_status,
       pg_size_pretty(pg_wal_lsn_diff(pg_current_wal_lsn(), restart_lsn)) AS retained_wal,
       pg_size_pretty(safe_wal_size) AS headroom
FROM pg_replication_slots;
```

### Replay delay in time (run on the standby)

```sql
-- v15+ : how far behind the standby's replay clock is
SELECT now() - pg_last_xact_replay_timestamp() AS replay_delay,
       pg_last_wal_receive_lsn()               AS received,
       pg_last_wal_replay_lsn()                AS replayed;
```

## Drop a slot after decommissioning a standby

```sql
-- v15+ : a stale slot keeps pinning WAL , drop it once the standby is gone.
-- The slot must be inactive (no consumer connected).
SELECT pg_drop_replication_slot('standby1_slot');
```

## WAL archiving as the durable fallback

```ini
# v15+ : primary postgresql.conf
archive_mode = on
archive_command = 'test ! -f /archive/%f && cp %p /archive/%f'
```

```ini
# v15+ : standby postgresql.conf , fetch missed WAL from the archive
restore_command = 'cp /archive/%f %p'
```
