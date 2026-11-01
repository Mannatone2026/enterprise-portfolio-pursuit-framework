---
name: postgres-syntax-window-functions
description: >
  Use when computing running totals, rank within partition, top-N-per-group via window, or comparing rows to neighbours.
  Prevents LAST_VALUE returning current row (wrong default frame), filtering on window result in WHERE (illegal, must wrap), and reaching for GROUP BY when window suffices.
  Covers OVER clause, PARTITION BY, frame clauses (ROWS/RANGE/GROUPS v11+), EXCLUDE (v11+), named windows, ranking (RANK/DENSE_RANK/ROW_NUMBER/NTILE), offset (LAG/LEAD/FIRST_VALUE/LAST_VALUE/NTH_VALUE), window vs aggregate decision tree.
  Keywords: window function, OVER, PARTITION BY, frame clause, ROWS BETWEEN, RANGE BETWEEN, GROUPS, LAG, LEAD, RANK, DENSE_RANK, ROW_NUMBER, NTILE, named window, running total, top N per group, why is LAST_VALUE returning current row, can I use window in WHERE
license: MIT
compatibility: "Designed for Claude Code. Requires PostgreSQL 15, 16, or 17."
metadata:
  author: OpenAEC-Foundation
  version: "1.0"
---

# postgres-syntax-window-functions

## Quick Reference :

A window function computes a value across a set of rows that are related to the current row, **without collapsing** the result set the way `GROUP BY` does. The shape is always `function(args) OVER (PARTITION BY ... ORDER BY ... frame_clause)`. `PARTITION BY` resets the window per group, `ORDER BY` (inside the OVER clause) defines the row sequence within a partition, and the frame clause picks the subset of the partition the function sees. Window functions are legal only in the `SELECT` list and the outer `ORDER BY` , never in `WHERE`, `GROUP BY`, or `HAVING` (wrap in a subquery or CTE to filter on a window result).

PostgreSQL ships three frame modes : `ROWS` (counts physical rows), `RANGE` (value-based window using the ORDER BY column), and `GROUPS` (v11+, counts peer groups). When you write `OVER (ORDER BY x)` without an explicit frame, the default is `RANGE BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW` , meaning the frame ends at the **last peer of the current row**, not at the current row itself. This is the root cause of the canonical "why is LAST_VALUE returning the current row" foot-gun : the answer is to write `ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING` explicitly. v11 added `EXCLUDE CURRENT ROW / GROUP / TIES / NO OTHERS` to trim the frame further.

## When To Use This Skill :

ALWAYS use this skill when :
- Computing running totals, moving averages, or other accumulating aggregates per row
- Assigning rank, dense_rank, row_number within a group ("top N per category")
- Comparing each row to the previous / next / first / last row (LAG, LEAD, FIRST_VALUE, LAST_VALUE)
- Bucketing rows into N quantiles with NTILE
- A query is using `GROUP BY` and a self-join to compute "rank within group" , a window function eliminates the self-join

NEVER use this skill for :
- Plain group aggregates with no per-row output : use `GROUP BY` directly
- Recursive hierarchy traversal or graph traversal : see `postgres-syntax-cte-lateral`
- Top-N-per-group via correlated subquery using LATERAL : see `postgres-syntax-cte-lateral`
- `FILTER (WHERE ...)` per-row conditional aggregation (works with both group and window) : see `postgres-syntax-aggregates`

## Decision Trees :

### Window function or GROUP BY? :

```
Need a value computed across many rows?
├── The result must keep every input row visible : WINDOW FUNCTION
│   SELECT col, sum(amount) OVER (PARTITION BY cat) FROM t;
├── The result should collapse to one row per group : GROUP BY
│   SELECT cat, sum(amount) FROM t GROUP BY cat;
└── Need BOTH a per-row computation AND a per-group aggregate :
    use a window function ; group aggregates can be re-derived as
    sum(...) OVER (PARTITION BY ...) without collapsing rows.
```

### Pick the right frame mode :

