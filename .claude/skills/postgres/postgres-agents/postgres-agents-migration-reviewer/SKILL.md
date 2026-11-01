---
name: postgres-agents-migration-reviewer
description: >
  Use when reviewing a database migration for production safety before it runs, or auditing a migration file in a pull request.
  Prevents shipping a plain CREATE INDEX that locks writes, a full-rewrite ALTER COLUMN TYPE, a breaking DROP/RENAME without expand-contract, and migrations that stall all traffic without lock_timeout.
  Covers a deterministic migration safety checklist (CONCURRENTLY, rewrite-triggering ALTERs, NOT VALID + VALIDATE, breaking changes, lock_timeout, chunked backfill, CONCURRENTLY-in-transaction), severity grading, safe-rewrite suggestions, SAFE / NEEDS-CHANGES / UNSAFE verdict.
  Keywords: migration review, migration safety, zero downtime, review migration, CREATE INDEX CONCURRENTLY, ALTER TABLE lock, breaking change, expand contract, is this migration safe, will this migration lock the table, migration checklist, schema change review
license: MIT
compatibility: "Designed for Claude Code. Requires PostgreSQL 15, 16, or 17."
metadata:
  author: OpenAEC-Foundation
  version: "1.0"
---

# postgres-agents-migration-reviewer

## Quick Reference :

This skill turns Claude into a migration safety reviewer. It is an
orchestration skill, not a DDL reference: it defines a fixed 14-point checklist,
a severity grading scheme, and which sibling skill to cite per finding. Run
every check against every statement in the migration. Never approve a migration
by inspection alone.

```
Review workflow :
  1. PARSE   -- split the migration into statements; identify each DDL/DML kind
               and its target table; note transaction boundaries
  2. CHECK   -- run all 14 checks (M-1..M-14), record each hit with a location
  3. LOCK    -- for any locking concern, recommend a rehearsal on a copy of
               production-sized data, timed under concurrent load
  4. REPORT  -- emit structured findings + one overall verdict
```

Severity grades :
- **CRITICAL** : blocks production traffic, rewrites a table, loses data, or
  breaks the running app. Verdict becomes UNSAFE.
- **WARNING** : safe in principle but a foot-gun at scale or under load
  (long lock, bloat, no timeout guard). Verdict becomes NEEDS-CHANGES.
- **INFO** : process or hygiene note; the author decides.

Verdict :
- `SAFE` : no CRITICAL and no WARNING.
- `NEEDS-CHANGES` : one or more WARNING, no CRITICAL.
- `UNSAFE` : one or more CRITICAL.

The core principle : lock danger is about CONCURRENT TRAFFIC, not table size. A
plain `CREATE INDEX` on a 500-row table still blocks every write to that table
for the build's duration. ALWAYS judge a lock by what it blocks and for how
long, NEVER by row count.

## When To Use This Skill :

ALWAYS use this skill when :
- A user asks "is this migration safe", "review this migration", "will this
  migration lock the table", or pastes a migration file for feedback
- Auditing a migration file in a pull request or a deploy diff
- Another agent generated schema-change SQL that must run against production

NEVER use this skill for :
- Reviewing plain queries for performance (use postgres-agents-query-reviewer)
- Writing the migration from scratch (use postgres-impl-zero-downtime-migrations)
- Reviewing table design / normalization (use postgres-core-schema-design)

## Decision Trees :

### Grading a finding :

```
A check fired. What severity?
  |
  |-- Does it block reads or writes to a hot table, rewrite a table,
  |   lose data, or break the running app?
  |     YES --> CRITICAL. Verdict becomes UNSAFE.
  |
  |-- Is it correct in isolation but a foot-gun under concurrent load
  |   or at scale (long lock, bloat, missing timeout guard)?
  |     YES --> WARNING. Verdict becomes NEEDS-CHANGES (if no CRITICAL).
  |
  |-- Is it a process / hygiene gap (no down path, no rehearsal note)?
        YES --> INFO. Does not change the verdict.
```

### Does this statement need an online-safe rewrite :

```
What does the statement do?
  |
  |-- CREATE INDEX (no CONCURRENTLY)
  |     --> blocks writes for the whole build. Require CONCURRENTLY,
  |         outside any transaction block.  [M-1, M-11]
  |
  |-- ALTER COLUMN TYPE
  |     --> full table rewrite unless binary-coercible with no USING.
  |         Require expand-contract.  [M-2]
  |
  |-- ADD COLUMN
  |     --> volatile DEFAULT rewrites the table; NOT NULL with no DEFAULT
  |         on a populated table fails.  [M-3, M-4]
  |
  |-- SET NOT NULL / ADD FOREIGN KEY / ADD CHECK
  |     --> scans (and FK/CHECK block writes) unless the
  |         NOT VALID + VALIDATE two-step is used.  [M-5, M-6]
  |
  |-- DROP COLUMN / RENAME COLUMN / RENAME TABLE
  |     --> breaks the running app unless app deploy is sequenced
  |         (expand-contract).  [M-7]
  |
  |-- Large UPDATE / DELETE backfill
        --> long lock + bloat unless chunked.  [M-9]
```

