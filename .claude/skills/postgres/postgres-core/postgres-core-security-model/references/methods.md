# postgres-core-security-model : Methods Reference

## CREATE ROLE : Full Syntax

```sql
CREATE ROLE name [ [ WITH ] option [ ... ] ]
```

`CREATE USER name ...` is identical to `CREATE ROLE name ...` except `LOGIN` is the default. There is no semantic distinction between "user" and "role" beyond this default.

### Role-attribute options

| Option | Default | Notes |
|---|---|---|
| `SUPERUSER` / `NOSUPERUSER` | NOSUPERUSER | Bypasses every privilege check (including RLS, `nosuperuser` pg_hba lines, BYPASSRLS, etc.). ALWAYS scope tightly. |
| `CREATEDB` / `NOCREATEDB` | NOCREATEDB | Allow `CREATE DATABASE`. |
| `CREATEROLE` / `NOCREATEROLE` | NOCREATEROLE | v16+ : CREATEROLE may only grant attributes on a target role if it also holds ADMIN OPTION on that role. |
| `INHERIT` / `NOINHERIT` | INHERIT | v15 : role-level inheritance attribute. v16+ : default for *new* memberships ; per-membership `INHERIT { TRUE | FALSE }` overrides. |
| `LOGIN` / `NOLOGIN` | NOLOGIN (CREATE ROLE) / LOGIN (CREATE USER) | Only LOGIN roles can authenticate via pg_hba. |
| `REPLICATION` / `NOREPLICATION` | NOREPLICATION | Required for physical and logical replication clients. |
| `BYPASSRLS` / `NOBYPASSRLS` | NOBYPASSRLS | Skips every Row-Level Security policy. Separate from SUPERUSER. |
| `CONNECTION LIMIT connlimit` | -1 (unlimited) | Per-role concurrent-connection limit. `-1` = no limit. |
| `[ ENCRYPTED ] PASSWORD 'password'` | NULL | Stored as scram-sha-256 when `password_encryption = 'scram-sha-256'` (default since v14). `PASSWORD NULL` removes the password. |
| `VALID UNTIL 'timestamp'` | infinity | Password-expiry timestamp. Does NOT disable the role, only the password. |
| `IN ROLE role_name [, ...]` | (none) | New role becomes member of `role_name`. Equivalent to `GRANT role_name TO new_role` with `SET TRUE, ADMIN FALSE`. |
| `ROLE role_name [, ...]` | (none) | New role is added as a parent to `role_name`. |
| `ADMIN role_name [, ...]` | (none) | Same as ROLE but the new role joins with `ADMIN OPTION`. |

Source : postgresql.org/docs/17/sql-createrole.html

## ALTER ROLE / DROP ROLE

```sql
ALTER ROLE name [ [ WITH ] option [ ... ] ]
ALTER ROLE name RENAME TO new_name
ALTER ROLE name [ IN DATABASE database_name ] SET parameter { TO | = } { value | DEFAULT }
ALTER ROLE name [ IN DATABASE database_name ] RESET parameter
DROP ROLE [ IF EXISTS ] name [, ...]
```

`DROP ROLE` fails if the role still owns any object (`ERROR: role "X" cannot be dropped because some objects depend on it`). Use `REASSIGN OWNED BY x TO y` and `DROP OWNED BY x` first :

```sql
REASSIGN OWNED BY app_old TO app_new;   -- reassigns ownership of objects in current DB
DROP OWNED BY app_old;                  -- drops privileges, default privs, policies
DROP ROLE app_old;
```

Source : postgresql.org/docs/17/sql-alterrole.html, postgresql.org/docs/17/sql-reassignowned.html, postgresql.org/docs/17/sql-dropowned.html.

## GRANT / REVOKE Privilege Matrix

```sql
GRANT privilege [, ...] ON object_spec TO role_spec [WITH GRANT OPTION] [GRANTED BY grantor]
REVOKE [GRANT OPTION FOR] privilege [, ...] ON object_spec FROM role_spec [GRANTED BY grantor] [CASCADE | RESTRICT]
```

