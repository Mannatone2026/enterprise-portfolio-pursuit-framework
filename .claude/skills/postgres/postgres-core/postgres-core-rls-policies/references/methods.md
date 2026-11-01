# RLS Methods Reference

Full syntax reference for Row-Level Security in PostgreSQL 15, 16, and 17. Every form below is verified against `postgresql.org/docs/17/ddl-rowsecurity.html`, `sql-createpolicy.html`, `sql-alterpolicy.html`, `sql-droppolicy.html`, and `sql-altertable.html` on 2026-05-19.

## Enabling RLS On A Table :

```sql
ALTER TABLE <table> ENABLE  ROW LEVEL SECURITY;   -- turn on
ALTER TABLE <table> DISABLE ROW LEVEL SECURITY;   -- turn off
ALTER TABLE <table> FORCE   ROW LEVEL SECURITY;   -- subject the table owner to policies
ALTER TABLE <table> NO FORCE ROW LEVEL SECURITY;  -- exempt the owner again (default)
```

Semantics :

- `ENABLE` flips `pg_class.relrowsecurity = true`. Without any policy, the table is effectively empty for non-owners (default-deny).
- `FORCE` flips `pg_class.relforcerowsecurity = true`. The table owner is now subject to policies. Does NOT affect `BYPASSRLS` roles or superusers.
- `DISABLE` flips `relrowsecurity = false`. Existing policies remain in `pg_policy` but are not evaluated. Re-enabling restores them.
- These four statements are NOT transactional in the sense that they take an `ACCESS EXCLUSIVE` lock on the table. Schedule them carefully.

## CREATE POLICY Full Syntax :

```sql
CREATE POLICY <policy_name> ON <table_name>
  [ AS { PERMISSIVE | RESTRICTIVE } ]
  [ FOR { ALL | SELECT | INSERT | UPDATE | DELETE } ]
  [ TO { role_name | PUBLIC | CURRENT_ROLE | CURRENT_USER | SESSION_USER } [, ...] ]
  [ USING (<using_expression>) ]
  [ WITH CHECK (<check_expression>) ];
```

Defaults when omitted :

- `AS` defaults to `PERMISSIVE`.
- `FOR` defaults to `ALL` (every command).
- `TO` defaults to `PUBLIC` (every role).
- `USING` defaults to NULL, which means "no rows are visible / no rows can be targeted". On a `FOR INSERT` policy, `USING` is not used; omitting it is correct.
- `WITH CHECK` defaults to the same expression as `USING` when applicable (INSERT and UPDATE-new-row), or NULL if `USING` is also NULL. Whenever the intended `WITH CHECK` differs from `USING`, write it explicitly.

## Clause Semantics By Command :

| Command   | USING evaluated ?         | WITH CHECK evaluated ?    |
|-----------|---------------------------|---------------------------|
| SELECT    | Yes : filters output rows | No                        |
| INSERT    | No                        | Yes : validates new row   |
| UPDATE    | Yes : filters target rows | Yes : validates new row   |
| DELETE    | Yes : filters target rows | No                        |
| ALL       | Same as SELECT/UPDATE-target/DELETE | Same as INSERT/UPDATE-new-row |

UPDATE evaluates `USING` first to determine which rows the user is allowed to target, THEN runs the actual UPDATE, THEN evaluates `WITH CHECK` on the resulting row. If WITH CHECK fails, the UPDATE is rolled back (per-row).

## PERMISSIVE vs RESTRICTIVE :

The full row predicate that PostgreSQL applies is :

```
(restrictive_policy_1 AND restrictive_policy_2 AND ...)
  AND
(permissive_policy_1 OR permissive_policy_2 OR ...)
```

Implications :

- If NO permissive policy applies to the (role, command) combination, every query returns zero rows. RESTRICTIVE-only policies are not enough.
- A RESTRICTIVE policy that returns FALSE blocks everything regardless of how many PERMISSIVE policies pass.
- RESTRICTIVE policies are an opt-in feature : add `AS RESTRICTIVE` explicitly. Default `PERMISSIVE` is correct for the happy-path case.

## TO Role Semantics :

`TO authenticated`, `TO anon`, `TO PUBLIC`, `TO my_role` etc. control which roles the policy applies to. Multiple roles : `TO role_a, role_b`. If the connected role does not match ANY policy's `TO` clause, the table appears empty for that role.

Supabase-specific roles :

- `anon` : the unauthenticated public role for PostgREST.
- `authenticated` : any user with a valid JWT.
- `service_role` : Supabase's `BYPASSRLS` role for trusted server-side work.
- `postgres` : the table owner; subject to policies ONLY when `FORCE ROW LEVEL SECURITY` is set.

## ALTER POLICY :

```sql
ALTER POLICY <policy_name> ON <table_name> RENAME TO <new_name>;

ALTER POLICY <policy_name> ON <table_name>
  [ TO { role_name | PUBLIC } [, ...] ]
  [ USING (<expression>) ]
  [ WITH CHECK (<expression>) ];
```

