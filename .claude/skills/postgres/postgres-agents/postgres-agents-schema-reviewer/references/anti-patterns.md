# Anti-Patterns : Schema Reviewer

These are anti-patterns of the REVIEWER, not of the schema. Each entry : the
anti-pattern, its root cause, the observable symptom, and the fix-pattern that
keeps the review trustworthy.

## AP-1 : Bikeshedding cosmetic naming while a CRITICAL is missed

**Anti-pattern** : the review spends most of its findings on `camelCase`
identifiers (S-5) and `char(n)` (S-14) and never mentions that a table has no
primary key (S-1).

**Cause** : naming and type nits are easy to spot and easy to write up.
Structural defects need the FK graph and a moment of thought, so they are
skipped under time pressure.

**Symptom** : a long, busy review that grades the schema `NEEDS-WORK` over
style, while a `POOR`-level data-integrity defect ships unflagged.

**Fix** : run all 16 checks before writing anything. Order findings by severity,
CRITICAL first. If a CRITICAL exists, it leads the report. Cosmetic INFO
findings go last and never crowd out a CRITICAL.

## AP-2 : Denormalization-by-ambush

**Anti-pattern** : the reviewer sees a denormalized column (S-10) or a wide
jsonb blob (S-8) and hard-fails the schema as `POOR` without asking whether the
choice was deliberate.

**Cause** : treating every normalization deviation as an error. Denormalization
is a legitimate, common performance decision (a cached count, a materialized
display name, an event-sourced jsonb payload).

**Symptom** : the author rejects the review as wrong, because the "defect" was a
considered trade-off. The review loses credibility and its real findings are
ignored too.

**Fix** : apply the deliberate-choice rule. Report S-8 / S-10 / S-11 / S-15 as
findings, but grade them at their normal severity and add a `Note:` asking the
author to confirm intent and document the rationale. NEVER escalate a plausibly
deliberate choice to CRITICAL, and never hard-fail it silently.

## AP-3 : Reviewing one table in isolation

**Anti-pattern** : grading each `CREATE TABLE` as it is read, before the rest of
the schema is parsed.

**Cause** : processing the DDL top to bottom and emitting findings inline,
instead of parsing everything first.

**Symptom** : false negatives on the highest-severity checks. An un-indexed FK
(S-3) is invisible because the parent table is read later. A redundant index
(S-6) is missed because its sibling index is on a table not yet seen. A
transitive dependency (S-10) spanning two tables is never detected.

**Fix** : two passes. Pass 1 parses every `CREATE TABLE`, column, constraint,
and index and builds the foreign-key graph. Pass 2 runs the checks against the
complete model. S-1, S-3, S-6, and S-10 are graded only in pass 2.

## AP-4 : Severity inflation and deflation

**Anti-pattern** : grading a `char(n)` choice (S-14) as CRITICAL, or grading a
missing primary key (S-1) as INFO.

**Cause** : grading by how strongly the reviewer dislikes the choice, or by how
small the fix looks, instead of by the consequence.

**Symptom** : the verdict stops meaning anything. An author who sees a CRITICAL
for a cosmetic nit learns to ignore CRITICALs ; a real `POOR` schema is graded
`GOOD`.

**Fix** : grade strictly by the severity rubric in methods.md. CRITICAL = data
integrity or tenant isolation broken. WARNING = scale or maintenance defect.
INFO = style or context-dependent. The size of the fix is irrelevant : a
one-line fix can still be CRITICAL.

## AP-5 : Citing nothing

**Anti-pattern** : reporting a finding ("this FK has no index") with no `Cite:`
line pointing at the sibling skill that explains the fix.

**Cause** : treating the review as a list of complaints rather than an
actionable hand-off.

**Symptom** : the author knows something is wrong but has no path to the
detailed guidance ; the finding generates a question instead of a fix.

**Fix** : every finding carries a `Cite:` line with the exact sibling skill
(`postgres-impl-indexing-strategy`, `postgres-core-rls-policies`, etc.). The
cite is part of the finding, not optional.

## AP-6 : Rubber-stamping

**Anti-pattern** : declaring a schema `GOOD` after a quick scan, without running
all 16 checks.

**Cause** : the schema "looks fine", or the reviewer trusts the author.

**Symptom** : a `GOOD` verdict on a schema that has an un-indexed FK and a
multi-tenant table with no RLS ; the defects surface in production.

**Fix** : `GOOD` is earned, not assumed. It requires that all 16 checks ran and
none produced a CRITICAL or WARNING. State the scope (`N tables, M findings`) so
the coverage is visible. A review with zero findings still lists the checks as
run.

## AP-7 : Confusing schema review with query or migration review

**Anti-pattern** : the schema reviewer also critiques the `SELECT` statements or
the migration's locking behaviour.

**Cause** : scope creep ; all three concern the database.

**Symptom** : findings the schema-reviewer checklist does not define, graded
with an ad-hoc severity, inconsistent with what the dedicated reviewer would
say.

**Fix** : stay in scope. Schema reviewer grades table and column DDL design.
Hand query concerns to `postgres-agents-query-reviewer` and migration locking /
rollout concerns to `postgres-agents-migration-reviewer`. Reference them in
`See Also`, do not absorb their job.
