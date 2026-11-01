# postgres-impl-streaming-replication : Methods Reference

API surface for PostgreSQL physical streaming replication. Verified against postgresql.org/docs/17 (Last Verified : 2026-05-19). Applies to PostgreSQL 15, 16, 17.

## Primary configuration parameters

| Parameter | Default | Purpose |
|---|---|---|
| `wal_level` | `replica` | `replica` or higher is required to ship WAL |
| `max_wal_senders` | 10 | concurrent WAL-sender processes (one per standby + base backup) |
| `max_replication_slots` | 10 | maximum number of replication slots |
| `wal_keep_size` | 0 | MB of recent WAL retained for standbys without a slot |
| `max_slot_wal_keep_size` | -1 (unlimited) | cap on WAL a slot may pin ; v13+ ; over-cap slots are invalidated |
| `synchronous_commit` | `on` | per-commit durability level (see table below) |
| `synchronous_standby_names` | empty | which standbys make a commit synchronous |
| `archive_mode` | `off` | enable continuous WAL archiving |
| `archive_command` | empty | shell command to archive one WAL file |

## Standby configuration parameters

| Parameter | Default | Purpose |
|---|---|---|
| `primary_conninfo` | empty | libpq connection string to the upstream server |
| `primary_slot_name` | empty | replication slot to use on the upstream server |
| `hot_standby` | `on` | allow read-only queries during recovery |
| `hot_standby_feedback` | `off` | report standby `xmin` upstream to prevent query-cancelling vacuum |
| `max_standby_streaming_delay` | 30s | grace before a query is cancelled for a streamed-WAL conflict |
| `max_standby_archive_delay` | 30s | same, for conflicts during archive-WAL replay |
| `restore_command` | empty | shell command to fetch one archived WAL file |
| `recovery_target_timeline` | `latest` | which timeline to follow after an upstream promotion |

## Standby mode signal file

- `standby.signal` , an empty file in the data directory. Its presence puts the server into standby mode on start. Introduced in v12, replacing the old `recovery.conf`.
- Recovery / standby settings (`primary_conninfo`, `restore_command`, etc.) are normal GUCs in `postgresql.conf` / `postgresql.auto.conf`, not in a separate file.

## synchronous_commit values

| Value | The commit waits for |
|---|---|
| `off` | nothing , returns before the local WAL flush ; recent commits lost on a primary crash |
| `local` | the local WAL fsync only ; never waits for standbys, even if `synchronous_standby_names` is set |
| `on` (default) | the local WAL fsync ; AND, if `synchronous_standby_names` is set, the standby WAL fsync |
| `remote_write` | the standby to OS-write WAL (survives a standby process crash, not a standby OS crash) |
| `remote_apply` | the standby to REPLAY the commit (the commit is visible to standby queries on return) |

`remote_write`, `on`, and `remote_apply` only add a standby wait when `synchronous_standby_names` is non-empty.

## synchronous_standby_names syntax

| Form | Meaning |
|---|---|
| `FIRST n (s1, s2, s3)` | priority-based : wait for the first `n` available standbys in list order |
| `ANY n (s1, s2, s3)` | quorum-based : wait for any `n` of the listed standbys |
| `s1, s2, s3` | legacy bare list , equivalent to `FIRST 1 (s1, s2, s3)` |
| empty | replication is fully asynchronous |

Standby names match each standby's `application_name` (set in its `primary_conninfo`). Always list more standbys than `n`.

## pg_basebackup options

| Option | Purpose |
|---|---|
| `-D, --pgdata=DIR` | required , target data directory for the clone |
| `-h, --host=HOST` | primary host |
| `-U, --username=NAME` | role with the `REPLICATION` attribute |
| `-X, --wal-method=stream\|fetch\|none` | `stream` (default) collects WAL during the backup for an immediately consistent result |
| `-R, --write-recovery-conf` | create `standby.signal` and append `primary_conninfo` (+ `primary_slot_name` if `-S`) to `postgresql.auto.conf` |
| `-C, --create-slot` | create the replication slot before the backup (requires `-S`) |
| `-S, --slot=NAME` | use this replication slot with `-X stream` |
| `-c, --checkpoint=fast\|spread` | `fast` forces an immediate checkpoint so the backup starts sooner |
| `-P, --progress` | show progress |
| `-Fp / -Ft` | output format : plain directory (default) or tar |

With tar format, `-R` writes the standby settings inside `base.tar` rather than as loose files.

## Replication functions

| Function | Runs on | Purpose |
|---|---|---|
| `pg_create_physical_replication_slot(name)` | primary | create a physical slot |
| `pg_drop_replication_slot(name)` | primary | drop a slot (must be inactive) |
| `pg_promote(wait, wait_seconds)` | standby | end recovery, promote to read-write primary |
| `pg_is_in_recovery()` | any | `true` while a server is a standby |
| `pg_current_wal_lsn()` | primary | current WAL write position |
| `pg_last_wal_receive_lsn()` | standby | last WAL position received |
| `pg_last_wal_replay_lsn()` | standby | last WAL position replayed |
| `pg_last_xact_replay_timestamp()` | standby | commit timestamp of the last replayed transaction |
| `pg_wal_lsn_diff(a, b)` | any | byte distance between two LSNs |

## pg_stat_replication columns (on the primary)

| Column | Meaning |
|---|---|
| `application_name` | standby identity, matched by `synchronous_standby_names` |
| `state` | `streaming`, `catchup`, `startup`, `backup` |
| `sent_lsn` | WAL position sent to the standby |
| `write_lsn` | WAL position written by the standby OS |
| `flush_lsn` | WAL position fsynced on the standby |
| `replay_lsn` | WAL position replayed (visible to standby queries) |
| `write_lag` / `flush_lag` / `replay_lag` | time intervals for each stage |
| `sync_state` | `async`, `potential`, `sync`, `quorum` |

## pg_replication_slots columns (on the primary)

| Column | Meaning |
|---|---|
| `slot_name` | slot identifier |
| `slot_type` | `physical` or `logical` |
| `active` | whether a consumer is currently connected |
| `restart_lsn` | oldest WAL the slot still pins |
| `wal_status` | `reserved`, `extended`, `unreserved`, `lost` (invalidated) |
| `safe_wal_size` | bytes of WAL that can be written before the slot risks invalidation |

## Promotion (v15 / v16 / v17)

- `SELECT pg_promote();` or `pg_ctl promote` , the supported methods on all of v15, 16, 17.
- `promote_trigger_file` was removed in v16. Do not rely on it.

## Source URLs

- https://www.postgresql.org/docs/17/warm-standby.html
- https://www.postgresql.org/docs/17/hot-standby.html
- https://www.postgresql.org/docs/17/app-pgbasebackup.html
- https://www.postgresql.org/docs/17/runtime-config-replication.html