```
What does "the window" mean physically?
├── Exactly N rows before / after current row : ROWS
│   ORDER BY ts ROWS BETWEEN 7 PRECEDING AND CURRENT ROW   -- last 8 rows
├── A value range on the ORDER BY column (e.g. last 7 days) : RANGE
│   ORDER BY ts RANGE BETWEEN INTERVAL '7 days' PRECEDING AND CURRENT ROW
└── N peer groups before / after (ties count once) : GROUPS (v11+)
    ORDER BY rank GROUPS BETWEEN 1 PRECEDING AND CURRENT ROW
```

### Picking the default vs explicit frame :

```
Function called?
├── row_number / rank / dense_rank / percent_rank / cume_dist / ntile :
│       Frame IGNORED. Only OVER (PARTITION BY ... ORDER BY ...) matters.
├── lag / lead :
│       Frame ignored in practice ; uses ORDER BY directly.
├── first_value / last_value / nth_value :
│       FRAME-SENSITIVE. Default frame ends at "last peer of current row" ,
│       LAST_VALUE on default frame returns the CURRENT row's last peer.
│       Use ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING
│       for "last in partition".
└── aggregate-as-window (sum, avg, count, ...) :
        Frame-sensitive. Default is RANGE UNBOUNDED PRECEDING ...
        CURRENT ROW : produces a running total when ORDER BY is set.
        Without ORDER BY : the frame is the whole partition.
```

## Patterns :

### Pattern 1 : Running total over time

ALWAYS specify `ORDER BY` inside `OVER (...)` to get a running total ; without it the frame is the full partition.
NEVER add an outer `ORDER BY` and assume the running sum follows ; the window frame is independent of presentation order.

```sql
-- Running total of payments per customer, ordered by date
SELECT customer_id,
       paid_at,
       amount,
       sum(amount) OVER (
           PARTITION BY customer_id
           ORDER BY paid_at
           ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
       ) AS running_total
FROM payments
ORDER BY customer_id, paid_at;
```

WHY : the frame `ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW` is explicit and unambiguous , every row sees exactly the rows that came before it plus itself. The default `RANGE` frame produces the same result when `paid_at` is unique, but with duplicate timestamps it lumps peers together. Source : postgresql.org/docs/17/sql-expressions.html#SYNTAX-WINDOW-FUNCTIONS.

### Pattern 2 : Top-N-per-group via ROW_NUMBER

ALWAYS use `ROW_NUMBER() OVER (PARTITION BY group_key ORDER BY rank_key DESC)` and filter `WHERE rn <= N` in a wrapping subquery.
NEVER attempt to filter the window result directly in `WHERE` , window functions execute AFTER `WHERE` ; SQLSTATE `42883` `window function is not allowed in WHERE`.

```sql
-- Top 3 highest-paid employee per department
SELECT department_id, employee_id, salary, rn
FROM (
  SELECT department_id, employee_id, salary,
         row_number() OVER (PARTITION BY department_id ORDER BY salary DESC) AS rn
  FROM employees
) ranked
WHERE rn <= 3
ORDER BY department_id, rn;
```

WHY : SQL's logical evaluation order processes `WHERE` BEFORE the `SELECT` (and therefore before window functions). The subquery materializes the rank column ; the outer `WHERE` filters on it. For ties on `salary` use `RANK()` (ties get the same rank with gaps) or `DENSE_RANK()` (ties get the same rank without gaps) depending on whether you want exactly 3 rows or up-to-N-rows-including-ties. Source : postgresql.org/docs/17/functions-window.html.

### Pattern 3 : Compare each row to the previous and next

ALWAYS use `LAG(col, n, default)` / `LEAD(col, n, default)` for off-by-N row comparison.
NEVER use a self-join on `t1.id = t2.id - 1` , it breaks on missing ids and produces a wrong join cardinality.

```sql
-- Day-over-day revenue delta per region
SELECT region,
       day,
       revenue,
       revenue - lag(revenue, 1, 0::numeric)
                 OVER (PARTITION BY region ORDER BY day) AS delta_vs_prev,
       lead(revenue, 1)
                 OVER (PARTITION BY region ORDER BY day) AS next_day_revenue
FROM daily_revenue
ORDER BY region, day;
```

