---
name: postgres-core-version-matrix
description: >
  Use when targeting PostgreSQL 15, 16, or 17 and needing to know which feature is available where, or migrating across versions.
  Prevents writing queries that fail on older supported versions, missing v17 features that solve real problems, and confusion about which MERGE / RETURNING / JSON_TABLE behavior applies.
  Covers per-version feature delta (v15 MERGE/security_invoker/row filters/public lockdown, v16 logical-from-standby/COPY DEFAULT/role membership rewrite, v17 incremental backup/MERGE-RETURNING/JSON_TABLE/transaction_timeout/pg_createsubscriber).
  Keywords: PostgreSQL 15, PostgreSQL 16, PostgreSQL 17, MERGE, RETURNING, JSON_TABLE, incremental backup, pg_createsubscriber, transaction_timeout, COPY DEFAULT, security_invoker, logical replication from standby, which feature is in which version, can I use this on v15, how to detect version, what's new in postgres 17, syntax error on old version, feature unavailable, ERROR function does not exist, missing GUC
license: MIT
compatibility: "Designed for Claude Code. Requires PostgreSQL 15, 16, or 17."
metadata:
  author: OpenAEC-Foundation
  version: "1.0"
---

# postgres-core-version-matrix

## Quick Reference :

PostgreSQL 15, 16, 17 are all currently supported (v15 EOL 2027-11, v16 EOL 2028-11, v17 EOL 2029-11). Every feature, GUC, and syntax form in any other skill in this package MUST be annotated with the earliest supported version that exposes it. This skill is the authoritative cross-reference.

Three-step decision flow when authoring or reviewing PostgreSQL code :

1. Identify the target PostgreSQL major version (or minimum supported version if running fleet has mixed versions).
2. Look up each feature in this matrix : if present in all three (v15+v16+v17), no annotation needed. If introduced in v16, annotate `v16+`. If only in v17, annotate `v17+`.
3. If a feature only exists in v17 but the target is v15 or v16, find the fallback pattern listed in references/methods.md (e.g. `INSERT ... ON CONFLICT` instead of `MERGE ... RETURNING`).

Detection : `SHOW server_version_num;` returns a single integer like `170002` (v17.0.2). Use `current_setting('server_version_num')::int >= 170000` in conditional SQL. NEVER parse `version()` string output (format is unstable).

## When To Use This Skill :

ALWAYS use this skill when :

- A user asks "can I use feature X on PostgreSQL 15 / 16 / 17"
- Writing code that may run on multiple PostgreSQL versions
- Migrating an application from a previous PostgreSQL version (e.g. v15 to v17)
- Annotating any skill content in this package with version markers
- A v17 feature (MERGE RETURNING, JSON_TABLE, transaction_timeout, incremental backup, pg_createsubscriber) is being considered

NEVER use this skill for :

- Versions earlier than v15 (out of scope per D-015)
- Beta / development versions (v18+) until officially released
- Third-party extension version mapping (pgvector, PostGIS, pg_partman use their own version axes)

## Decision Trees :

### Which version to target :

```
Is target version explicit (e.g. AWS RDS PostgreSQL 16, Supabase = 15) ?
├── YES → use that version. Annotate any feature outside that version.
└── NO  → ask user, then default to v17 (newest GA, longest support window).

Is the fleet mixed (e.g. some prod on v15, some staging on v17) ?
├── YES → write to the LOWEST version. Add `v16+` / `v17+` guarded blocks for opt-in.
└── NO  → write to the target version.
```

### Choose MERGE variant :

```
Need to UPDATE/INSERT and return affected rows ?
├── v17+ → use MERGE ... RETURNING merge_action(), col1, col2 ...
├── v15+v16 → MERGE has no RETURNING. Use INSERT ... ON CONFLICT ... RETURNING instead.
└── target unclear → use INSERT ... ON CONFLICT ... RETURNING (works on all three).

Need to delete target rows missing from source (full-sync) ?
├── v17+ → MERGE ... WHEN NOT MATCHED BY SOURCE THEN DELETE
└── v15+v16 → manual DELETE WHERE NOT EXISTS in a separate statement.
```

### Choose JSON-to-rows method :

```
Need to project JSON array into rows-and-columns ?
├── v17+ → JSON_TABLE(jsonb_col, '$.items[*]' COLUMNS (...))
└── v15+v16 → jsonb_to_recordset(jsonb_col) AS x(id int, name text)
              or jsonb_array_elements(jsonb_col) + ->>
```

### Choose backup strategy :

```
Need page-level incremental backup ?
├── v17+ → set summarize_wal = on; pg_basebackup --incremental=<manifest>;
│           combine with pg_combinebackup before restore.
└── v15+v16 → file-system-level snapshot + WAL archive (PITR),
              or third-party (pgBackRest, Barman) which already do incrementals.
```

### Choose timeout GUC :

