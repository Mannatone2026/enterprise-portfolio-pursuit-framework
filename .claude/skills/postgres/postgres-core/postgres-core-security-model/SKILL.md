---
name: postgres-core-security-model
description: >
  Use when designing PostgreSQL permission models, picking which role attributes to grant, or auditing existing privileges before a security review.
  Prevents over-granting SUPERUSER, leaving public schema writable, forgetting ALTER DEFAULT PRIVILEGES for future tables, and confusion between role attributes and predefined roles.
  Covers role attributes (LOGIN, SUPERUSER, REPLICATION, BYPASSRLS, CREATEDB, CREATEROLE, INHERIT), GRANT/REVOKE matrix, ALTER DEFAULT PRIVILEGES, predefined roles (pg_read_all_data, pg_write_all_data, pg_monitor, pg_use_reserved_connections v16+, pg_maintain v17+), role membership, SET ROLE.
  Keywords: GRANT, REVOKE, role, predefined role, BYPASSRLS, SUPERUSER, ALTER DEFAULT PRIVILEGES, pg_read_all_data, pg_maintain, permission denied, who can see this table, how to set up read-only user, where do default privileges come from
license: MIT
compatibility: "Designed for Claude Code. Requires PostgreSQL 15, 16, or 17."
metadata:
  author: OpenAEC-Foundation
  version: "1.0"
---

# postgres-core-security-model

## Quick Reference :

PostgreSQL has a single identity primitive : the **role**. There is no separate "user" or "group" object. `CREATE USER` is identical to `CREATE ROLE ... LOGIN`. Every role carries attributes (`LOGIN`, `SUPERUSER`, `CREATEDB`, `CREATEROLE`, `INHERIT`, `REPLICATION`, `BYPASSRLS`, `CONNECTION LIMIT`, `PASSWORD`, `VALID UNTIL`) and can be a **member** of other roles (forming a directed graph). Privileges on database objects are granted via `GRANT` per object class : TABLE, SEQUENCE, DATABASE, SCHEMA, FUNCTION, TYPE, LANGUAGE, DOMAIN, FOREIGN SERVER, TABLESPACE, LARGE OBJECT, PARAMETER. Two grant systems coexist : (1) **role membership** (`GRANT app_reader TO alice`) controls which roles a session can act as via `SET ROLE` and which privileges are inherited ; (2) **object privileges** (`GRANT SELECT ON TABLE orders TO app_reader`) controls what a role can do to an object. `ALTER DEFAULT PRIVILEGES` is a separate command that pre-registers privileges on objects that **do not exist yet** ; it does NOT retroactively grant on existing objects.

PostgreSQL ships fifteen **predefined roles** (`pg_read_all_data`, `pg_write_all_data`, `pg_read_all_settings`, `pg_read_all_stats`, `pg_stat_scan_tables`, `pg_monitor`, `pg_database_owner`, `pg_signal_backend`, `pg_read_server_files`, `pg_write_server_files`, `pg_execute_server_program`, `pg_checkpoint`, `pg_maintain` (v17+), `pg_use_reserved_connections` (v16+), `pg_create_subscription` (v16+)) which let you delegate operational privilege without granting SUPERUSER. v16 rewrote role-membership semantics : `GRANT role TO member` now takes explicit `WITH ADMIN | INHERIT | SET { TRUE | FALSE }` options and members no longer auto-administer themselves. RLS (Row-Level Security) sits on top of object privileges and is covered in `postgres-core-rls-policies` ; pg_hba.conf controls who can connect at all and is covered in `postgres-errors-connection-auth`.

## When To Use This Skill :

ALWAYS use this skill when :
- Provisioning a new application user, read-only reporting user, or migration role
- Auditing "who can see / write / execute X" across a database
- Reviewing whether a role should be `SUPERUSER`, `CREATEROLE`, or use a predefined role instead
- Adding a new schema and deciding default privileges for objects created in it
- Encountering `ERROR: permission denied for table X` / `ERROR: permission denied for schema Y`
- Designing GRANT chains across application tiers (owner, app, reader, analyst)

NEVER use this skill for :
- Row-level filtering policies : see `postgres-core-rls-policies`
- pg_hba.conf entries, SCRAM-SHA-256, peer auth, SSL : see `postgres-errors-connection-auth`
- Encryption at rest, TDE, pgcrypto column-level encryption : not in scope of this package
- Setting `password_encryption`, `ssl`, `ssl_cert_file` : see `postgres-impl-configuration-tuning`

