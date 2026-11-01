# postgres-syntax-window-functions : Methods Reference

## OVER Clause : Anatomy

```sql
function ( [ expression [, ...] ] [ ORDER BY ... ] ) [ FILTER ( WHERE filter_clause ) ]
    OVER ( window_definition | window_name )
```

`window_definition` is :

```sql
[ existing_window_name ]
[ PARTITION BY expression [, ...] ]
[ ORDER BY expression [ ASC | DESC | USING operator ]
                     [ NULLS { FIRST | LAST } ] [, ...] ]
[ frame_clause ]
```

Component meanings :

| Clause | Effect |
|---|---|
| `PARTITION BY` | Resets the window per group. Without it, the whole row set is one partition. |
| `ORDER BY` (inside OVER) | Defines row sequence within partition. Required for `RANK`, `DENSE_RANK`, `PERCENT_RANK`, `CUME_DIST`. Without it, ranking functions are non-deterministic. |
| Frame clause | Specifies which subset of the partition the function sees per current row. |

Where window functions are allowed : `SELECT` list, outer `ORDER BY` of the same query level. Forbidden in `WHERE`, `GROUP BY`, `HAVING` , wrap in a subquery / CTE and filter on the column. Source : postgresql.org/docs/17/sql-expressions.html#SYNTAX-WINDOW-FUNCTIONS.

## Frame Clause Grammar

```sql
{ RANGE | ROWS | GROUPS } frame_start [ frame_exclusion ]
{ RANGE | ROWS | GROUPS } BETWEEN frame_start AND frame_end [ frame_exclusion ]
```

`frame_start` / `frame_end` values :

| Bound | Meaning |
|---|---|
| `UNBOUNDED PRECEDING` | Start of partition. |
| `offset PRECEDING` | `offset` units before current row. |
| `CURRENT ROW` | The current row (or its peer group in RANGE/GROUPS mode). |
| `offset FOLLOWING` | `offset` units after current row. |
| `UNBOUNDED FOLLOWING` | End of partition. |

Restrictions :
- `frame_start` cannot be `UNBOUNDED FOLLOWING`.
- `frame_end` cannot be `UNBOUNDED PRECEDING`.
- `frame_end` cannot lie before `frame_start`.

### Frame modes

| Mode | What `offset` means | Requires ORDER BY |
|---|---|---|
| `ROWS` | A non-negative integer count of physical rows. | No (without it the frame is the partition) |
| `RANGE` | A value offset on the (single) ORDER BY column. For dates/timestamps use `INTERVAL`. | Effectively yes for non-trivial use. |
| `GROUPS` (v11+) | A non-negative integer count of **peer groups**. | Yes. |

### EXCLUDE (v11+)

| Clause | Meaning |
|---|---|
| `EXCLUDE NO OTHERS` | Default. No exclusion. |
| `EXCLUDE CURRENT ROW` | Drop the current row from the frame. |
| `EXCLUDE GROUP` | Drop the current row and all its ORDER BY peers. |
| `EXCLUDE TIES` | Drop the current row's peers but KEEP the current row. |

Source : postgresql.org/docs/17/sql-expressions.html#SYNTAX-WINDOW-FUNCTIONS.

## Default Frame

- `OVER ()` with no ORDER BY : frame = the entire partition (every row is a peer of every other row).
- `OVER (ORDER BY x)` with no explicit frame : `RANGE BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW`. In RANGE mode `CURRENT ROW` means "the current row and all its peers" , i.e. all rows with the same ORDER BY value. This is the source of the LAST_VALUE-returns-current-row foot-gun.

To get a fully inclusive frame for `LAST_VALUE` or aggregate-as-window over the whole partition with row-sensitive behavior, write the frame explicitly :

```sql
ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING
```

## Named WINDOW Clause

The `WINDOW` clause sits between `HAVING` and `ORDER BY`. Syntax :

```sql
SELECT ...
FROM ...
[ WHERE ... ]
[ GROUP BY ... ]
[ HAVING ... ]
WINDOW window_name AS ( window_definition ) [, ... ]
[ ORDER BY ... ]
```

