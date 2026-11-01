# postgres-core-security-model : Anti-Patterns

Each entry : cause -> symptom (with SQLSTATE where applicable) -> fix pattern.

## AP-1 : Application logs in as SUPERUSER

**Cause** : Convenience during onboarding ; `DATABASE_URL` uses `postgres` or any role with `rolsuper = t`.

**Symptom** : RLS policies silently bypassed. pg_hba lines with `nosuperuser` filter never apply. Audit extensions miss the actor. `pg_signal_backend` granted to other roles cannot terminate the misbehaving session because superuser backends are exempt.

**Fix** :
```sql
CREATE ROLE app_user LOGIN PASSWORD '...' CONNECTION LIMIT n;
GRANT CONNECT ON DATABASE app_prod TO app_user;
GRANT USAGE ON SCHEMA app TO app_user;
ALTER DEFAULT PRIVILEGES FOR ROLE app_owner IN SCHEMA app
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO app_user;
-- update DATABASE_URL ; rotate the postgres password.
```

## AP-2 : Treating CREATE USER and CREATE ROLE as different concepts

**Cause** : Carry-over from MySQL / SQL Server mental model.

**Symptom** : `CREATE USER alice` (LOGIN implied) and `CREATE ROLE bi_group` (NOLOGIN implied) are added inconsistently. Some "users" are actually NOLOGIN ; some "groups" can log in.

**Fix** : pick one. ALWAYS use `CREATE ROLE name LOGIN ...` for principals that authenticate ; `CREATE ROLE name` (defaults to NOLOGIN) for groups.

## AP-3 : `GRANT SELECT ON ALL TABLES IN SCHEMA s` and expecting it to cover future tables

**Cause** : The wording suggests a forward-looking grant.

**Symptom** : New tables created later are not visible to the grantee. SELECT fails with `42501` (`ERROR: permission denied for table X`).

**Fix** : combine the immediate grant with `ALTER DEFAULT PRIVILEGES` :
```sql
GRANT SELECT ON ALL TABLES IN SCHEMA app TO bi_reader;            -- existing
ALTER DEFAULT PRIVILEGES FOR ROLE app_owner IN SCHEMA app
  GRANT SELECT ON TABLES TO bi_reader;                            -- future
```
NEVER omit `FOR ROLE` if more than one role creates objects in the schema ; `ALTER DEFAULT PRIVILEGES` is keyed by the creator.

## AP-4 : `pg_read_all_data` does not bypass RLS

**Cause** : Documentation skim implies "read everything".

**Symptom** : DBA grants `pg_read_all_data` to triage role and still sees zero rows on RLS-enabled tables.

**Fix** :
```sql
-- v14+ : grant predefined role AND BYPASSRLS attribute
GRANT pg_read_all_data TO oncall;
ALTER ROLE oncall BYPASSRLS;
```

## AP-5 : Public schema writable to PUBLIC after upgrade

**Cause** : Database originated on pre-v15 PostgreSQL and was upgraded via `pg_upgrade` ; the original ACL `=UC/postgres` survives.

**Symptom** : Any logged-in role can `CREATE TABLE public.users` and shadow the real `app.users` via `search_path = "$user", public`.

**Fix** :
```sql
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
REVOKE ALL    ON SCHEMA public FROM PUBLIC;
GRANT  USAGE  ON SCHEMA public TO PUBLIC;
```

## AP-6 : Membership granted without `WITH SET TRUE` ; SET ROLE fails

**Cause** : v16+ explicit options ; the operator passed `WITH INHERIT FALSE` and forgot `SET TRUE`.

**Symptom** : `SET ROLE app_owner` returns SQLSTATE `42501` : `ERROR: permission denied to set role "app_owner"`.

**Fix** :
```sql
GRANT app_owner TO alice WITH SET TRUE INHERIT FALSE;
```
ALWAYS specify both `INHERIT` and `SET` flags when granting to a role that will switch identity.

## AP-7 : Owning tables as the `postgres` superuser

**Cause** : Migration tooling connects as `postgres` and does not `SET ROLE` to the application owner before DDL.

**Symptom** : Restore on a fresh machine fails because the target has no `postgres` role with the matching OID, or `pg_dump --no-owner` is required for every restore.

**Fix** :
```sql
-- Migration tool runs :
SET ROLE app_owner;
CREATE TABLE ...;
RESET ROLE;
-- Existing wrong-owner objects :
REASSIGN OWNED BY postgres TO app_owner;
```

## AP-8 : Column-level GRANT after table-level REVOKE

**Cause** : Intent : "let analytics see email but not salary." Operator runs `REVOKE SELECT ON employees FROM analytics; GRANT SELECT(email) ON employees TO analytics;`.

**Symptom** : `SELECT email FROM employees` returns SQLSTATE `42501` `permission denied for table employees`.