## Decision Trees :

### Pick the right role attribute set for a new principal :

```
Is this principal a human or a service?
├── Human DBA / on-call operator :
│   ├── Needs to read everything for triage : GRANT pg_read_all_data + pg_monitor
│   ├── Needs to run VACUUM/ANALYZE/REINDEX : also GRANT pg_maintain (v17+)
│   └── Needs to terminate runaway sessions : also GRANT pg_signal_backend
│       NEVER : SUPERUSER for routine DBA work
└── Service (application backend, ETL, replica):
    ├── Application backend : LOGIN, NOSUPERUSER, NOCREATEDB, NOCREATEROLE,
    │                          NOREPLICATION, INHERIT, CONNECTION LIMIT n,
    │                          PASSWORD '...', VALID UNTIL '...'
    ├── Logical/physical replication client : LOGIN, REPLICATION, all other NO*
    └── Migration tool (Liquibase/Flyway/Alembic) :
        LOGIN, owner of the target schema, NOSUPERUSER.
        If schema does not yet exist : grant CREATEDB once, revoke afterwards.
```

### Should this be a role attribute or a predefined role? :

```
Capability needed?
├── Read every table everywhere : predefined role pg_read_all_data (NOT SUPERUSER)
├── Write every table everywhere : predefined role pg_write_all_data (NOT SUPERUSER)
├── Read pg_stat_* and pg_settings : predefined role pg_monitor
├── VACUUM/ANALYZE/CLUSTER/REINDEX everywhere (v17+) : pg_maintain
├── Terminate other backends : pg_signal_backend
├── Manual CHECKPOINT : pg_checkpoint
├── Use the reserved_connections slots (v16+) : pg_use_reserved_connections
├── CREATE SUBSCRIPTION as non-superuser (v16+) : pg_create_subscription
├── Create databases : attribute CREATEDB
├── Create other roles : attribute CREATEROLE
├── Run replication protocol : attribute REPLICATION
├── Bypass every RLS policy : attribute BYPASSRLS
└── All of the above + everything else : SUPERUSER (default-deny posture broken)
```

### Where do this user's privileges come from? :

```
SELECT current_user; --> alice
1. Direct object grants : SELECT * FROM information_schema.role_table_grants
                          WHERE grantee = 'alice';
2. Inherited from member roles : SELECT * FROM pg_auth_members
                                  WHERE member = 'alice'::regrole;
   (only contributes privileges when INHERIT is true on the grant)
3. PUBLIC grants : SELECT * FROM information_schema.role_table_grants
                    WHERE grantee = 'PUBLIC';
4. Object ownership : SELECT tableowner FROM pg_tables WHERE tableowner = 'alice';
5. Predefined role membership : SELECT rolname FROM pg_roles r
                                JOIN pg_auth_members m ON m.roleid = r.oid
                                WHERE m.member = 'alice'::regrole
                                  AND rolname LIKE 'pg\_%';
```

## Patterns :

### Pattern 1 : Provision a least-privilege application user

ALWAYS create a separate role per application tier (owner, app, reader).
NEVER use the `postgres` superuser as the application login.

```sql
-- 1. Owner role : owns the schema and tables, does not LOGIN
CREATE ROLE app_owner NOLOGIN;

-- 2. Application login role : reads + writes data, never DDL
CREATE ROLE app_user LOGIN
  PASSWORD 'use a real secret'
  CONNECTION LIMIT 50
  VALID UNTIL '2027-01-01';

-- 3. Read-only role : reports, BI tools, replicas
CREATE ROLE app_reader LOGIN PASSWORD 'reader secret' CONNECTION LIMIT 20;

-- 4. Schema and ownership
CREATE SCHEMA app AUTHORIZATION app_owner;

-- 5. Default privileges for FUTURE objects created BY app_owner :
ALTER DEFAULT PRIVILEGES FOR ROLE app_owner IN SCHEMA app
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO app_user;
ALTER DEFAULT PRIVILEGES FOR ROLE app_owner IN SCHEMA app
  GRANT USAGE, SELECT ON SEQUENCES TO app_user;
ALTER DEFAULT PRIVILEGES FOR ROLE app_owner IN SCHEMA app
  GRANT SELECT ON TABLES TO app_reader;
ALTER DEFAULT PRIVILEGES FOR ROLE app_owner IN SCHEMA app
  GRANT SELECT ON SEQUENCES TO app_reader;

-- 6. USAGE on the schema itself (NOT a default-privilege thing)
GRANT USAGE ON SCHEMA app TO app_user, app_reader;
```

