# postgres-agents-migration-reviewer : methods

The full checklist table, lock-level reference, severity rubric, and report
schema. Verified against PostgreSQL 17 documentation (sql-altertable.html,
sql-createindex.html, transaction-iso.html).

## Checklist table :

| ID | Trigger | Severity | Cite |
|----|---------|----------|------|
| M-1 | `CREATE INDEX` without `CONCURRENTLY` on a written table | CRITICAL | postgres-impl-zero-downtime-migrations |
| M-2 | `ALTER COLUMN TYPE` that rewrites the table | CRITICAL | postgres-impl-zero-downtime-migrations |
| M-3 | `ADD COLUMN` with a volatile `DEFAULT` | WARNING | postgres-impl-zero-downtime-migrations |
| M-4 | `ADD COLUMN NOT NULL` with no `DEFAULT`, populated table | CRITICAL | postgres-impl-zero-downtime-migrations |
| M-5 | bare `SET NOT NULL` on a large table | WARNING | postgres-impl-zero-downtime-migrations |
| M-6 | `ADD FOREIGN KEY` / `ADD CHECK` without `NOT VALID` + `VALIDATE` | WARNING | postgres-impl-zero-downtime-migrations |
| M-7 | `DROP COLUMN` / `RENAME COLUMN` / `RENAME TABLE` | CRITICAL | postgres-impl-zero-downtime-migrations |
| M-8 | no `lock_timeout` guard around locking DDL | WARNING | postgres-errors-statement-timeouts |
| M-9 | single large `UPDATE` / `DELETE` backfill | WARNING | postgres-impl-zero-downtime-migrations |
| M-10 | DDL plus heavy data change in one transaction | WARNING | postgres-impl-zero-downtime-migrations |
| M-11 | `CONCURRENTLY` inside a transaction block | CRITICAL | postgres-impl-zero-downtime-migrations |
| M-12 | no rollback / down path | INFO | postgres-impl-zero-downtime-migrations |
| M-13 | `TRUNCATE` in a migration | CRITICAL | postgres-impl-zero-downtime-migrations |
| M-14 | index / constraint on a partitioned table | WARNING | postgres-impl-partitioning |

## Lock-level reference :

The skill grades locks, so the reviewer must know what each lock blocks. From
sql-altertable.html and sql-createindex.html :

| Operation | Lock | Blocks | Notes |
|-----------|------|--------|-------|
| plain `CREATE INDEX` | write-blocking table lock | INSERT/UPDATE/DELETE | reads continue; held for the whole single-scan build |
| `CREATE INDEX CONCURRENTLY` | `SHARE UPDATE EXCLUSIVE` | nothing | two scans, longer wall-clock, no read/write block |
| `ALTER TABLE` (default) | `ACCESS EXCLUSIVE` | reads AND writes | the strongest lock; default unless a subform is noted weaker |
| `ALTER COLUMN TYPE` | `ACCESS EXCLUSIVE` | reads AND writes | rewrites the table unless binary-coercible with no `USING` |
| `ADD COLUMN` (non-volatile DEFAULT) | `ACCESS EXCLUSIVE` | reads AND writes briefly | metadata only since v11, no rewrite |
| `ADD COLUMN` (volatile DEFAULT) | `ACCESS EXCLUSIVE` | reads AND writes | full table + index rewrite |
| `SET NOT NULL` | `ACCESS EXCLUSIVE` | reads AND writes | scans the table; scan skipped if a valid CHECK proves no NULL |
| `ADD CONSTRAINT ... NOT VALID` | `ACCESS EXCLUSIVE` | reads AND writes briefly | no scan, commits immediately |
| `VALIDATE CONSTRAINT` | `SHARE UPDATE EXCLUSIVE` | nothing | scans pre-existing rows without blocking writes |
| `ADD FOREIGN KEY` (one step) | `SHARE ROW EXCLUSIVE` | writes | scans existing rows to validate the FK |

Exact wording from sql-createindex.html on the plain build : "Other
transactions can still read the table, but if they try to insert, update, or
delete rows in the table they will block until the index build is finished."

Exact wording on the transaction restriction : "a regular `CREATE INDEX`
command can be performed within a transaction block, but `CREATE INDEX
CONCURRENTLY` cannot."

## Rewrite vs no-rewrite (sql-altertable.html) :

- No rewrite : `ADD COLUMN` with no `DEFAULT` or a non-volatile constant
  `DEFAULT` (v11+, the default is stored in catalog metadata).
- No rewrite : `ALTER COLUMN TYPE` when the `USING` clause does not change
  contents and the old type is binary-coercible to the new type (for example
  `text` to `varchar`). Indexes still rebuild unless logically equivalent.
- Full rewrite : `ADD COLUMN` with a volatile `DEFAULT`; any other
  `ALTER COLUMN TYPE`. The whole table and all its indexes are rewritten under
  `ACCESS EXCLUSIVE`.

## Failed CONCURRENTLY build :

A failed `CREATE INDEX CONCURRENTLY` (deadlock, uniqueness violation) leaves an
`INVALID` index that is ignored for queries but still adds update overhead. The
reviewer should note: recovery is `DROP INDEX` (use `DROP INDEX CONCURRENTLY`),
then retry, or `REINDEX INDEX CONCURRENTLY`.

## Severity rubric :

| Grade | Definition | Verdict effect |
|-------|------------|----------------|
| CRITICAL | blocks production traffic, rewrites a table, loses data, breaks the running app, or errors out | verdict becomes UNSAFE |
| WARNING | safe in principle but a foot-gun at scale or under concurrent load | verdict becomes NEEDS-CHANGES (if no CRITICAL) |
| INFO | process / hygiene gap; the author decides | no verdict effect |

Verdict mapping :
- `SAFE` : zero CRITICAL, zero WARNING.
- `NEEDS-CHANGES` : at least one WARNING, zero CRITICAL.
- `UNSAFE` : at least one CRITICAL.

INFO findings are always reported but never change the verdict.

## Report schema :

The review output has two parts, findings then verdict.

Per finding :

```
[SEVERITY] <check id> : <location>
  Problem : <what is unsafe; what it blocks, rewrites, or breaks>
  Fix     : <concrete safe rewrite, an actual statement where possible>
  Cite    : <sibling skill name>
```

`<location>` identifies the statement: a statement number, a line range, or the
verbatim offending SQL fragment. The verdict line is exactly one line :

```
Verdict : <SAFE | NEEDS-CHANGES | UNSAFE>   (<n> CRITICAL, <n> WARNING, <n> INFO)
```

If no check fires :

```
Verdict : SAFE   (0 CRITICAL, 0 WARNING, 0 INFO)
```

## Rehearsal recommendation :

For any CRITICAL or WARNING about locking or rewrites, the reviewer ALWAYS adds
one line recommending a rehearsal: run the migration against a copy of the
table at production row count, while a synthetic concurrent write load runs,
and measure how long the lock is actually held. A migration is not "safe"
because it is short SQL; it is safe because its lock window was measured.
