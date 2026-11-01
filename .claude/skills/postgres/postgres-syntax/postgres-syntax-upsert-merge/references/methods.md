# UPSERT and MERGE Methods Reference

Full syntax reference for `INSERT ... ON CONFLICT` and `MERGE` in PostgreSQL 15, 16, and 17. Every form verified against `postgresql.org/docs/17/sql-insert.html`, `postgresql.org/docs/17/sql-merge.html`, and `postgresql.org/docs/release/17.0/` on 2026-05-19.

## INSERT ... ON CONFLICT Full Syntax :

```sql
[ WITH [ RECURSIVE ] cte_name [(col, ...)] AS ( ... ) ]
INSERT INTO table_name [ AS alias ] [ ( column_name [, ...] ) ]
  { DEFAULT VALUES | VALUES (expr [, ...]) [, ...] | query }
[ ON CONFLICT [ conflict_target ] conflict_action ]
[ RETURNING * | output_expression [ [ AS ] output_name ] [, ...] ];
```

Where :

```sql
conflict_target := ( column_name [, ...] ) [ WHERE index_predicate ]
                 | ON CONSTRAINT constraint_name

conflict_action := DO NOTHING
                 | DO UPDATE SET { column_name = expr [, ...]
                                 | (column_name [, ...]) = (expr [, ...])
                                 | (column_name [, ...]) = (sub-SELECT) }
                                [, ...]
                   [ WHERE condition ]
```

### Conflict Target Rules :

- The conflict target MUST match an existing UNIQUE or PRIMARY KEY index that is NOT DEFERRABLE.
- Column list (`(col1, col2)`) : PostgreSQL searches for a unique index whose columns match exactly (order-independent, but every column must be covered).
- Constraint name (`ON CONSTRAINT pkey_name`) : binds to a specific named constraint. Required when multiple unique constraints exist on the same column set.
- Partial unique indexes (`CREATE UNIQUE INDEX ... WHERE pred`) : the conflict target MUST repeat the predicate : `ON CONFLICT (col) WHERE pred`. PostgreSQL matches the predicate string.
- Exclusion constraints (EXCLUDE USING GIST ...) are NOT valid arbiters. ON CONFLICT works only with UNIQUE/PK.
- Expressions in unique indexes : `CREATE UNIQUE INDEX ON t (lower(email))` → `ON CONFLICT (lower(email))`.

### EXCLUDED Pseudo-Table :

`EXCLUDED` is bound to the values proposed for INSERT inside `DO UPDATE` :

```sql
INSERT INTO t (id, count, last_seen) VALUES (1, 1, now())
ON CONFLICT (id) DO UPDATE
SET count     = t.count + EXCLUDED.count,      -- existing + proposed
    last_seen = GREATEST(t.last_seen, EXCLUDED.last_seen);
```

- `EXCLUDED.col` : the value the INSERT would have written.
- `target_table.col` (or alias) : the existing row in storage.
- Both are available in the SET-list and the optional `WHERE condition`.
- The WHERE condition runs AFTER the row would otherwise be updated; rows where it evaluates FALSE are skipped (treated like DO NOTHING).

### RETURNING Semantics :

- Returns one row per actually-affected row (insert or update). DO NOTHING no-ops do NOT appear in RETURNING.
- Expressions can reference the target table (post-update state) and EXCLUDED is NOT accessible in RETURNING.
- Useful for emitting events : `RETURNING id, (xmax = 0) AS was_inserted` distinguishes insert (xmax=0) from update.

## MERGE Full Syntax :

```sql
[ WITH cte_name AS ( ... ) ]
MERGE INTO [ ONLY ] target_table_name [ * ] [ [ AS ] target_alias ]
USING { [ ONLY ] source_table_name [ * ] | (source_query) | (VALUES ...) } [ [ AS ] source_alias ]
ON merge_condition
when_clause [ when_clause ... ]
[ RETURNING { * | output_expression [ [ AS ] output_name ] } [, ...] ]   -- v17+
;
```

Where each `when_clause` is one of :

```sql
WHEN MATCHED [ AND extra_condition ] THEN merge_when_matched
WHEN NOT MATCHED [ AND extra_condition ] THEN merge_when_not_matched
WHEN NOT MATCHED BY SOURCE [ AND extra_condition ] THEN merge_when_matched   -- v17+
```

