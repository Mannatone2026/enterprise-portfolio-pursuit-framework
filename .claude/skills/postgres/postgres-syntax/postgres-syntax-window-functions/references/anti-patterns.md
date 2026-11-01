# postgres-syntax-window-functions : Anti-Patterns

Each entry : cause , symptom (with SQLSTATE where applicable) , fix pattern.

## AP-1 : `LAST_VALUE(col) OVER (ORDER BY ts)` returns the current row

**Cause** : With `ORDER BY` but no explicit frame, the default is `RANGE BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW`. In RANGE mode `CURRENT ROW` means "the current row plus its peers (rows with the same ORDER BY value)". For a unique `ts` the frame ends at the current row itself.

**Symptom** : `last_value(paid_at) OVER (PARTITION BY customer_id ORDER BY paid_at)` returns `paid_at` on every row instead of the last paid_at per customer. Silent wrong result , no error.

**Fix** :
```sql
last_value(paid_at) OVER (
    PARTITION BY customer_id ORDER BY paid_at
    ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING
)
```
Or reverse the ORDER BY and use `first_value()` :
```sql
first_value(paid_at) OVER (PARTITION BY customer_id ORDER BY paid_at DESC)
```

## AP-2 : Window function in `WHERE` / `GROUP BY` / `HAVING`

**Cause** : Mental model that "where" comes after "select" in execution.

**Symptom** : `ERROR: window functions are not allowed in WHERE` (SQLSTATE `42P20`). Same error for GROUP BY and HAVING.

**Fix** : wrap in a subquery / CTE :
```sql
SELECT * FROM (
  SELECT t.*, row_number() OVER (PARTITION BY g ORDER BY x DESC) AS rn FROM t
) s WHERE s.rn <= 3;
```
Window functions execute AFTER `WHERE` ; the wrapping query gives them a column to be filtered.

## AP-3 : `ROW_NUMBER()` without `ORDER BY` in OVER

**Cause** : Quick prototype, "I just want a row number".

**Symptom** : The row-numbers vary between runs (and between parallel workers). Tests that assume `rn = 1` is a specific row fail intermittently.

**Fix** : always supply an ORDER BY inside OVER that breaks ties deterministically :
```sql
row_number() OVER (ORDER BY id)              -- deterministic by primary key
row_number() OVER (PARTITION BY g ORDER BY x DESC, id)  -- deterministic tiebreaker
```

## AP-4 : Using `RANK()` when `DENSE_RANK()` was intended

**Cause** : The names sound similar.

**Symptom** : `RANK` produces gaps after ties : `1, 2, 2, 4, 5`. Downstream consumers that expect contiguous ranks see "no rank 3".

**Fix** : pick deliberately :
```
row_number(): 1, 2, 3, 4, 5   -- arbitrary tiebreak unless ORDER BY breaks ties
rank():       1, 2, 2, 4, 5   -- gap after tie
dense_rank(): 1, 2, 2, 3, 4   -- no gap
```

## AP-5 : Correlated subquery instead of window for running total

**Cause** : Carry-over from older SQL dialects without window support.

**Symptom** :
```sql
SELECT id, amount,
       (SELECT sum(amount) FROM payments p2 WHERE p2.id <= p1.id) AS running_total
FROM payments p1;
```
O(n^2) execution , every row scans the entire prefix. On 1M rows the query never returns.

**Fix** :
```sql
SELECT id, amount,
       sum(amount) OVER (ORDER BY id ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)
       AS running_total
FROM payments;
```
O(n) with a single sequential scan + sort by id.

## AP-6 : Self-join `t.id = prev.id - 1` to look at previous row

**Cause** : The previous row is implied by integer adjacency.

**Symptom** : Breaks when ids are not contiguous (deletions, sharding, gaps from rollbacks). The "previous" row is silently dropped for any row whose `id - 1` does not exist.

**Fix** : `LAG` uses the OVER ORDER BY, not arithmetic on the ID :
```sql
SELECT id, val, lag(val) OVER (ORDER BY id) AS prev_val FROM t;
```
Works through any sequence of ids.

## AP-7 : `RANGE BETWEEN n PRECEDING AND CURRENT ROW` where ROWS was meant

**Cause** : Mental shortcut "n preceding rows" : RANGE looks the same in the syntax.

