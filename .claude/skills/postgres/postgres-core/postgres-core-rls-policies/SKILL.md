---
name: postgres-core-rls-policies
description: >
  Use when implementing multi-tenant isolation, Supabase auth-based row access, or any per-row authorization in PostgreSQL.
  Prevents forgetting WITH CHECK on UPDATE (silent data leak via update-target), missing FORCE ROW LEVEL SECURITY (owner bypasses policies), and confusing PERMISSIVE vs RESTRICTIVE evaluation order.
  Covers ENABLE / FORCE ROW LEVEL SECURITY, CREATE POLICY (USING vs WITH CHECK semantics), PERMISSIVE vs RESTRICTIVE combinators, policy-evaluation order per command, role-per-policy decomposition, BYPASSRLS attribute, Supabase auth.uid() / auth.jwt() patterns.
  Keywords: ROW LEVEL SECURITY, RLS, CREATE POLICY, USING, WITH CHECK, PERMISSIVE, RESTRICTIVE, FORCE ROW LEVEL SECURITY, BYPASSRLS, auth.uid, auth.jwt, Supabase RLS, multi-tenant, row not visible, user sees other tenant data, why can owner bypass my policy, how to write a policy, RLS not working, policy not applied, ERROR new row violates row-level security policy, ERROR permission denied for table
license: MIT
compatibility: "Designed for Claude Code. Requires PostgreSQL 15, 16, or 17."
metadata:
  author: OpenAEC-Foundation
  version: "1.0"
---

# postgres-core-rls-policies

## Quick Reference :

Row-Level Security (RLS) adds per-row visibility and mutability filters that act as implicit `WHERE` clauses on every query. RLS is enforced by PostgreSQL itself, not by application code, which makes it the only correct primitive for multi-tenant isolation on a shared schema.

Four facts to internalize before writing any policy :

1. RLS is OFF by default. `ALTER TABLE t ENABLE ROW LEVEL SECURITY` turns it on. When enabled with NO policies, the default is **deny everything** for non-owners.
2. The table owner and any role with `BYPASSRLS` skip policies entirely. Apply `ALTER TABLE t FORCE ROW LEVEL SECURITY` to subject the owner to policies (does NOT affect BYPASSRLS or superuser).
3. `USING (expr)` filters EXISTING rows (visibility for SELECT, UPDATE-target, DELETE). `WITH CHECK (expr)` validates NEW row values (INSERT, post-UPDATE). UPDATE policies need BOTH or users can update to rows they could not insert.
4. PERMISSIVE policies (default) are OR-combined. RESTRICTIVE policies are AND-combined. The full predicate is `(R1 AND R2 AND ...) AND (P1 OR P2 OR ...)`. At least one PERMISSIVE policy must exist or every query returns zero rows.

Two-line minimum-viable policy (per-user task ownership) :

```sql
ALTER TABLE tasks ENABLE ROW LEVEL SECURITY;
ALTER TABLE tasks FORCE ROW LEVEL SECURITY;
CREATE POLICY tasks_owner ON tasks FOR ALL TO authenticated
  USING (user_id = (SELECT auth.uid())) WITH CHECK (user_id = (SELECT auth.uid()));
```

Wrapping `auth.uid()` in a subquery (`(SELECT auth.uid())`) is the official Supabase performance recommendation : it makes PostgreSQL evaluate the function once per query instead of per row, which is critical for large result sets. Always create an index on policy-referenced columns (`CREATE INDEX ON tasks (user_id)`).

## When To Use This Skill :

ALWAYS use this skill when :

- A user asks how to isolate tenants, users, or organizations on a shared table
- Writing or reviewing any `CREATE POLICY` / `ALTER POLICY` / `DROP POLICY` statement
- Configuring Supabase tables that are exposed via PostgREST (RLS is the only access layer)
- Debugging "row not visible" / "user sees other tenant data" / `new row violates row-level security policy` errors
- Auditing whether table owner or service roles bypass RLS unintentionally
- Designing a `BYPASSRLS` service role for an ETL or background worker

NEVER use this skill for :

- Column-level privileges (use `GRANT SELECT (col1, col2) ON t TO role` instead, see [postgres-core-architecture](../postgres-core-architecture/SKILL.md))
- Encryption-at-rest or transparent data encryption (RLS is access control, not crypto)
- Replacing application authentication (RLS authorizes; authentication is upstream)
- Foreign-key constraint visibility (FK checks bypass RLS unconditionally, see anti-patterns)

## Decision Trees :

### Should this table have RLS :

```
Is the table exposed to multiple end-users on a shared schema (multi-tenant) ?
├── YES → ENABLE + FORCE ROW LEVEL SECURITY. Write at least one PERMISSIVE policy.
└── NO  → Is it exposed via PostgREST / Supabase API ?
         ├── YES → ENABLE RLS. Even a single-tenant app needs a default-deny stance.
         └── NO  → Only a trusted service role connects → standard GRANT model is sufficient.
```

### USING vs WITH CHECK :