**Fix** : column grants are independent of table grants ; both are checked. ALWAYS grant column privileges directly without revoking the table grant if you only want to scope columns. To deny all-columns access while permitting some columns, the correct pattern is :
```sql
REVOKE ALL ON employees FROM analytics;          -- remove broad grant
GRANT SELECT (id, email, hired_at) ON employees TO analytics;  -- scoped grant
GRANT USAGE ON SCHEMA public TO analytics;       -- still needed
```
Schema USAGE is independently required ; without it the column grant is unreachable.

## AP-9 : `BYPASSRLS` on the application login

**Cause** : RLS tested as superuser, "made to work" by adding BYPASSRLS to the app role.

**Symptom** : Every query bypasses every policy ; multitenant isolation is silently disabled in production while passing all integration tests that connect as the app user.

**Fix** : revoke `BYPASSRLS` on the app role. Scope `BYPASSRLS` to maintenance roles only :
```sql
ALTER ROLE app_user NOBYPASSRLS;
ALTER ROLE app_maintenance BYPASSRLS;
```

## AP-10 : Revoking per-schema defaults to undo a global default

**Cause** : Operator believed `ALTER DEFAULT PRIVILEGES IN SCHEMA s REVOKE ...` could remove a previously global `ALTER DEFAULT PRIVILEGES REVOKE ...`.

**Symptom** : The per-schema REVOKE appears to "do nothing" : PUBLIC still receives EXECUTE on new functions.

**Fix** : per-schema defaults can only ADD to global defaults. Revoke at the global level :
```sql
ALTER DEFAULT PRIVILEGES FOR ROLE app_owner
  REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC;
```
Source : postgresql.org/docs/17/sql-alterdefaultprivileges.html.

## AP-11 : Dropping a role that still owns objects

**Cause** : `DROP ROLE app_owner` without cleanup.

**Symptom** : `ERROR: role "app_owner" cannot be dropped because some objects depend on it` (SQLSTATE `2BP01`).

**Fix** : run `REASSIGN OWNED BY` + `DROP OWNED BY` in every database where the role had objects, then `DROP ROLE`. See examples §9.

## AP-12 : `pg_read_server_files` treated as a "monitoring" capability

**Cause** : Operator confuses it with `pg_read_all_data`.

**Symptom** : Recipient can `COPY ... FROM '/etc/passwd'` or call `pg_read_file('/etc/shadow')` , equivalent to filesystem access as the postgres OS user.

**Fix** : never grant `pg_read_server_files`, `pg_write_server_files`, or `pg_execute_server_program` to anything that is not effectively the DB superuser. For application reads use `pg_read_all_data`.

## AP-13 : `password_encryption = md5` left on a v15+ cluster

**Cause** : Carry-over from old `postgresql.conf` templates.

**Symptom** : New passwords stored as MD5 hashes ; pg_hba `scram-sha-256` lines force a fallback to `md5` for those users and cannot be migrated cleanly to SSL-required + SCRAM channels.

**Fix** :
```sql
ALTER SYSTEM SET password_encryption = 'scram-sha-256';
SELECT pg_reload_conf();
-- Then reset every user password so it is re-hashed :
ALTER ROLE app_user PASSWORD '...';
```
This entry is here only for cross-reference ; deep-dive at `postgres-impl-configuration-tuning` and `postgres-errors-connection-auth`.

## AP-14 : Granting CREATEROLE to delegate user-management on v16+

**Cause** : Pre-v16 `CREATEROLE` was effectively "manage every non-superuser role." v16 changed semantics : a CREATEROLE role can only grant attributes on a target role if it also holds `WITH ADMIN OPTION` on that target.

**Symptom** : Delegated admin gets `ERROR: must have admin option on role "X"` (`42501`) when running `GRANT pg_monitor TO Y`.

**Fix** : grant ADMIN OPTION on every role the delegated admin needs to manage :
```sql
GRANT pg_monitor       TO useradmin WITH ADMIN OPTION;
GRANT app_reader       TO useradmin WITH ADMIN OPTION;
```
Or grant CREATEROLE with explicit scope using membership trees.

## SQLSTATE Reference

| SQLSTATE | Meaning | Typical security-model trigger |
|---|---|---|
| `42501` | `insufficient_privilege` | Missing object grant, missing schema USAGE, missing column grant, missing SET option on membership, missing ADMIN OPTION. |
| `2BP01` | `dependent_objects_still_exist` | `DROP ROLE` while role still owns objects. |
| `42P01` | `undefined_table` | Sometimes surfaces when search_path masks a table because the visible one is in a schema the role lacks USAGE on. |
| `28P01` | `invalid_password` | Wrong password or expired password (VALID UNTIL passed). Authentication-layer ; deep-dive at `postgres-errors-connection-auth`. |
| `28000` | `invalid_authorization_specification` | pg_hba rejected the connection ; or `SET SESSION AUTHORIZATION` by non-superuser. |

Source : postgresql.org/docs/17/errcodes-appendix.html.
