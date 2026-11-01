# postgres-syntax-window-functions : Examples

All examples verified against PostgreSQL 17 docs. Assume the standard `payments(customer_id, paid_at, amount)` and `employees(employee_id, department_id, hired_at, salary)` shapes unless noted.

## Example 1 : Running total per customer

```sql
SELECT customer_id, paid_at, amount,
       sum(amount) OVER (
           PARTITION BY customer_id
           ORDER BY paid_at
           ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
       ) AS running_total
FROM payments
ORDER BY customer_id, paid_at;
```

Output (per customer, monotonically increasing) :

```
 customer_id |        paid_at         | amount | running_total
-------------+------------------------+--------+---------------
           1 | 2026-01-05 10:00:00+01 |  50.00 |         50.00
           1 | 2026-01-12 11:30:00+01 |  20.00 |         70.00
           1 | 2026-02-03 09:15:00+01 |  30.00 |        100.00
           2 | 2026-01-09 08:00:00+01 |  75.00 |         75.00
```

## Example 2 : Top-N-per-group with ROW_NUMBER (top 3 paid per department)

```sql
SELECT department_id, employee_id, salary, rn
FROM (
  SELECT department_id, employee_id, salary,
         row_number() OVER (PARTITION BY department_id ORDER BY salary DESC) AS rn
  FROM employees
) ranked
WHERE rn <= 3
ORDER BY department_id, rn;
```

Variant with `RANK` (keeps ties together, possibly more than 3 rows per dept) :

```sql
SELECT department_id, employee_id, salary, r
FROM (
  SELECT department_id, employee_id, salary,
         rank() OVER (PARTITION BY department_id ORDER BY salary DESC) AS r
  FROM employees
) ranked
WHERE r <= 3;
```

## Example 3 : LAG / LEAD for day-over-day delta

```sql
SELECT region, day, revenue,
       revenue - lag(revenue, 1, 0::numeric) OVER w AS delta_vs_prev,
       lead(revenue, 1)                    OVER w AS next_revenue,
       lead(revenue, 1, revenue)           OVER w - revenue AS delta_to_next
FROM daily_revenue
WINDOW w AS (PARTITION BY region ORDER BY day)
ORDER BY region, day;
```

`lag(revenue, 1, 0::numeric)` : look back 1 row, return `0` at the partition start instead of NULL.
`lead(revenue, 1, revenue)` : look forward 1 row, return the current row's revenue if at partition end (so the delta is 0).

## Example 4 : LAST_VALUE done correctly

```sql
-- WRONG (returns paid_at itself on every row) :
SELECT customer_id, paid_at,
       last_value(paid_at) OVER (PARTITION BY customer_id ORDER BY paid_at) AS broken
FROM payments;

-- CORRECT (last paid_at per customer, on every row) :
SELECT customer_id, paid_at,
       last_value(paid_at) OVER (
           PARTITION BY customer_id
           ORDER BY paid_at
           ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING
       ) AS last_paid_at
FROM payments;

-- Alternative without changing the frame : reverse ORDER BY and use FIRST_VALUE
SELECT customer_id, paid_at,
       first_value(paid_at) OVER (PARTITION BY customer_id ORDER BY paid_at DESC) AS last_paid_at
FROM payments;
```

## Example 5 : Named windows shared across multiple OVER references

```sql
SELECT customer_id, paid_at, amount,
       sum(amount)          OVER w_running AS running_total,
       avg(amount)          OVER w_running AS running_avg,
       count(*)             OVER w_running AS payment_no,
       sum(amount)          OVER w_full    AS lifetime_total,
       first_value(paid_at) OVER w_full    AS first_paid,
       last_value(paid_at)  OVER w_full    AS last_paid
FROM payments
WINDOW
  base      AS (PARTITION BY customer_id ORDER BY paid_at),
  w_running AS (base ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW),
  w_full    AS (base ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING);
```

The base named window factors PARTITION BY + ORDER BY ; the two derived windows attach different frames.

## Example 6 : Moving average , ROWS vs RANGE