```
What command does the policy apply to ?
├── SELECT → USING only. WITH CHECK is ignored.
├── INSERT → WITH CHECK only. USING is not evaluated (no pre-existing row).
├── UPDATE → BOTH. USING filters which rows the user can target, WITH CHECK validates the new values.
├── DELETE → USING only. WITH CHECK is ignored (no post-state row).
└── ALL    → USING applies to SELECT/UPDATE-target/DELETE. WITH CHECK applies to INSERT and post-UPDATE. If WITH CHECK is omitted, it DEFAULTS to USING for INSERT/UPDATE-new-row (CRITICAL : verify intent).
```

### PERMISSIVE or RESTRICTIVE :

```
Is this policy describing the HAPPY PATH (the rows a user is allowed to see) ?
├── YES → PERMISSIVE (default). At least one must pass.
└── NO  → Is it a GLOBAL CONSTRAINT applied on top of all happy-path policies ?
         ├── YES → RESTRICTIVE. AND-combined with the OR-of-permissives.
         └── NO  → Reconsider; you almost certainly want PERMISSIVE.
```

Typical shape : N PERMISSIVE policies (one per role or per access pattern) plus 0 or 1 RESTRICTIVE policy for cross-cutting constraints (e.g. "admin actions only from local socket", "soft-deleted rows hidden globally").

### One policy or many :

```
Is the predicate identical across all roles ?
├── YES → single FOR ALL TO PUBLIC policy.
└── NO  → one policy per (role × command) tuple. Example :
         - FOR SELECT TO anon         USING (is_public = true)
         - FOR SELECT TO authenticated USING (is_public OR owner_id = (SELECT auth.uid()))
         - FOR INSERT TO authenticated WITH CHECK (owner_id = (SELECT auth.uid()))
         - FOR UPDATE TO authenticated USING (owner_id = (SELECT auth.uid()))
                                       WITH CHECK (owner_id = (SELECT auth.uid()))
         - FOR DELETE TO authenticated USING (owner_id = (SELECT auth.uid()))
```

Role-per-policy decomposition keeps each predicate minimal and lets PostgreSQL skip policy evaluation entirely for roles with no matching policy (the role gets zero visibility).

## Core Patterns :

### Pattern 1 : multi-tenant isolation by `tenant_id` :

```sql
ALTER TABLE invoices ENABLE ROW LEVEL SECURITY;
ALTER TABLE invoices FORCE ROW LEVEL SECURITY;

CREATE POLICY invoice_tenant_isolation ON invoices FOR ALL TO app_user
  USING      (tenant_id = current_setting('app.current_tenant_id', true)::uuid)
  WITH CHECK (tenant_id = current_setting('app.current_tenant_id', true)::uuid);

CREATE INDEX ON invoices (tenant_id);
```

The application sets `SET LOCAL app.current_tenant_id = '...'` per transaction. `current_setting(..., true)` returns NULL instead of error if unset, so users without a tenant simply see nothing (default-deny).

### Pattern 2 : Supabase user-owned rows :

```sql
ALTER TABLE notes ENABLE ROW LEVEL SECURITY;

CREATE POLICY notes_select ON notes FOR SELECT TO authenticated
  USING (user_id = (SELECT auth.uid()));

CREATE POLICY notes_insert ON notes FOR INSERT TO authenticated
  WITH CHECK (user_id = (SELECT auth.uid()));

CREATE POLICY notes_update ON notes FOR UPDATE TO authenticated
  USING      (user_id = (SELECT auth.uid()))
  WITH CHECK (user_id = (SELECT auth.uid()));

CREATE POLICY notes_delete ON notes FOR DELETE TO authenticated
  USING (user_id = (SELECT auth.uid()));

CREATE INDEX ON notes (user_id);
```

Note the `TO authenticated` clause : `anon` requests get zero rows because no policy applies, and PostgreSQL short-circuits policy evaluation. This is the official Supabase pattern for skipping policy work on unauthenticated traffic.

### Pattern 3 : RESTRICTIVE policy as global guard :

```sql
ALTER TABLE secrets ENABLE ROW LEVEL SECURITY;

CREATE POLICY secret_owner ON secrets AS PERMISSIVE FOR ALL TO authenticated
  USING (owner_id = (SELECT auth.uid()))
  WITH CHECK (owner_id = (SELECT auth.uid()));

CREATE POLICY secret_business_hours ON secrets AS RESTRICTIVE FOR SELECT TO authenticated
  USING (EXTRACT(hour FROM now() AT TIME ZONE 'UTC') BETWEEN 6 AND 22);
```

Effective predicate for SELECT : `(business_hours_check) AND (owner_id = auth.uid())`. The RESTRICTIVE policy applies on top of every PERMISSIVE policy.

### Pattern 4 : role with `BYPASSRLS` for ETL :

```sql
CREATE ROLE etl_worker WITH LOGIN BYPASSRLS PASSWORD '...';
GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA public TO etl_worker;
```

