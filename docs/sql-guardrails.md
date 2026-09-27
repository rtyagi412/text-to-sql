# SQL guardrails

Every query the model writes (`/sql/write`), every query a reviewer approves (`/sql/approve`) and any query sent to
`/sql/validate` goes through the same checks. They run in order and stop at the first failure.

| # | Check | Where | Runs the query? | On failure |
|---|---|---|---|---|
| 1 | **Valid & safe**: exactly one `SELECT`. No `SELECT *`, CTE, `INTO` or `OPENROWSET`/`OPENQUERY`. Every table schema-qualified. Every table and column in the catalog. Every output column named and unique | `sql_check_service.check_sql` (sqlglot) | no | rejected |
| 2 | **Does what was asked** (write only): the select list is exactly the requested fields, every filter is used, every derived field's source table is read, and no table is joined without a condition | `write._check_generated_sql` | no | rejected |
| 3 | **Compiles**: SQL Server compiles the batch and reports its output columns | `sql_compile_service` (`sp_describe_first_result_set`) | no | rejected |
| 4 | **Estimated plan**: SQL Server's plan is read for performance problems | `sql_plan_service` (`SET SHOWPLAN_XML ON`) | no | warning (rejected above `SQL_PLAN_MAX_COST`) |
| 5 | **Sample run**: the query runs as `SELECT TOP n …` under a query timeout and a lock timeout, then is rolled back | `sql_sample_service` | yes, capped at `SQL_SAMPLE_ROWS` rows | a runtime error is rejected; slow or no rows is a warning |

"Rejected" means different things depending on the endpoint:
- `/sql/write` sends SQL Server's own reason back to the model so it can fix the query (`generate_checked`, one retry).
- `/sql/approve` refuses the SQL with a 400.
- `/sql/validate` answers `valid: false` with the `errors`.

Checks 3–5 are orchestrated by `sql_guardrail_service.run_on_source`. If SQL Server can't be reached, or the login has no permission, that is not the query's fault: the checks are skipped and a warning says so.

## Performance findings (warnings)

From the SQL text (`sql_check_service`):
- A function or arithmetic applied to a filtered or joined column (non-sargable).
- `LIKE '%…'`.
- `DISTINCT`.
- A comma or `CROSS JOIN` with no join condition.
- `NOT IN (subquery)`: use `NOT EXISTS` instead.
- A subquery in the select list.
- `OR` across different columns.
- A `LEFT JOIN` whose table is then filtered in `WHERE`, which turns it into an inner join.

From SQL Server's estimated plan (`sql_plan_service`):
- Estimated cost above `SQL_PLAN_WARN_COST`.
- A scan of a table of at least `SQL_PLAN_SCAN_MIN_ROWS` rows that keeps under 10% of them.
- An implicit conversion that prevents a seek.
- A join with no join predicate.
- Columns without statistics.
- The missing indexes SQL Server suggests.

From the sample run (`sql_sample_service`):
- It didn't return its first rows within `SQL_SAMPLE_TIMEOUT_SECONDS`.
- It took longer than `SQL_SAMPLE_SLOW_MS`.
- It returned no rows, which usually means a filter value doesn't match the stored data.

## Why a sample run as well as a compile

Compiling catches everything visible without data. Some errors only appear once rows flow:
- a string that won't convert to a number (`Conversion failed …`)
- divide by zero
- a subquery that returns more than one value
- arithmetic overflow

A run capped at a few rows catches these without reading the whole table.

A capped run can't prove that no later row fails. It proves the query executes and returns data of the right shape.

## Safety of the sample run

- Only SQL that has passed check 1 runs: one plain, read-only `SELECT`.
- `TOP n` is added to the outer `SELECT`. An existing `TOP` keeps the smaller of the two. `TOP … PERCENT`/`WITH TIES` and `OFFSET … FETCH` are left as written, and at most `n` rows are fetched.
- It runs with a pyodbc query timeout and `SET LOCK_TIMEOUT 5000`, so it never waits long behind another session's locks.
- Afterwards the transaction is rolled back and the connection settings are restored. A connection whose settings can't be restored is discarded from the pool.
- Sample rows are returned to the caller only when `SQL_SAMPLE_PREVIEW` is on, and are never sent to the model.
- The source database login should be read-only.

## Response fields

`/sql/write`, `/sql/approve` and `/sql/validate` return:
- `compiled`
- `plan`: `estimated_cost`, `estimated_rows`, `missing_indexes`
- `sample`: `executed`, `row_limit`, `rows_returned`, `elapsed_ms`, `timed_out`, `columns`, `rows`
- `warnings`

## Settings

`WRITE_COMPILE_CHECK`, `SQL_PLAN_CHECK`, `SQL_PLAN_WARN_COST`, `SQL_PLAN_MAX_COST`, `SQL_PLAN_SCAN_MIN_ROWS`,
`SQL_SAMPLE_CHECK`, `SQL_SAMPLE_ROWS`, `SQL_SAMPLE_TIMEOUT_SECONDS`, `SQL_SAMPLE_SLOW_MS`, `SQL_SAMPLE_PREVIEW` (see
`.env.example`).