WHY : the **owner / app / reader** triad isolates change authority (only `app_owner` creates tables) from runtime authority (only `app_user` mutates data). `ALTER DEFAULT PRIVILEGES` applies **only to objects created after** the statement runs ; tables that already exist need an explicit `GRANT ... ON ALL TABLES IN SCHEMA app TO ...`. Source : postgresql.org/docs/17/sql-alterdefaultprivileges.html, postgresql.org/docs/17/sql-grant.html.

### Pattern 2 : Revoke the public schema write privilege (v15+ secure default)

ALWAYS revoke `CREATE` on `public` from the `PUBLIC` role on any database that holds production data.
NEVER assume the v15+ default `pg_database_owner` ownership of `public` is enough on its own ; legacy databases upgraded from earlier versions still have `PUBLIC` writable.

```sql
-- Inspect current grants on public :
\dn+ public

-- Secure baseline (PostgreSQL 15+ does this for new databases) :
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO PUBLIC;
```

WHY : pre-v15 PostgreSQL granted `CREATE` on `public` to every role by default, which let any logged-in role create objects that other roles would then resolve through `search_path`. v15 changed the default but legacy `pg_dump --create` round-trips keep the old ACL alive. Source : postgresql.org/docs/15/ddl-schemas.html, postgresql.org/docs/17/ddl-schemas.html.

### Pattern 3 : Use predefined roles instead of SUPERUSER for ops

ALWAYS reach for `pg_monitor`, `pg_read_all_data`, `pg_maintain`, `pg_signal_backend` before granting `SUPERUSER`.
NEVER grant `SUPERUSER` to a personal account ; superuser bypasses every check including row-level security and pg_hba `nosuperuser` directives.

```sql
-- DBA on-call role : can read everything, watch stats, kill bad sessions,
-- and (v17+) run VACUUM / ANALYZE / REINDEX. CANNOT change roles or own data.
CREATE ROLE oncall_dba LOGIN PASSWORD 'redacted';

GRANT pg_read_all_data TO oncall_dba;
GRANT pg_monitor       TO oncall_dba;
GRANT pg_signal_backend TO oncall_dba;
GRANT pg_maintain      TO oncall_dba;   -- v17+ only

-- Verify NOT superuser :
SELECT rolname, rolsuper, rolinherit, rolcreaterole, rolcreatedb,
       rolcanlogin, rolreplication, rolbypassrls
FROM pg_roles WHERE rolname = 'oncall_dba';
```

WHY : predefined roles are a fixed-scope, non-cascading capability set ; you can audit "who has X" by reading `pg_auth_members`. `SUPERUSER` is an unbounded capability. `pg_read_all_data` does **not** set `BYPASSRLS` ; if RLS is in use and you genuinely need to read filtered tables, also grant `BYPASSRLS` explicitly. Source : postgresql.org/docs/17/predefined-roles.html.

### Pattern 4 : v16 explicit ADMIN / INHERIT / SET on role membership

ALWAYS specify `WITH ADMIN OPTION` (or `WITH ADMIN TRUE`) when a member must be allowed to grant the role onward.
NEVER assume membership implies inheritance ; from v16 a member that joins with `WITH INHERIT FALSE` must `SET ROLE` explicitly.

```sql
-- v16+ explicit options. Member 'alice' joins 'app_owner' :
GRANT app_owner TO alice
  WITH ADMIN OPTION                 -- alice may grant app_owner to others
       INHERIT FALSE                -- alice does NOT auto-inherit app_owner's privileges
       SET TRUE;                    -- alice CAN do SET ROLE app_owner

-- Inspect membership options :
SELECT roleid::regrole AS granted_role,
       member::regrole AS grantee,
       admin_option, inherit_option, set_option
FROM pg_auth_members WHERE member = 'alice'::regrole;

-- v16 also changed CREATEROLE : a role with CREATEROLE can only grant
-- attributes on a target role if it has ADMIN OPTION on that target.
```

