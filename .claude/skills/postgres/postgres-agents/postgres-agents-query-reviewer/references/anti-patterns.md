# Reference : Reviewer Anti-Patterns

These are failure modes of the REVIEWER, not of the SQL under review. Each entry :
CAUSE, SYMPTOM, FIX. Last verified : 2026-05-19.

## AP-1 : Rubber-stamping

CAUSE : approving a query by eyeballing it, skipping checks because the query
"looks fine", or running only a subset of C-1..C-13.

SYMPTOM : a query passes review, then a `WHERE`-less `DELETE` (C-2) wipes a
table, or a `NOT IN` with NULLs (C-10) silently returns zero rows in production.
The defects were statically detectable; the reviewer never looked.

FIX : run all 13 checks on every query, every time. The checklist is the
contract. If a query is trivially short, the checks are trivially fast, not
skippable. Record each check's result so the report proves coverage.

## AP-2 : Seq-Scan-phobia

CAUSE : treating any `Seq Scan` in an `EXPLAIN` plan as a defect to be flagged.

SYMPTOM : the reviewer recommends an index on a 200-row lookup table, or on a
query that returns 90% of its rows. Both recommendations make the query SLOWER
and add write overhead. The author loses trust in the review.

FIX : a Seq Scan is the CORRECT plan for a small table and for a low-selectivity
query. Only flag a Seq Scan when the table is large, the predicate is selective,
and `Rows Removed by Filter` is high. Index selectivity, not Seq-Scan presence,
is the signal. See the SKILL.md decision tree "Is this Seq Scan actually a
problem?".

## AP-3 : Syntax-only review

CAUSE : checking that the SQL parses and reads cleanly, then stopping. The
reviewer never asks for or reads an execution plan.

SYMPTOM : a query with an implicit cast (C-3), a function on an indexed column
(C-5), or a missing index (C-12) is approved because the SQL "is correct". It is
correct, and it seqscans a 50M-row table on every call.

FIX : performance findings are unconfirmed until an `EXPLAIN (ANALYZE, BUFFERS)`
plan is seen. For any index-related check, recommend the EXPLAIN and mark the
finding "suspected" until the plan confirms it. Reviewing SQL means reviewing
how it will execute, not only how it reads.

## AP-4 : Severity inflation and deflation

CAUSE : grading findings by gut feeling instead of the rubric. Either every nit
becomes CRITICAL ("the team must take this seriously"), or a real bug is softened
to INFO ("it probably will not happen").

SYMPTOM : inflation : a `SELECT *` (WARNING) blocks a PR as CRITICAL, reviews
become noise, authors start ignoring them. Deflation : a `NOT IN` over a nullable
column (CRITICAL correctness bug) is filed as INFO and ships.

FIX : grade strictly by the rubric. CRITICAL = the result set is wrong or data
is lost. WARNING = correct but degrades at scale. INFO = style. The verdict is
mechanical : any CRITICAL forces `REQUEST CHANGES`; warnings cap the verdict at
`APPROVE WITH WARNINGS`.

## AP-5 : Citing nothing

CAUSE : reporting a finding with a problem and a fix but no pointer to the skill
that explains the fix in depth.

SYMPTOM : the author reads "add a GIN index" but does not know which operator
class, or "use keyset pagination" without the pattern. The review identifies the
problem but does not transfer the knowledge to fix it well.

FIX : every finding that maps to a checklist row carries that row's cite-skill
(see references/methods.md). The cite turns a one-line fix into a path to the
full pattern : C-4 -> postgres-syntax-jsonb, C-6 -> postgres-syntax-full-text-search,
C-8/C-11/C-12 -> postgres-impl-query-performance-toolkit, and so on.

## AP-6 : Reviewing past the skill's scope

CAUSE : while reviewing a query, the reviewer also starts grading table DDL,
migration locking, or RLS policy design.

SYMPTOM : the review mixes query findings with schema opinions, the verdict
becomes ambiguous, and DDL / migration concerns get a shallow treatment they
would not get from the dedicated skill.

FIX : this skill reviews QUERIES. Route DDL design to
`postgres-agents-schema-reviewer` and migration safety / locking to
`postgres-agents-migration-reviewer`. If a query review surfaces a schema
problem, note it and hand off; do not grade it here.