**Symptom** : On a numeric ORDER BY column, `RANGE BETWEEN 7 PRECEDING AND CURRENT ROW` includes rows whose value is in the range `[current_value - 7, current_value]` , row counts vary depending on data density. On a date column `RANGE BETWEEN 7 PRECEDING ...` errors with `42883` because `7` cannot be subtracted from a `date` without an INTERVAL.

**Fix** : use `ROWS` for row-count windows ; reserve `RANGE` for value-range windows (with `INTERVAL` for time types) :
```sql
-- N-row window :
ROWS BETWEEN 6 PRECEDING AND CURRENT ROW            -- 7 rows total
-- Time-value window :
RANGE BETWEEN INTERVAL '7 days' PRECEDING AND CURRENT ROW
```

## AP-8 : Forgetting NULL handling in LAG / FIRST_VALUE

**Cause** : Expecting `IGNORE NULLS` semantics (SQL-standard, not implemented in PostgreSQL).

**Symptom** : `lag(col)` returns NULL for any row whose immediate predecessor's `col` is NULL ; intermediate NULLs propagate. Aggregations on the lagged column underestimate.

**Fix** : pre-filter NULLs in a CTE / subquery, then apply the window function :
```sql
WITH non_null AS (
  SELECT * FROM t WHERE col IS NOT NULL
)
SELECT id, col, lag(col) OVER (ORDER BY id) AS prev_col FROM non_null;
```
Or use `coalesce(lag(col, 1, default), default)` if a static default is acceptable.

## AP-9 : `OVER (PARTITION BY x ORDER BY x)` with no other key

**Cause** : Trying to "rank x by itself".

**Symptom** : Each partition has exactly one row (every row has a unique value of x) , `ROW_NUMBER()` is always 1, `SUM` over the window equals x itself. The query technically works but is meaningless.

**Fix** : either drop the PARTITION BY (rank x across the whole table) or add a real grouping key (`PARTITION BY g ORDER BY x`).

## AP-10 : `NTILE(n)` without ORDER BY

**Cause** : Operator wants n buckets and trusts the planner's row order.

**Symptom** : Bucket assignment varies between runs and between parallel workers. Reports that depend on quartile membership are non-reproducible.

**Fix** : always supply an ORDER BY :
```sql
ntile(4) OVER (ORDER BY salary DESC)            -- quartiles by salary, highest first
```

## AP-11 : Multiple OVER clauses that are subtly different

**Cause** : Copy-paste with a typo (`ORDER BY paid_at` vs `ORDER BY paid_at, id`).

**Symptom** : Two columns in the same SELECT use frames that look identical but rank rows differently on ties. Downstream summation produces rows that don't add up.

**Fix** : factor every OVER into a named WINDOW :
```sql
SELECT ...
WINDOW w AS (PARTITION BY customer_id ORDER BY paid_at, id);
-- ALL references use `OVER w` ; impossible to desync.
```

## AP-12 : Using window when GROUP BY was correct

**Cause** : The query wants one row per group ; the author wrote a window aggregate and then `DISTINCT` to dedupe.

**Symptom** : Slower than the equivalent GROUP BY because every row carries the aggregate, then DISTINCT scans the entire result.

**Fix** : if the output is one row per group, use `GROUP BY` :
```sql
-- Window + DISTINCT (slow) :
SELECT DISTINCT region, sum(revenue) OVER (PARTITION BY region) FROM daily_revenue;
-- Plain GROUP BY (fast) :
SELECT region, sum(revenue) FROM daily_revenue GROUP BY region;
```

## SQLSTATE Reference

| SQLSTATE | Meaning | Typical window-function trigger |
|---|---|---|
| `42P20` | `windowing_error` | Window function in `WHERE` / `GROUP BY` / `HAVING` ; invalid frame clause (e.g. RANGE without ORDER BY when offset is non-zero). |
| `42883` | `undefined_function` | RANGE offset with incompatible type (e.g. integer offset on a date column without INTERVAL). |
| `2201X` | `invalid_argument_for_nth_value_function` | `nth_value(value, n)` called with `n` <= 0. |
| `22023` | `invalid_parameter_value` | `ntile(0)` or `ntile(negative)`. |
| `0A000` | `feature_not_supported` | Attempting `IGNORE NULLS` on LAG/LEAD/FIRST/LAST/NTH_VALUE , not implemented. |

Source : postgresql.org/docs/17/errcodes-appendix.html, postgresql.org/docs/17/functions-window.html.