| Object | Privileges |
|---|---|
| TABLE | SELECT, INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER, **MAINTAIN (v17+)** |
| SEQUENCE | USAGE, SELECT, UPDATE |
| DATABASE | CREATE, CONNECT, TEMPORARY (TEMP) |
| SCHEMA | CREATE, USAGE |
| FUNCTION / PROCEDURE / ROUTINE | EXECUTE |
| LANGUAGE | USAGE |
| TYPE / DOMAIN | USAGE |
| FOREIGN DATA WRAPPER | USAGE |
| FOREIGN SERVER | USAGE |
| TABLESPACE | CREATE |
| LARGE OBJECT | SELECT, UPDATE |
| PARAMETER | SET, ALTER SYSTEM |

Column-level grants : `GRANT SELECT (col1, col2) ON t TO r`. Column-level privileges do NOT override a table-level REVOKE.

`GRANT ALL PRIVILEGES` expands to every privilege on the object class.

`GRANT ... ON ALL TABLES IN SCHEMA s TO r` applies only to tables that exist at execution time. For future objects use `ALTER DEFAULT PRIVILEGES`.

Source : postgresql.org/docs/17/sql-grant.html, postgresql.org/docs/17/sql-revoke.html.

## GRANT Role Membership (v16+ option semantics)

```sql
GRANT role_name [, ...] TO role_spec [, ...] [WITH { ADMIN | INHERIT | SET } { TRUE | FALSE | OPTION } [, ...]]
                                              [GRANTED BY grantor]
```

| Option | Default | Effect |
|---|---|---|
| `ADMIN { TRUE \| OPTION }` | FALSE | Member can GRANT the role onward and ALTER attributes the grantor has admin on. |
| `INHERIT { TRUE \| FALSE }` | follows member's `INHERIT` attribute | Privileges of `role_name` are automatically usable by member ; FALSE means privileges only apply after `SET ROLE`. |
| `SET { TRUE \| FALSE }` | TRUE | Member may `SET ROLE role_name` to switch active identity. |

Pre-v16 : a member auto-administered roles they created. v16 removed that and requires explicit `WITH ADMIN OPTION`. Pre-v16 `INHERIT` was a role attribute ; v16+ it is per-membership.

Source : postgresql.org/docs/17/sql-grant.html.

## ALTER DEFAULT PRIVILEGES

```sql
ALTER DEFAULT PRIVILEGES
  [ FOR { ROLE | USER } target_role [, ...] ]
  [ IN SCHEMA schema_name [, ...] ]
  abbreviated_grant_or_revoke

abbreviated_grant ::=
  GRANT privilege [, ...] ON
    { TABLES | SEQUENCES | FUNCTIONS | ROUTINES | TYPES | SCHEMAS }
    TO role_spec [WITH GRANT OPTION]
```

Default object-type matrix :

| Default-priv object type | Privileges |
|---|---|
| TABLES (incl. views and foreign tables) | SELECT, INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER, MAINTAIN (v17+) |
| SEQUENCES | USAGE, SELECT, UPDATE |
| FUNCTIONS / ROUTINES | EXECUTE |
| TYPES (incl. domains) | USAGE |
| SCHEMAS | USAGE, CREATE |

Behavior :
- Applies to objects **created after** the statement executes ; existing objects are not affected.
- `FOR ROLE x` : default privileges that take effect when role `x` creates the object. If omitted, the current role is used.
- `IN SCHEMA s` : restricts the rule to objects created inside `s`. Without `IN SCHEMA` the rule applies to objects created in any schema.
- Per-schema defaults **add to** global defaults ; they cannot REVOKE a globally-granted default.

Inspect : `\ddp` in psql, system catalog `pg_default_acl`.

Source : postgresql.org/docs/17/sql-alterdefaultprivileges.html.

## Predefined Roles