And :

```sql
merge_when_matched := UPDATE SET { col = expr | col = DEFAULT } [, ...]
                    | DELETE
                    | DO NOTHING

merge_when_not_matched := INSERT [( cols )] { VALUES ( exprs ) | OVERRIDING { SYSTEM | USER } VALUE VALUES (...) }
                        | DO NOTHING
```

### Action Resolution :

- WHEN clauses are evaluated TOP-TO-BOTTOM per source row. First match wins. Order specific cases before catch-alls.
- A source row produces AT MOST ONE action against a target row. The ON condition + WHEN ordering must guarantee this.
- If no WHEN clause matches, the source row is silently ignored.

### Cardinality Constraint :

The `ON merge_condition` MUST produce AT MOST ONE source row per target row. Otherwise PostgreSQL raises :

```
ERROR:  MERGE command cannot affect row a second time
HINT:   Ensure that not more than one source row matches any one target row.
SQLSTATE 21000
```

This is the most common MERGE bug. Always pre-aggregate or `DISTINCT ON` the source.

### merge_action() Function (v17+) :

Returns a text value `'INSERT'`, `'UPDATE'`, or `'DELETE'` per RETURNING row. ONLY callable inside `MERGE ... RETURNING`. Using it elsewhere raises `0A000 feature not supported`.

```sql
MERGE INTO target t USING source s ON t.id = s.id
WHEN MATCHED THEN UPDATE SET val = s.val
WHEN NOT MATCHED THEN INSERT VALUES (s.id, s.val)
RETURNING merge_action() AS action, t.*;
```

### Restrictions :

- MERGE target CANNOT be a materialized view (use REFRESH MATERIALIZED VIEW instead).
- MERGE target CANNOT be a foreign table.
- MERGE target CAN be an updatable view via RULES (v17+) — earlier versions raise `0A000`.
- MERGE CANNOT use WITH RECURSIVE in the source.
- MERGE inside a function with `STABLE` or `IMMUTABLE` volatility category : not supported. Use `VOLATILE`.

## ON CONFLICT vs MERGE Cheat Sheet :

| Property | INSERT ... ON CONFLICT | MERGE |
|---|---|---|
| First available | v9.5 | v15 |
| Conflict targets | one (UNIQUE/PK index) | arbitrary join condition |
| Actions per call | 1 (DO NOTHING or DO UPDATE) | many (UPDATE / DELETE / INSERT / DO NOTHING per WHEN) |
| Source | VALUES or SELECT inline | any relation, subquery, VALUES |
| RETURNING | yes (all versions) | v17+ only |
| Performance | optimized hot-path (single index probe) | general join + per-row branch |
| DELETE rows missing from source | no | yes, via `WHEN NOT MATCHED BY SOURCE THEN DELETE` (v17+) |
| Atomicity | single statement, lock target row | single statement, lock target rows in scan order |

## Concurrency Notes :

- `INSERT ... ON CONFLICT DO UPDATE` is atomic per row and serializable-safe : the conflict-resolution path uses `ExclusiveLock` on the existing tuple, no read-after-write race.
- Concurrent UPSERTs on the same key serialize at the row lock; readers see either the pre-update or post-update version per MVCC.
- MERGE acquires row locks in scan order; high-concurrency MERGE workloads can deadlock if multiple concurrent MERGEs touch overlapping target rows in different orders. Mitigation : enforce a fixed scan order via `ORDER BY` in the source, or serialize MERGEs via advisory lock.

## DEFAULT Keyword In UPDATE / INSERT :

Both UPSERT and MERGE accept `DEFAULT` in SET-lists and VALUES :

```sql
INSERT INTO t (id, version) VALUES (1, 1)
ON CONFLICT (id) DO UPDATE SET version = DEFAULT;   -- reset to column default

MERGE INTO t USING src ON t.id = src.id
WHEN MATCHED THEN UPDATE SET created_at = DEFAULT;   -- reset to DEFAULT now()
```

For columns with `GENERATED ALWAYS AS IDENTITY`, only `DEFAULT` is valid in INSERT (other values raise an error unless `OVERRIDING SYSTEM VALUE` is used).