The ETL role bypasses every policy on every table. ALWAYS audit `SELECT rolname FROM pg_roles WHERE rolbypassrls;` before granting and document the role's purpose in a runbook. NEVER grant `BYPASSRLS` to a role that handles user input.

## Common Errors :

| Error message | Cause | Fix |
|---|---|---|
| `new row violates row-level security policy for table "..."` | INSERT or UPDATE produces a row that fails `WITH CHECK` | Verify the new row values satisfy WITH CHECK. Often a missing `tenant_id` or `user_id` column in the INSERT. |
| `permission denied for table ...` (before RLS evaluation) | Role lacks GRANT on the table; RLS never runs | `GRANT SELECT, INSERT, UPDATE, DELETE ON t TO role;` THEN policies apply. |
| Query returns zero rows for owner / superuser | Table owner bypasses RLS by default; FORCE is OFF | Either `SET ROLE other_role` or `ALTER TABLE t FORCE ROW LEVEL SECURITY;`. |
| User sees rows they should not | Missing policy filter, BYPASSRLS role, table owner without FORCE, or FK leak | Run introspection (see references/methods.md `audit_rls_status()`). Check `pg_class.relrowsecurity` AND `pg_class.relforcerowsecurity`. |
| `cannot truncate a table containing foreign-key references when row_security is on` (pg_dump) | `pg_dump` with default `row_security = off` raises on tables with policies | Set `row_security = on` in the dump command if filtered output is acceptable, otherwise dump as a role that bypasses RLS. |
| Performance degrades on large tables | `auth.uid()` evaluated per row | Wrap in subquery : `USING ((SELECT auth.uid()) = user_id)`. Add index on `user_id`. |

## Introspection :

ALWAYS run these queries when debugging RLS :

```sql
-- Is RLS enabled and forced on this table ?
SELECT relname, relrowsecurity AS enabled, relforcerowsecurity AS forced
FROM pg_class WHERE oid = 'public.tasks'::regclass;

-- List policies on a table with their full predicates :
SELECT polname, polcmd, polpermissive,
       pg_get_expr(polqual, polrelid)     AS using_expr,
       pg_get_expr(polwithcheck, polrelid) AS with_check_expr,
       (SELECT array_agg(rolname) FROM pg_roles WHERE oid = ANY(polroles)) AS roles
FROM pg_policy WHERE polrelid = 'public.tasks'::regclass;

-- Who can bypass RLS globally ?
SELECT rolname FROM pg_roles WHERE rolbypassrls OR rolsuper;

-- Simulate as a specific role to verify policies :
SET ROLE app_user;  -- assumes you have SET privilege
SELECT * FROM tasks;
RESET ROLE;
```

`pg_policies` view (less detail) and `pg_policy` system catalog (canonical) are both valid sources. Always prefer `pg_class.relrowsecurity` over `\d+` output when scripting.

## Anti-Patterns :

The five most common RLS bugs (full list with fixes in references/anti-patterns.md) :

1. ENABLE without FORCE → table owner silently bypasses every policy in `psql` testing.
2. `FOR UPDATE` policy with only `USING` and no `WITH CHECK` → user updates rows to values they cannot INSERT.
3. `auth.uid()` outside a subquery → evaluated per row → 10x-100x slower on large tables.
4. Missing index on policy-referenced column → `seq scan` regardless of statistics.
5. `BYPASSRLS` granted to an application role "just in case" → every policy on every table is silently bypassed.

## Version Notes :

RLS is stable since v9.5. All syntax in this skill applies to PostgreSQL 15, 16, and 17 identically. v15 introduced no RLS changes. v16 introduced no RLS changes. v17 introduced no RLS changes. The only adjacent-version-relevant change : v16 rewrote `GRANT role TO member` semantics, which can indirectly affect which roles are subject to which policies (see [postgres-core-version-matrix](../postgres-core-version-matrix/SKILL.md) §role-membership).

## Reference Links :

- [methods.md](references/methods.md) : full `CREATE POLICY` / `ALTER POLICY` / `DROP POLICY` syntax, every clause, introspection helpers, role-per-policy templates.
- [examples.md](references/examples.md) : 10 complete patterns including multi-tenant SaaS, soft-delete with RLS, time-bounded RESTRICTIVE policies, Supabase storage-bucket integration, security-definer escape hatches.
- [anti-patterns.md](references/anti-patterns.md) : 12 documented bugs with reproducible failure SQL and the official fix.

Official PostgreSQL documentation (verified 2026-05-19) :

- ENABLE ROW LEVEL SECURITY : https://www.postgresql.org/docs/17/ddl-rowsecurity.html
- CREATE POLICY : https://www.postgresql.org/docs/17/sql-createpolicy.html
- ALTER POLICY : https://www.postgresql.org/docs/17/sql-alterpolicy.html
- DROP POLICY : https://www.postgresql.org/docs/17/sql-droppolicy.html
- Supabase RLS guide : https://supabase.com/docs/guides/database/postgres/row-level-security