### Is this lock actually dangerous :

```
The statement takes a lock. Dangerous?
  |
  |-- Lock is ACCESS EXCLUSIVE or blocks writes, AND the table
  |   receives concurrent traffic?
  |     YES --> dangerous regardless of table size. The table is small
  |             but the traffic is not. Flag it.
  |
  |-- Lock is SHARE UPDATE EXCLUSIVE (CONCURRENTLY, VALIDATE CONSTRAINT)?
  |     --> does not block reads or writes. Safe; long wall-clock only.
  |
  |-- Held only for a metadata change, sub-millisecond, behind lock_timeout?
        --> acceptable. Confirm a lock_timeout guard exists. [M-8]
```

## The Review Checklist :

Run every check. Each has a trigger, an ALWAYS/NEVER rule, a severity, and a
cite-skill. Full rationale and safe rewrites are in references/methods.md and
references/examples.md.

### M-1 : CREATE INDEX without CONCURRENTLY

ALWAYS flag `CREATE INDEX` with no `CONCURRENTLY` on a table that takes writes.
NEVER wave it through because the table looks small.
Severity : CRITICAL. Cite : postgres-impl-zero-downtime-migrations.
WHY : a plain `CREATE INDEX` locks the table against inserts, updates and
deletes for the entire build; reads continue. Fix : `CREATE INDEX CONCURRENTLY`.

### M-2 : ALTER COLUMN TYPE that rewrites the table

ALWAYS flag `ALTER COLUMN ... TYPE` unless the change is binary-coercible with
no `USING` clause. NEVER assume the type change is free.
Severity : CRITICAL. Cite : postgres-impl-zero-downtime-migrations.
WHY : a type change rewrites the whole table and its indexes under
`ACCESS EXCLUSIVE`, blocking all traffic. Fix : expand-contract (add a new
column, backfill in chunks, swap, drop the old column).

### M-3 : ADD COLUMN with a volatile DEFAULT

ALWAYS flag `ADD COLUMN ... DEFAULT <volatile>` such as `DEFAULT now()` or
`DEFAULT random()`.
Severity : WARNING. Cite : postgres-impl-zero-downtime-migrations.
WHY : a non-volatile constant default is metadata-only since v11, but a
volatile default rewrites the entire table and its indexes. Fix : add the
column with no default, then backfill in chunks, then set the default.

### M-4 : ADD COLUMN NOT NULL without a DEFAULT on a populated table

ALWAYS flag `ADD COLUMN ... NOT NULL` with no `DEFAULT` when the table has rows.
Severity : CRITICAL. Cite : postgres-impl-zero-downtime-migrations.
WHY : every existing row would violate `NOT NULL`, so the statement fails. Fix :
add the column nullable, backfill in chunks, then add `NOT NULL` via M-5.

### M-5 : SET NOT NULL without the NOT VALID CHECK path

ALWAYS flag a bare `ALTER COLUMN ... SET NOT NULL` on a large table.
Severity : WARNING. Cite : postgres-impl-zero-downtime-migrations.
WHY : `SET NOT NULL` scans the whole table under `ACCESS EXCLUSIVE`. Fix : add
`CHECK (col IS NOT NULL) NOT VALID`, `VALIDATE` it (light lock), then
`SET NOT NULL` skips its scan because the proven CHECK already guarantees it.

### M-6 : ADD FOREIGN KEY or ADD CHECK without NOT VALID + VALIDATE

ALWAYS flag a new `FOREIGN KEY` or `CHECK` constraint added in one step on a
large table.
Severity : WARNING. Cite : postgres-impl-zero-downtime-migrations.
WHY : a one-step constraint scans every existing row to validate it while
holding a write-blocking lock. Fix : `ADD CONSTRAINT ... NOT VALID` (fast,
metadata only), then `VALIDATE CONSTRAINT` later (takes only
`SHARE UPDATE EXCLUSIVE`, does not block writes).

### M-7 : DROP COLUMN, RENAME COLUMN, or RENAME TABLE

ALWAYS flag a `DROP COLUMN`, `RENAME COLUMN`, or `RENAME TABLE`.
Severity : CRITICAL. Cite : postgres-impl-zero-downtime-migrations.
WHY : the old app code still references the old name and breaks the instant the
migration commits, before the new code is deployed. Fix : expand-contract,
sequence the schema change and the app deploy so neither side ever sees a name
the other does not.

### M-8 : No lock_timeout guard around the migration