A named window can inherit another named window's PARTITION BY and ORDER BY (but not its frame) :

```sql
WINDOW base AS (PARTITION BY dep ORDER BY hire_date),
       w_running AS (base ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW),
       w_full    AS (base ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING)
```

Source : postgresql.org/docs/17/sql-select.html.

## Window-Function Reference Table

| Function | Args | Return | Requires ORDER BY in OVER | Frame-sensitive |
|---|---|---|---|---|
| `row_number()` | , | `bigint` | No (but result is non-deterministic without it) | No (ignores frame) |
| `rank()` | , | `bigint` | Yes | No |
| `dense_rank()` | , | `bigint` | Yes | No |
| `percent_rank()` | , | `double precision` | Yes | No |
| `cume_dist()` | , | `double precision` | Yes | No |
| `ntile(n integer)` | n | `integer` | No (non-deterministic without it) | No |
| `lag(value [, offset integer [, default ]])` | value, optional offset (default 1), optional default (default NULL) | same as value | No (recommended) | No (uses ORDER BY directly) |
| `lead(value [, offset integer [, default ]])` | value, optional offset, optional default | same as value | No (recommended) | No |
| `first_value(value)` | value | same as value | No (recommended) | **YES** |
| `last_value(value)` | value | same as value | No (recommended) | **YES** , default frame gives "current row" |
| `nth_value(value, n integer)` | value, n | same as value | No (recommended) | **YES** |

Any built-in or user-defined ordinary aggregate (`sum`, `avg`, `count`, `min`, `max`, `array_agg`, `string_agg`, ...) becomes a window function when followed by `OVER (...)`. Ordered-set aggregates (`percentile_cont`, `percentile_disc`, `mode`) and hypothetical-set aggregates do NOT support `OVER`. Source : postgresql.org/docs/17/functions-window.html, postgresql.org/docs/17/functions-aggregate.html.

### Ranking semantics

| Function | Ties handled by | Example (values 10, 20, 20, 30) |
|---|---|---|
| `row_number()` | Arbitrary tiebreaker (deterministic only if ORDER BY breaks ties) | 1, 2, 3, 4 |
| `rank()` | Same rank, leaves gap | 1, 2, 2, 4 |
| `dense_rank()` | Same rank, no gap | 1, 2, 2, 3 |
| `percent_rank()` | `(rank - 1) / (total - 1)` | 0.000, 0.333, 0.333, 1.000 |
| `cume_dist()` | `rows_with_value_<=_current / total` | 0.25, 0.75, 0.75, 1.00 |
| `ntile(4)` | Splits partition into 4 equal-size buckets | 1, 2, 3, 4 |

### NULL handling

PostgreSQL `LAG`, `LEAD`, `FIRST_VALUE`, `LAST_VALUE`, `NTH_VALUE` ALL use `RESPECT NULLS` semantics. The SQL standard `IGNORE NULLS` option is **not implemented**. To skip NULLs : pre-filter the input with a CTE / subquery and ROW_NUMBER over the non-NULL rows. Source : postgresql.org/docs/17/functions-window.html.

### `nth_value` direction

PostgreSQL implements only `FROM FIRST` semantics for `nth_value(col, n)`. To get the n-th-from-last value, reverse the ORDER BY :

```sql
nth_value(col, n) OVER (ORDER BY ts DESC)
```

## Logical Evaluation Order (why WHERE cannot see window results)

```
FROM / JOIN
WHERE
GROUP BY
HAVING
WINDOW         <-- window functions evaluated here
SELECT
DISTINCT
ORDER BY
LIMIT / OFFSET
```

This is why you cannot write `WHERE row_number() OVER (...) <= 3`. Wrap the SELECT and filter the outer SELECT :

```sql
SELECT * FROM (
  SELECT col, row_number() OVER (PARTITION BY g ORDER BY x DESC) AS rn FROM t
) s
WHERE s.rn <= 3;
```

`DISTINCT ON (...)` is a PostgreSQL extension that often replaces top-1-per-group queries without a window function ; see `postgres-syntax-cte-lateral`. Source : postgresql.org/docs/17/sql-select.html.
