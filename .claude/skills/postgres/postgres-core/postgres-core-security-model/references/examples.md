# postgres-core-security-model : Examples

All examples were checked against the v17 docs cited at the bottom. Where v15-only or v16-only behavior matters it is annotated. ALL examples assume `psql` connected as a SUPERUSER unless noted.

## Example 1 : Read-only reporting user (v15+)

```sql
-- 1. Create the role
CREATE ROLE bi_reader LOGIN
  PASSWORD 'rotate me'
  CONNECTION LIMIT 10
  VALID UNTIL '2027-06-30';

-- 2. Database-level CONNECT (CONNECT is granted to PUBLIC by default ; revoke if locked down)
GRANT CONNECT ON DATABASE app_prod TO bi_reader;

-- 3. Schema USAGE
GRANT USAGE ON SCHEMA app, reporting TO bi_reader;

-- 4. SELECT on existing tables
GRANT SELECT ON ALL TABLES IN SCHEMA app, reporting TO bi_reader;
GRANT SELECT ON ALL SEQUENCES IN SCHEMA app, reporting TO bi_reader;

-- 5. SELECT on FUTURE tables (created by the schema owner ; adjust FOR ROLE)
ALTER DEFAULT PRIVILEGES FOR ROLE app_owner IN SCHEMA app, reporting
  GRANT SELECT ON TABLES TO bi_reader;
ALTER DEFAULT PRIVILEGES FOR ROLE app_owner IN SCHEMA app, reporting
  GRANT SELECT ON SEQUENCES TO bi_reader;

-- Verify :
SELECT has_table_privilege('bi_reader','app.orders','SELECT');   -- t
SELECT has_table_privilege('bi_reader','app.orders','INSERT');   -- f
```

## Example 2 : Application user with grant chain via owner role (v15+)

```sql
-- Owner role : no login, owns the schema
CREATE ROLE app_owner NOLOGIN;
CREATE SCHEMA app AUTHORIZATION app_owner;

-- App login role
CREATE ROLE app_user LOGIN PASSWORD 'secret' CONNECTION LIMIT 100;
GRANT CONNECT ON DATABASE app_prod TO app_user;
GRANT USAGE ON SCHEMA app TO app_user;

-- Make sure ALL future objects owned by app_owner in schema app are usable :
ALTER DEFAULT PRIVILEGES FOR ROLE app_owner IN SCHEMA app
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO app_user;
ALTER DEFAULT PRIVILEGES FOR ROLE app_owner IN SCHEMA app
  GRANT USAGE, SELECT ON SEQUENCES TO app_user;
ALTER DEFAULT PRIVILEGES FOR ROLE app_owner IN SCHEMA app
  GRANT EXECUTE ON FUNCTIONS TO app_user;

-- Migration runner switches to the owner role to create objects :
-- (executed by a DBA role that holds app_owner membership)
SET ROLE app_owner;
CREATE TABLE app.orders (id bigserial primary key, customer_id bigint);
RESET ROLE;
```

## Example 3 : v16+ explicit role-membership options

```sql
-- v16+ : alice may switch identity but does NOT auto-inherit privileges
GRANT app_owner TO alice
  WITH INHERIT FALSE   -- privileges only via explicit SET ROLE
       SET TRUE        -- SET ROLE app_owner allowed
       ADMIN FALSE;    -- alice may NOT GRANT app_owner onward

-- alice can now run :
SET ROLE app_owner;
-- ... DDL ...
RESET ROLE;

-- v16+ adds ADMIN OPTION distinction for CREATEROLE :
GRANT CREATEROLE TO bob;             -- bob can create roles
GRANT app_owner TO bob WITH ADMIN OPTION;
-- Only now may bob run :
GRANT app_owner TO carol;
```

Behavioral difference vs pre-v16 : on v15, `GRANT app_owner TO alice` alone allowed alice to grant `app_owner` onward (because role-creators were implicit admins). v16 requires `WITH ADMIN OPTION` explicitly.

## Example 4 : Predefined-role-based DBA without SUPERUSER (v17+)

```sql
CREATE ROLE oncall LOGIN PASSWORD 'redacted' CONNECTION LIMIT 5;

-- Read every table everywhere
GRANT pg_read_all_data    TO oncall;
-- pg_stat_*, pg_settings, scan tables
GRANT pg_monitor          TO oncall;
-- Cancel / terminate non-superuser backends
GRANT pg_signal_backend   TO oncall;
-- v17 only : run VACUUM / ANALYZE / REINDEX / CLUSTER / REFRESH / LOCK TABLE
GRANT pg_maintain         TO oncall;
-- v16 only : keep working when max_connections is exhausted
GRANT pg_use_reserved_connections TO oncall;
-- v15 only : manual CHECKPOINT
GRANT pg_checkpoint       TO oncall;

-- Confirm oncall is NOT superuser :
SELECT rolname, rolsuper, rolbypassrls FROM pg_roles WHERE rolname = 'oncall';
-- rolname | rolsuper | rolbypassrls
-- oncall  | f        | f
```