WHY : `LAG` and `LEAD` use the OVER clause's `ORDER BY` to navigate ; they do not depend on the physical row order. The third argument is the default returned at the partition boundary (here `0::numeric` so the first day's `delta_vs_prev` is `revenue - 0`, not `NULL`). Source : postgresql.org/docs/17/functions-window.html.

### Pattern 4 : LAST_VALUE without the default-frame trap

ALWAYS write an explicit frame `ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING` when you want LAST_VALUE to mean "last row in the partition".
NEVER call `LAST_VALUE(col) OVER (ORDER BY ts)` with no frame , the default frame ends at the current row's last peer, so LAST_VALUE returns the current row's value when ts is unique.

```sql
-- Correct : "last paid_at per customer" alongside every row
SELECT customer_id,
       paid_at,
       amount,
       last_value(paid_at) OVER (
           PARTITION BY customer_id
           ORDER BY paid_at
           ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING
       ) AS last_paid_at
FROM payments;

-- WRONG : default frame returns paid_at itself
SELECT customer_id,
       paid_at,
       last_value(paid_at) OVER (PARTITION BY customer_id ORDER BY paid_at)
           AS broken_last_paid_at        -- equals paid_at on every row
FROM payments;
```

WHY : `OVER (ORDER BY ts)` with no explicit frame defaults to `RANGE UNBOUNDED PRECEDING` , equivalent to `RANGE BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW`. The frame end is the **last peer of the current row**, which for a unique `ORDER BY` value is the current row. Source : postgresql.org/docs/17/sql-expressions.html#SYNTAX-WINDOW-FUNCTIONS, postgresql.org/docs/17/functions-window.html.

### Pattern 5 : Named windows for repeated OVER clauses

ALWAYS factor a repeated OVER clause into a named `WINDOW` definition.
NEVER copy-paste an OVER clause across multiple SELECT-list columns , a typo desyncs the windows.

```sql
SELECT customer_id,
       paid_at,
       amount,
       sum(amount)            OVER w AS running_total,
       avg(amount)            OVER w AS running_avg,
       count(*)               OVER w AS running_count,
       row_number()           OVER w AS rn,
       first_value(paid_at)   OVER w AS first_paid_at
FROM payments
WINDOW w AS (
    PARTITION BY customer_id
    ORDER BY paid_at
    ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
);
```

WHY : the `WINDOW w AS (...)` clause sits between `HAVING` and `ORDER BY`. Multiple OVER references can name `w` , the plan reuses one window scan instead of one per OVER. A window definition can also inherit from another : `WINDOW base AS (PARTITION BY ...), w AS (base ORDER BY ...)`. Source : postgresql.org/docs/17/sql-select.html.

### Pattern 6 : Moving N-row average with the ROWS frame

ALWAYS use `ROWS BETWEEN n PRECEDING AND CURRENT ROW` for "last N data points" averaging.
NEVER use `RANGE BETWEEN n PRECEDING AND CURRENT ROW` for row-count windows , RANGE counts a **value range** on the ORDER BY column, not row positions.

```sql
-- Trailing 7-day moving average (row-count window) :
SELECT sensor_id,
       ts,
       reading,
       avg(reading) OVER (
           PARTITION BY sensor_id
           ORDER BY ts
           ROWS BETWEEN 6 PRECEDING AND CURRENT ROW         -- 7 rows total
       ) AS ma_7row
FROM sensor_readings;

-- Trailing 7-day moving average (value-range window) :
SELECT sensor_id,
       ts,
       reading,
       avg(reading) OVER (
           PARTITION BY sensor_id
           ORDER BY ts
           RANGE BETWEEN INTERVAL '6 days' PRECEDING AND CURRENT ROW
       ) AS ma_7day
FROM sensor_readings;
```

WHY : the ROWS variant captures exactly the last 7 rows regardless of timestamp gaps. The RANGE variant captures every row whose `ts` is within the last 7 days , the row count varies if the timestamps are uneven. Pick the one that matches the business definition. Source : postgresql.org/docs/17/sql-expressions.html#SYNTAX-WINDOW-FUNCTIONS.