ALWAYS flag a locking migration that does not set `lock_timeout` (and usually
`statement_timeout`).
Severity : WARNING. Cite : postgres-errors-statement-timeouts.
WHY : `lock_timeout` defaults to 0, wait forever. A migration that cannot get
its lock then queues behind one slow query and blocks ALL traffic to the table.
Fix : `SET lock_timeout = '5s'` (and a sane `statement_timeout`) so the
migration fails fast and is retried, instead of stalling the system.

### M-9 : Single large UPDATE or DELETE backfill

ALWAYS flag a backfill that updates or deletes a whole large table in one
statement.
Severity : WARNING. Cite : postgres-impl-zero-downtime-migrations.
WHY : one huge write holds row locks for minutes and bloats the table with dead
tuples. Fix : chunk it (`WHERE id BETWEEN ...` or `ctid` batches with a small
`LIMIT`), committing each batch.

### M-10 : DDL plus heavy data change in one transaction

ALWAYS flag a transaction that takes a DDL lock and then runs a long data
change before commit.
Severity : WARNING. Cite : postgres-impl-zero-downtime-migrations.
WHY : the `ACCESS EXCLUSIVE` lock from the DDL is held for the entire data
change, multiplying the outage window. Fix : commit the DDL first, then run the
data change in its own chunked transactions.

### M-11 : CONCURRENTLY inside a transaction block

ALWAYS flag `CREATE INDEX CONCURRENTLY`, `REINDEX ... CONCURRENTLY`, or
`DROP INDEX CONCURRENTLY` wrapped in `BEGIN`/`COMMIT` or run by a tool that
wraps each migration in a transaction.
Severity : CRITICAL. Cite : postgres-impl-zero-downtime-migrations.
WHY : `CONCURRENTLY` cannot run inside a transaction block; the migration
errors out. Fix : run it as its own statement, outside any transaction;
configure the migration tool to disable its transaction wrapper for that step.

### M-12 : No rollback or down path considered

ALWAYS note when a migration has no down / rollback path and no recovery plan.
Severity : INFO. Cite : postgres-impl-zero-downtime-migrations.
WHY : an irreversible migration that fails mid-deploy leaves the schema in an
unknown state. Fix : write a down path, or document explicitly why the change
is forward-only and how to recover.

### M-13 : TRUNCATE in a migration

ALWAYS flag any `TRUNCATE` in a migration.
Severity : CRITICAL. Cite : postgres-impl-zero-downtime-migrations.
WHY : `TRUNCATE` deletes every row irreversibly and is rarely what a schema
migration should do. Fix : confirm the data loss is intended in writing; if it
is a test-data reset, it does not belong in a production migration.

### M-14 : Index or constraint on a partitioned table

ALWAYS check whether an index or constraint targets a partitioned-table parent
or its individual partitions.
Severity : WARNING. Cite : postgres-impl-partitioning.
WHY : creating an index on the partitioned parent cascades to every partition
and a plain (non-concurrent) form locks them all. Fix : build the index
`CONCURRENTLY` on each partition first, then `CREATE INDEX ON ONLY` the parent
and `ATTACH` the partition indexes.

## Output Format :

Emit findings as a list, then exactly one verdict line. Per finding :

```
[SEVERITY] <check id> : <location, e.g. statement 3, line 12>
  Problem : <what is unsafe and what it blocks / rewrites / breaks>
  Fix     : <concrete safe rewrite>
  Cite    : <sibling skill>
```

End with one verdict :

```
Verdict : UNSAFE   (2 CRITICAL, 1 WARNING, 0 INFO)
```

See references/methods.md for the full report schema and references/examples.md
for complete worked reviews.

## Anti-Patterns :

(Reviewer anti-patterns. Full cause + symptom + fix in references/anti-patterns.md)

- Lock-blind approval : approving a migration without judging each lock
- Small-table fallacy : assuming few rows makes a lock free (it is concurrent
  traffic that matters, not row count)
- Deploy-order blindness : passing a DROP / RENAME without checking the app
  deploy sequence
- Severity inflation : grading a missing down path as CRITICAL, or a table
  rewrite as INFO
- Citing nothing : reporting a finding without the sibling skill that fixes it

## Reference Links :

- [references/methods.md](references/methods.md) : full checklist table, lock-level reference, severity rubric, report schema
- [references/examples.md](references/examples.md) : complete worked reviews, unsafe-to-safe rewrites per check, version-annotated
- [references/anti-patterns.md](references/anti-patterns.md) : reviewer anti-patterns with cause + symptom + fix

## See Also :

- postgres-agents-query-reviewer : reviews query correctness and performance
- postgres-impl-zero-downtime-migrations : the online-safe DDL primitives this skill checks for
- postgres-errors-statement-timeouts : lock_timeout and statement_timeout guards
- postgres-impl-partitioning : index and constraint handling on partitioned tables
- Vooronderzoek section : §16 (Zero-Downtime Migrations), §19 (Anti-Patterns)
- Official docs : https://www.postgresql.org/docs/17/sql-altertable.html
- Official docs : https://www.postgresql.org/docs/17/sql-createindex.html