WHY : pre-v16 the role-creator automatically had `ADMIN OPTION` on every role they created and `INHERIT` was a role-level attribute. v16 rewrote both : `INHERIT` / `SET` / `ADMIN` are now per-membership flags so an ops role can be in a group without inheriting its full DML rights until it deliberately calls `SET ROLE`. Source : postgresql.org/docs/16/sql-grant.html, postgresql.org/docs/17/sql-createrole.html.

### Pattern 5 : Column-level and row-level scoping without RLS

ALWAYS prefer column-level `GRANT` to a view-wrapper when you just need to hide columns.
NEVER use a `SECURITY DEFINER` function to fake row filtering when RLS is the right tool ; see `postgres-core-rls-policies`.

```sql
-- Hide salary column from app_reader, expose the rest :
GRANT SELECT (id, email, hired_at) ON employees TO app_reader;
-- Row level filtering still allowed only via a view or RLS policy :
CREATE VIEW employees_pub AS SELECT id, email, hired_at FROM employees;
GRANT SELECT ON employees_pub TO app_reader;
```

WHY : a column-level grant is enforced by the executor on every plan that touches `employees`. The view variant is enforced when callers reference `employees_pub`. Column-level grants do **not** override a table-level revoke. Source : postgresql.org/docs/17/sql-grant.html.

## Anti-Patterns :

(One-liners ; full diagnosis + fix in `references/anti-patterns.md`.)

- Granting `SUPERUSER` to the application login : bypasses RLS, audit logs, and `nosuperuser` pg_hba lines. Use a predefined role instead.
- Using `CREATE USER` and assuming it differs from `CREATE ROLE` : it is identical except for the implicit `LOGIN` default.
- Running `GRANT SELECT ON ALL TABLES IN SCHEMA app TO app_reader` and expecting future tables to inherit the grant : it grants only on existing tables ; use `ALTER DEFAULT PRIVILEGES` for future objects.
- Granting `pg_read_all_data` to bypass RLS : `pg_read_all_data` does NOT set `BYPASSRLS` ; the role still hits every policy.
- Leaving `CREATE` on schema `public` granted to `PUBLIC` after upgrading from pre-v15 : every logged-in role can create objects that shadow real tables via `search_path`.
- Granting role membership without `WITH SET TRUE` and then expecting `SET ROLE` to work : SQLSTATE `42501` permission denied to set role.
- Owning tables as the `postgres` superuser : a non-superuser application cannot ALTER or DROP them and `pg_dump --no-owner` is required for restores.
- Forgetting that column-level grants don't lift a table-level revoke : SELECT still fails with SQLSTATE `42501`.
- Assigning `BYPASSRLS` to the application login instead of a separate maintenance role : every query bypasses every policy.
- Treating REVOKE on `ALTER DEFAULT PRIVILEGES IN SCHEMA` as a way to remove globally-granted defaults : per-schema defaults can only **add** to global defaults.

## Reference Links :

- [references/methods.md](references/methods.md) : Full CREATE ROLE / GRANT / ALTER DEFAULT PRIVILEGES syntax, attribute defaults, predefined-roles table, system-catalog cheat sheet
- [references/examples.md](references/examples.md) : Verified end-to-end privilege provisioning scripts (read-only user, migration role, multi-tenant), version-annotated
- [references/anti-patterns.md](references/anti-patterns.md) : Security anti-patterns with cause, symptom, SQLSTATE (where applicable), and fix pattern

## See Also :

- `postgres-core-rls-policies` : row-level security policies, USING / WITH CHECK, PERMISSIVE vs RESTRICTIVE
- `postgres-errors-connection-auth` : pg_hba.conf records, SCRAM-SHA-256, peer authentication, SSL modes
- `postgres-core-architecture` : transactional DDL applies to GRANT / REVOKE / CREATE ROLE inside `BEGIN`
- `postgres-core-version-matrix` : v16 role-membership rewrite, v17 pg_maintain, v16 pg_use_reserved_connections
- `postgres-impl-configuration-tuning` : `password_encryption`, `ssl`, `default_table_access_method`
- Vooronderzoek section : §13 (Security Model)
- Official docs : https://www.postgresql.org/docs/17/sql-createrole.html, https://www.postgresql.org/docs/17/sql-grant.html, https://www.postgresql.org/docs/17/predefined-roles.html, https://www.postgresql.org/docs/17/sql-alterdefaultprivileges.html