```sql
-- Last 7 rows (regardless of timestamp gaps)
SELECT sensor_id, ts, reading,
       avg(reading) OVER (
           PARTITION BY sensor_id ORDER BY ts
           ROWS BETWEEN 6 PRECEDING AND CURRENT ROW
       ) AS ma_7row
FROM sensor_readings;

-- Last 7 days (variable row count)
SELECT sensor_id, ts, reading,
       avg(reading) OVER (
           PARTITION BY sensor_id ORDER BY ts
           RANGE BETWEEN INTERVAL '6 days' PRECEDING AND CURRENT ROW
       ) AS ma_7day
FROM sensor_readings;
```

Verify the difference :

```sql
SELECT ts, count(*) OVER (PARTITION BY sensor_id ORDER BY ts
                          ROWS BETWEEN 6 PRECEDING AND CURRENT ROW) AS rows_in_frame,
            count(*) OVER (PARTITION BY sensor_id ORDER BY ts
                          RANGE BETWEEN INTERVAL '6 days' PRECEDING AND CURRENT ROW) AS rows_in_range
FROM sensor_readings ORDER BY ts;
```

## Example 7 : EXCLUDE CURRENT ROW for neighbour-only average (v11+)

```sql
SELECT sensor_id, ts, reading,
       avg(reading) OVER (
           PARTITION BY sensor_id ORDER BY ts
           ROWS BETWEEN 3 PRECEDING AND 3 FOLLOWING
           EXCLUDE CURRENT ROW
       ) AS neighbours_avg
FROM sensor_readings;
```

Output : every row sees the average of its 3-before + 3-after rows, NOT itself. At partition boundaries the frame is shorter ; `avg` handles short frames automatically.

## Example 8 : GROUPS frame (v11+)

```sql
-- Salary changes ranked into peer groups ; sum each peer group with its neighbours
SELECT employee_id, salary,
       rank() OVER w AS r,
       sum(salary) OVER (
           ORDER BY salary
           GROUPS BETWEEN 1 PRECEDING AND 1 FOLLOWING
       ) AS sum_3_peer_groups
FROM employees
WINDOW w AS (ORDER BY salary);
```

`GROUPS BETWEEN 1 PRECEDING AND 1 FOLLOWING` includes the previous peer group, the current peer group, and the next peer group , row counts vary with the group sizes.

## Example 9 : NTILE for quartile bucketing

```sql
-- Assign each employee to a salary quartile (1 = highest 25%)
SELECT employee_id, salary,
       ntile(4) OVER (ORDER BY salary DESC) AS salary_quartile
FROM employees;
```

`NTILE(4)` divides the partition into 4 contiguous buckets of roughly equal size. The extra rows (when count is not divisible by 4) go to the lowest-numbered buckets.

## Example 10 : Compare current row to first and last of partition

```sql
SELECT customer_id, paid_at, amount,
       amount - first_value(amount) OVER (
           PARTITION BY customer_id ORDER BY paid_at
           ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING
       ) AS delta_vs_first,
       last_value(amount) OVER (
           PARTITION BY customer_id ORDER BY paid_at
           ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING
       ) - amount AS delta_to_last
FROM payments;
```

Both calls share the same partition+order+frame , factor them via `WINDOW` to avoid duplication.

## Example 11 : Filter on window result via subquery

```sql
-- Find every row whose value is in the bottom 10% within its category :
SELECT *
FROM (
  SELECT t.*, percent_rank() OVER (PARTITION BY category ORDER BY score) AS pr
  FROM things t
) s
WHERE pr <= 0.10;
```

Cannot write `WHERE percent_rank() OVER (...) <= 0.10` directly , SQLSTATE `42P20` `window functions are not allowed in WHERE`.

## Example 12 : Eliminating self-join with LAG

Before (broken on missing days) :

```sql
SELECT a.day, a.revenue,
       a.revenue - b.revenue AS delta
FROM daily_revenue a
LEFT JOIN daily_revenue b ON b.day = a.day - 1
WHERE a.region = b.region;
```

After (works regardless of date gaps) :

```sql
SELECT day, revenue,
       revenue - lag(revenue) OVER (PARTITION BY region ORDER BY day) AS delta
FROM daily_revenue
ORDER BY region, day;
```

Sources verified against : postgresql.org/docs/17/tutorial-window.html, postgresql.org/docs/17/sql-expressions.html#SYNTAX-WINDOW-FUNCTIONS, postgresql.org/docs/17/functions-window.html, postgresql.org/docs/17/sql-select.html.