NOTE : `pg_read_all_data` does NOT set `BYPASSRLS`. If oncall must read tables that have RLS policies, grant `BYPASSRLS` separately :

```sql
ALTER ROLE oncall BYPASSRLS;
```

## Example 5 : Logical-replication subscriber role (v16+)

```sql
-- Publisher side : login role with REPLICATION attribute
CREATE ROLE repl_user LOGIN PASSWORD 'redacted'
  REPLICATION
  CONNECTION LIMIT 5;

GRANT CONNECT ON DATABASE source_db TO repl_user;
GRANT USAGE ON SCHEMA public TO repl_user;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO repl_user;

-- Subscriber side (v16+) : non-superuser may create the subscription
CREATE ROLE sub_owner LOGIN PASSWORD 'redacted';
GRANT pg_create_subscription TO sub_owner;
GRANT CREATE ON DATABASE target_db TO sub_owner;

SET ROLE sub_owner;
CREATE SUBSCRIPTION my_sub
  CONNECTION 'host=publisher dbname=source_db user=repl_user password=redacted'
  PUBLICATION pub_all;
RESET ROLE;
```

## Example 6 : Lock down the public schema after upgrade

```sql
-- Inspect baseline :
\dn+ public
-- Pre-v15 default :
--   public | postgres | =UC/postgres
-- ^ "=UC/postgres" means PUBLIC has USAGE and CREATE granted by postgres.

-- Secure baseline :
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO PUBLIC;

-- v15+ new databases : ownership is pg_database_owner ; CREATE is NOT granted to PUBLIC.
-- Confirm :
\dn+ public
-- public | pg_database_owner | postgres=UC/postgres   +
--                              | =U/postgres
```

## Example 7 : Column-level scoping for PII

```sql
-- Allow analytics to read everything EXCEPT salary
REVOKE SELECT ON employees FROM analytics;
GRANT SELECT (id, email, hired_at, department_id) ON employees TO analytics;

-- Attempt SELECT * is rejected ; SELECT of allowed columns succeeds :
SET ROLE analytics;
SELECT * FROM employees;     -- ERROR : permission denied for column salary
SELECT id, email FROM employees;  -- OK
RESET ROLE;
```

## Example 8 : Audit "who can see this table?"

```sql
-- Effective privileges on app.orders, expanded across role membership :
SELECT grantee, privilege_type, is_grantable
FROM information_schema.table_privileges
WHERE table_schema = 'app' AND table_name = 'orders'
ORDER BY grantee, privilege_type;

-- Role-membership-aware test :
SELECT rolname,
       has_table_privilege(rolname, 'app.orders', 'SELECT') AS can_select,
       has_table_privilege(rolname, 'app.orders', 'INSERT') AS can_insert
FROM pg_roles
WHERE rolcanlogin
ORDER BY rolname;
```

## Example 9 : Reassign before drop

```sql
-- Cannot drop a role that still owns objects :
DROP ROLE app_owner;
-- ERROR : role "app_owner" cannot be dropped because some objects depend on it

-- Reassign, drop owned privileges, then drop the role :
REASSIGN OWNED BY app_owner TO new_owner;
DROP OWNED BY app_owner;   -- removes grants made BY app_owner + default-priv entries
DROP ROLE app_owner;
```

`REASSIGN OWNED BY` operates per-database ; run in every database the role had objects in.

## Example 10 : Verify role attributes at scale

```sql
SELECT rolname,
       rolsuper          AS superuser,
       rolinherit        AS inherits,
       rolcreaterole     AS can_create_roles,
       rolcreatedb       AS can_create_dbs,
       rolcanlogin       AS can_login,
       rolreplication    AS can_replicate,
       rolbypassrls      AS bypass_rls,
       rolconnlimit      AS conn_limit,
       rolvaliduntil     AS password_valid_until
FROM pg_roles
ORDER BY rolname;
```

Sources verified against : postgresql.org/docs/17/sql-createrole.html, postgresql.org/docs/17/sql-grant.html, postgresql.org/docs/17/predefined-roles.html, postgresql.org/docs/17/sql-alterdefaultprivileges.html, postgresql.org/docs/17/sql-set-role.html, postgresql.org/docs/17/sql-reassignowned.html.