```
Need to limit one statement                → statement_timeout (v15+)
Need to limit lock acquisition             → lock_timeout (v15+)
Need to limit idle-in-transaction sessions → idle_in_transaction_session_timeout (v15+)
Need to limit any idle session             → idle_session_timeout (v15+)
Need to limit WHOLE transaction duration   → transaction_timeout (v17+ ONLY)
```

## Patterns :

### Pattern 1 : Detect server version at runtime

ALWAYS : compare `server_version_num` as integer.
NEVER : parse `version()` text.

```sql
-- WORKS on v15, v16, v17
SELECT current_setting('server_version_num')::int >= 160000 AS at_least_v16;

DO $$
BEGIN
  IF current_setting('server_version_num')::int >= 170000 THEN
    -- run v17-only DDL such as ALTER SYSTEM SET transaction_timeout
    PERFORM 1;
  END IF;
END$$;
```

WHY : `server_version_num` is a stable 6-digit integer (`MMmm00` for major M, minor m). `version()` returns a human-readable string whose format has changed between releases.

### Pattern 2 : Portable UPSERT (works on v15, v16, v17)

ALWAYS : use `INSERT ... ON CONFLICT ... DO UPDATE ... RETURNING` for portable UPSERT-with-RETURNING.
NEVER : use `MERGE ... RETURNING` if any target server is v15 or v16.

```sql
INSERT INTO accounts (id, balance)
VALUES (1, 100)
ON CONFLICT (id) DO UPDATE
  SET balance = accounts.balance + EXCLUDED.balance
RETURNING id, balance;
```

WHY : MERGE was introduced in v15 but RETURNING on MERGE only landed in v17. INSERT ... ON CONFLICT ... RETURNING has been stable since v9.5.

### Pattern 3 : Version-guarded transaction_timeout

ALWAYS : guard `transaction_timeout` with a version check.
NEVER : set `transaction_timeout` unconditionally in shared config that targets v15 or v16.

```sql
DO $$
BEGIN
  IF current_setting('server_version_num')::int >= 170000 THEN
    EXECUTE 'SET LOCAL transaction_timeout = ''30s''';
  ELSE
    -- v15+v16 fallback : combine statement_timeout + idle_in_transaction_session_timeout
    EXECUTE 'SET LOCAL statement_timeout = ''30s''';
    EXECUTE 'SET LOCAL idle_in_transaction_session_timeout = ''30s''';
  END IF;
END$$;
```

WHY : `transaction_timeout` (v17+) limits the wall-clock lifetime of the entire transaction, including idle-in-transaction time AND every statement. On v15+v16 only the separate statement-level and idle-in-tx-level GUCs exist; combining them approximates but does not equal `transaction_timeout`.

### Pattern 4 : security_invoker views (v15+)

ALWAYS : add `WITH (security_invoker = true)` when row-level security policies on underlying tables must apply to the view's caller, not its owner.
NEVER : use SECURITY INVOKER views on v14 or earlier (not supported).

```sql
-- v15+ : view executes RLS as caller, not view owner
CREATE VIEW active_orders WITH (security_invoker = true) AS
  SELECT * FROM orders WHERE archived_at IS NULL;
```

WHY : Before v15, views always executed with the owner's privileges, meaning RLS policies on base tables checked the view owner, not the querying user. v15+ makes this configurable per view.

### Pattern 5 : public schema lockdown (v15+, new databases only)

ALWAYS : when creating a database on v15+, expect the public schema to no longer grant CREATE to PUBLIC.
NEVER : assume any non-superuser role can create objects in `public` of a freshly-created v15+ database without an explicit GRANT.

```sql
-- v15+ new database : public has USAGE for PUBLIC but NOT CREATE
-- to allow app role to create objects there :
GRANT CREATE ON SCHEMA public TO app_user;

-- preferred pattern : give each app its own schema and revoke from public entirely
CREATE SCHEMA app AUTHORIZATION app_user;
```

WHY : CVE-2018-1058 mitigation. Pre-v15 default allowed any role to create objects in public, enabling search-path-based privilege escalation. v15+ default removes the CREATE grant for new databases. Pre-existing databases keep prior permissions on pg_upgrade.

### Pattern 6 : Logical replication from standby (v16+)

ALWAYS : on v16+, set up logical decoding directly on a hot standby to offload publisher load.
NEVER : attempt logical decoding on a v15 standby (returns `ERROR: logical decoding requires a non-hot-standby`).

```sql
-- v16+ : on the primary, ensure WAL records carry standby snapshot info
SELECT pg_log_standby_snapshot();

-- on the standby
SELECT pg_create_logical_replication_slot('slot_on_standby', 'pgoutput');
```

WHY : Logical decoding was previously only supported on primaries. v16 extended decoding state to be reachable via WAL, so a standby can run subscriptions and reduce primary load.

### Pattern 7 : COPY DEFAULT (v16+)