| Role | Allows | Added |
|---|---|---|
| `pg_read_all_data` | SELECT + USAGE on all tables, views, sequences, schemas. Does NOT set BYPASSRLS. | v14 |
| `pg_write_all_data` | INSERT, UPDATE, DELETE on all tables. Does NOT set BYPASSRLS. | v14 |
| `pg_read_all_settings` | Read all GUCs including superuser-only ones. | v10 |
| `pg_read_all_stats` | Read all `pg_stat_*` views and stat-extension data. | v10 |
| `pg_stat_scan_tables` | Execute monitoring functions that may take ACCESS SHARE locks. | v10 |
| `pg_monitor` | Member of `pg_read_all_settings`, `pg_read_all_stats`, `pg_stat_scan_tables`. | v10 |
| `pg_database_owner` | Implicit role : current database owner. Initially owns schema `public` (v15+). | v14 |
| `pg_signal_backend` | `pg_cancel_backend()` and `pg_terminate_backend()` on non-superuser backends. | v9.6 |
| `pg_read_server_files` | Read server-side files via COPY, `pg_read_file`, etc. Effectively superuser-equivalent. | v11 |
| `pg_write_server_files` | Write server-side files via COPY, `pg_write_file`. Effectively superuser-equivalent. | v11 |
| `pg_execute_server_program` | Execute COPY ... FROM PROGRAM. Effectively superuser-equivalent. | v11 |
| `pg_checkpoint` | `CHECKPOINT` command. | v15 |
| `pg_use_reserved_connections` | Use slots from `reserved_connections` GUC (separate from superuser slots). | v16 |
| `pg_create_subscription` | `CREATE SUBSCRIPTION` if also holds CREATE on the database. | v16 |
| `pg_maintain` | VACUUM, ANALYZE, CLUSTER, REFRESH MATERIALIZED VIEW, REINDEX, LOCK TABLE on all relations. | v17 |

Predefined roles cannot be dropped. Membership is granted via plain `GRANT pg_role_name TO target`. `pg_read_server_files`, `pg_write_server_files`, `pg_execute_server_program` are documented as effectively-superuser ; treat them like SUPERUSER for risk classification.

Source : postgresql.org/docs/17/predefined-roles.html.

## SET ROLE vs SET SESSION AUTHORIZATION

```sql
SET [ SESSION | LOCAL ] ROLE rolename
SET [ SESSION | LOCAL ] ROLE NONE
RESET ROLE

SET [ SESSION | LOCAL ] SESSION AUTHORIZATION rolename
SET [ SESSION | LOCAL ] SESSION AUTHORIZATION DEFAULT
RESET SESSION AUTHORIZATION
```

| Command | What it switches | Who may use it |
|---|---|---|
| `SET ROLE x` | Current role (`current_user`) only. `session_user` stays the original login. | Any role with `SET` option on membership in `x`, or `WITH SET TRUE`. |
| `SET SESSION AUTHORIZATION x` | Both `session_user` and `current_user`. Bypasses role-membership checks. | Superuser only. |

ALWAYS use `SET ROLE` in application code that needs to drop down to a less-privileged role. `SET SESSION AUTHORIZATION` is reserved for superuser-driven impersonation.

Source : postgresql.org/docs/17/sql-set-role.html, postgresql.org/docs/17/sql-set-session-authorization.html.

## System-Catalog Cheat Sheet

| Question | Query |
|---|---|
| List all roles + attributes | `\du+` in psql, or `SELECT * FROM pg_roles` |
| Direct membership graph | `SELECT roleid::regrole, member::regrole, admin_option, inherit_option, set_option FROM pg_auth_members` |
| Object-level grants on a table | `SELECT * FROM information_schema.role_table_grants WHERE table_name = 't'` |
| Column-level grants | `SELECT * FROM information_schema.role_column_grants` |
| Default-priv assignments | `SELECT * FROM pg_default_acl` (or `\ddp`) |
| Predefined-role members | `SELECT rolname FROM pg_roles r JOIN pg_auth_members m ON m.roleid = r.oid WHERE rolname LIKE 'pg\_%'` |
| Currently effective identity | `SELECT current_user, session_user` |

## pg_hba.conf (Overview)

Authentication is decided BEFORE any GRANT check runs. pg_hba.conf entries are evaluated top-down, first-match-wins. Record format :

```
TYPE  DATABASE  USER  ADDRESS  METHOD  [OPTIONS]
```

| Field | Values |
|---|---|
| TYPE | `local`, `host`, `hostssl`, `hostnossl`, `hostgssenc`, `hostnogssenc` |
| METHOD | `trust`, `reject`, `scram-sha-256`, `md5`, `password`, `peer`, `ident`, `cert`, `gss`, `sspi`, `ldap`, `radius`, `pam`, `bsd` |

v16+ supports regex in DATABASE/USER (prefix `/`) and `include` / `include_if_exists` / `include_dir` directives. Inspect parsed rules : `SELECT * FROM pg_hba_file_rules`. Reload after edits : `SELECT pg_reload_conf()`. Deep-dive : `postgres-errors-connection-auth`.

Source : postgresql.org/docs/17/auth-pg-hba-conf.html.