Notes :

- ALTER POLICY can change `TO`, `USING`, `WITH CHECK`. It CANNOT change `AS PERMISSIVE`/`RESTRICTIVE` or `FOR` (the command). To change those, DROP and CREATE.
- Renaming is a separate form. RENAME cannot be combined with other ALTER actions.

## DROP POLICY :

```sql
DROP POLICY [ IF EXISTS ] <policy_name> ON <table_name> [ CASCADE | RESTRICT ];
```

`CASCADE` is accepted for syntactic compatibility but has no effect : policies have no dependent objects.

## Introspection Catalogs And Views :

| Object | Purpose | Example |
|---|---|---|
| `pg_policy` | Canonical catalog of all policies | `SELECT * FROM pg_policy WHERE polrelid = 'public.t'::regclass;` |
| `pg_policies` | Human-readable view over `pg_policy` | `SELECT * FROM pg_policies WHERE tablename = 't';` |
| `pg_class.relrowsecurity` | Is RLS enabled on the table | `SELECT relrowsecurity FROM pg_class WHERE relname = 't';` |
| `pg_class.relforcerowsecurity` | Is RLS forced on the owner | `SELECT relforcerowsecurity FROM pg_class WHERE relname = 't';` |
| `pg_roles.rolbypassrls` | Does this role bypass RLS | `SELECT rolname FROM pg_roles WHERE rolbypassrls;` |
| `\d+ <table>` (psql) | Shows policies in dump form | `\d+ tasks` |

## Audit Helper (Drop-In) :

```sql
CREATE OR REPLACE FUNCTION audit_rls_status(p_schema text DEFAULT 'public')
RETURNS TABLE (
  schemaname        text,
  tablename         text,
  rls_enabled       boolean,
  rls_forced        boolean,
  policy_count      bigint,
  permissive_count  bigint,
  restrictive_count bigint
) LANGUAGE sql STABLE AS $$
  SELECT
    n.nspname::text,
    c.relname::text,
    c.relrowsecurity,
    c.relforcerowsecurity,
    COUNT(p.oid),
    COUNT(p.oid) FILTER (WHERE p.polpermissive),
    COUNT(p.oid) FILTER (WHERE NOT p.polpermissive)
  FROM pg_class c
  JOIN pg_namespace n ON n.oid = c.relnamespace
  LEFT JOIN pg_policy p ON p.polrelid = c.oid
  WHERE c.relkind = 'r' AND n.nspname = p_schema
  GROUP BY n.nspname, c.relname, c.relrowsecurity, c.relforcerowsecurity
  ORDER BY c.relname;
$$;

-- Usage :
SELECT * FROM audit_rls_status('public') WHERE rls_enabled = false;  -- tables missing RLS
SELECT * FROM audit_rls_status('public') WHERE policy_count = 0 AND rls_enabled;  -- enabled but no policies = empty
```

## Switching `row_security` GUC :

`row_security` is a session GUC controlling how the planner handles RLS :

- `row_security = on` (default) : policies are silently applied and rows that don't match are filtered out.
- `row_security = off` : if any policy would filter or block, the query ERRORS instead of silently returning a filtered set. Useful for `pg_dump` to fail loudly rather than produce incomplete dumps.

Set per session : `SET row_security = off;`. Set per role : `ALTER ROLE etl_worker SET row_security = off;`.

## BYPASSRLS Attribute :

```sql
CREATE ROLE etl_worker WITH LOGIN BYPASSRLS PASSWORD '...';
ALTER ROLE etl_worker WITH BYPASSRLS;     -- grant
ALTER ROLE etl_worker WITH NOBYPASSRLS;   -- revoke
```

- Only a superuser can grant `BYPASSRLS`.
- Bypasses ALL policies on ALL tables for that role, system-wide.
- Audit regularly : `SELECT rolname FROM pg_roles WHERE rolbypassrls;`
- Document every role that has it.

## Security-Definer Escape Hatch :

When a service needs to perform actions that legitimately cross RLS boundaries (admin reports, cross-tenant joins), wrap the work in a SECURITY DEFINER function owned by a role with the required privileges. The function executes with the owner's privileges, including BYPASSRLS if applicable :

```sql
CREATE FUNCTION admin_cross_tenant_count() RETURNS bigint
LANGUAGE sql SECURITY DEFINER SET search_path = public AS $$
  SELECT count(*) FROM invoices;  -- runs as the function owner, who can be a BYPASSRLS role
$$;

REVOKE EXECUTE ON FUNCTION admin_cross_tenant_count FROM PUBLIC;
GRANT  EXECUTE ON FUNCTION admin_cross_tenant_count TO authenticated_admin;
```

Always REVOKE from PUBLIC and explicitly GRANT EXECUTE to the role that should call it. Always set `search_path` to a fixed value to prevent search-path injection.