ALWAYS : on v16+, use `COPY ... WITH (DEFAULT '\\D')` to map a placeholder token in CSV to the column's default expression.
NEVER : use this on v15 (silently treated as literal text, results in invalid value).

```sql
-- v16+ : '\D' rows pick up column DEFAULTs
COPY orders (id, created_at, status) FROM STDIN WITH (FORMAT csv, DEFAULT '\D');
1,2025-01-01,paid
2,\D,paid
3,2025-01-03,\D
\.
```

WHY : Before v16, COPY required every row to supply explicit values (or NULL). Importing externally produced CSV that wanted "use the DB default" required pre-processing. v16's COPY DEFAULT removes that step.

### Pattern 8 : Incremental backup (v17+)

ALWAYS : on v17+, enable `summarize_wal` BEFORE taking the baseline, otherwise incrementals cannot be assembled.
NEVER : restore incremental backups directly; ALWAYS combine with `pg_combinebackup` first.

```bash
# v17+ ONLY. On primary, in postgresql.conf :
# summarize_wal = on
# wal_summary_keep_time = '7d'

# Baseline full backup
pg_basebackup -D /backup/full -Fp -X stream -c fast

# Incremental against the previous manifest
pg_basebackup --incremental=/backup/full/backup_manifest -D /backup/incr1 -Fp

# Combine before restore (offline)
pg_combinebackup -o /restore /backup/full /backup/incr1
```

WHY : v17 native incremental backup tracks changed blocks via WAL summary files. Each `pg_basebackup --incremental` only copies blocks modified since the previous manifest. Without `pg_combinebackup` the incremental directory is not a valid data directory.

### Pattern 9 : pg_createsubscriber (v17+)

ALWAYS : prefer `pg_createsubscriber` over manual logical-slot + initial-copy when converting a physical standby into a logical subscriber on v17+.
NEVER : run `pg_createsubscriber` against a primary; it operates on a standby.

```bash
# v17+ ONLY. Cleanly converts an existing physical standby into a logical subscriber.
pg_createsubscriber \
  -D /var/lib/postgresql/17/standby \
  --publisher-server='host=primary port=5432 dbname=postgres user=replicator' \
  --subscriber-server='host=standby port=5432 dbname=postgres user=postgres' \
  --database=app
```

WHY : Pre-v17, converting a physical standby into a logical subscriber required a manual sequence (promote, dump+restore-or-snapshot, create subscription, manage initial-sync). pg_createsubscriber automates this in one command.

## Anti-Patterns :

(Full content in references/anti-patterns.md)

- MERGE ... RETURNING on v15 or v16 : SQLSTATE 42601 syntax error. See references/anti-patterns.md.
- JSON_TABLE on v15 or v16 : SQLSTATE 42883 function does not exist. See references/anti-patterns.md.
- `SET transaction_timeout` on v15 or v16 : SQLSTATE 22023 unrecognized configuration parameter. See references/anti-patterns.md.
- Parsing `version()` text instead of `server_version_num` : breaks silently on minor release upgrades. See references/anti-patterns.md.
- Assuming `public` schema is writable on v15+ new DBs : SQLSTATE 42501 insufficient privilege on first CREATE TABLE. See references/anti-patterns.md.
- Restoring a v17 incremental backup directory directly without `pg_combinebackup` : incomplete data directory, server fails to start.
- Logical decoding on a v15 standby : `ERROR: logical decoding requires a non-hot-standby` (functionality only added in v16).
- `pg_dump --filter=` on v15 : SQLSTATE not applicable, but pg_dump exits with `unrecognized option --filter`. v16+ only.

## Reference Links :

- [references/methods.md](references/methods.md) : Full per-version feature table, GUC introductions per version, fallback patterns for each v16+/v17+ feature, and version-detection helpers.
- [references/examples.md](references/examples.md) : Working code samples per pattern, all version-annotated, runnable against the corresponding PostgreSQL major version.
- [references/anti-patterns.md](references/anti-patterns.md) : Anti-patterns with cause, symptom, SQLSTATE where applicable, and version-specific fixes.

## See Also :

- postgres-core-architecture : the MVCC / WAL invariants are stable across v15-v17. Version differences live in this skill.
- postgres-syntax-merge-upsert : MERGE specifics; this skill answers "is MERGE-RETURNING available".
- postgres-syntax-json-jsonb : JSON_TABLE and JSON constructors; this skill answers "is JSON_TABLE available".
- postgres-core-replication-modes : logical-from-standby and pg_createsubscriber are referenced here for version targeting.
- postgres-impl-backup-restore : incremental backup mechanics; this skill answers "is incremental backup available".
- Vooronderzoek section : §20 (Version Matrix table), §2 (SQL surface deltas), §3 (JSON deltas), §16 (zero-downtime DDL deltas).
- Official docs : https://www.postgresql.org/docs/release/17.0/ , https://www.postgresql.org/docs/release/16.0/ , https://www.postgresql.org/docs/release/15.0/