### Pattern 7 : EXCLUDE CURRENT ROW / GROUP / TIES (v11+)

ALWAYS reach for `EXCLUDE` when "the average of my neighbours, not including me" is the question.
NEVER manually subtract the current row's value , `EXCLUDE` is correct in the presence of NULLs and on the partition boundary.

```sql
-- 7-row moving average of the readings EXCLUDING the current row
SELECT sensor_id, ts, reading,
       avg(reading) OVER (
           PARTITION BY sensor_id
           ORDER BY ts
           ROWS BETWEEN 3 PRECEDING AND 3 FOLLOWING
           EXCLUDE CURRENT ROW
       ) AS neighbour_avg
FROM sensor_readings;
```

`EXCLUDE` modes : `NO OTHERS` (default), `CURRENT ROW`, `GROUP` (current row plus its ORDER BY peers), `TIES` (peers but keep current row). Source : postgresql.org/docs/17/sql-expressions.html#SYNTAX-WINDOW-FUNCTIONS.

## Anti-Patterns :

(One-liners ; full diagnosis + fix in `references/anti-patterns.md`.)

- `LAST_VALUE(col) OVER (ORDER BY ts)` without explicit frame , returns the current row, not the partition's last. Use `ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING`.
- Window function inside `WHERE` / `GROUP BY` / `HAVING` : SQLSTATE `42883` / `42P20`. Wrap in a subquery or CTE and filter on the named column.
- `ROW_NUMBER()` without `ORDER BY` inside `OVER` , row order is non-deterministic ; the row-numbers are arbitrary on every run.
- Using `RANK()` when you wanted `DENSE_RANK()` , `RANK` produces gaps on ties (1, 2, 2, 4) ; `DENSE_RANK` does not (1, 2, 2, 3).
- Using `NTILE(n)` without `ORDER BY` , buckets are assigned in physical order, which is non-deterministic.
- Self-joining `t1.id = t2.id - 1` to look at the previous row , breaks on missing ids. Use `LAG` over `ORDER BY id`.
- Computing a "running total" with a correlated subquery : `(SELECT sum(...) FROM t s WHERE s.id <= t.id)` , O(n^2) ; window function is O(n).
- Filtering NULLs out of a `LAG`/`LEAD` chain after the fact , PostgreSQL has no `IGNORE NULLS` ; pre-filter with a CTE.
- Frame `RANGE BETWEEN 7 PRECEDING AND CURRENT ROW` on an integer ORDER BY column when "7 rows" was meant , use `ROWS` instead.
- `OVER (PARTITION BY x ORDER BY x)` , partitioning and ordering on the same column with no other key produces one row per partition, which is rarely intended.

## Reference Links :

- [references/methods.md](references/methods.md) : Full window-function table (args, return type, ORDER BY requirement, frame sensitivity), frame-clause grammar, EXCLUDE modes
- [references/examples.md](references/examples.md) : Verified examples for running total, top-N-per-group, LAG/LEAD, moving average (ROWS vs RANGE), named windows, EXCLUDE
- [references/anti-patterns.md](references/anti-patterns.md) : Window-function anti-patterns with cause, symptom, SQLSTATE (where applicable), fix pattern

## See Also :

- `postgres-syntax-cte-lateral` : LATERAL joins as a top-N-per-group alternative when the rank logic is complex
- `postgres-syntax-aggregates` : `FILTER (WHERE ...)` aggregate clause , works with both window and group aggregates
- `postgres-syntax-dml-ddl` : `RETURNING` does NOT support window functions ; use a CTE if you need a window on the affected rows
- `postgres-core-version-matrix` : v11 added `GROUPS` frame mode and `EXCLUDE`, v15 optimized `row_number`/`rank`/`dense_rank`/`count` performance
- Vooronderzoek section : §5 (Query Features , Windows, CTE, LATERAL)
- Official docs : https://www.postgresql.org/docs/17/sql-expressions.html#SYNTAX-WINDOW-FUNCTIONS, https://www.postgresql.org/docs/17/functions-window.html, https://www.postgresql.org/docs/17/tutorial-window.html
