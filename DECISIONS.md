# DECISIONS.md

**1. Indexes**

I created four indexes:

- `employees.emp_code` (unique) — enforces uniqueness so concurrent POST `/employees` requests yield exactly one 201 and the rest 409, without a pre-check read.
- `employees.(department, emp_code)` — covers the `list_employees` filter + sort without hitting the collection scan path.
- `attendance_logs.(emp_code, date)` (unique) — the primary key of every attendance record; makes the PATCH lookup O(1) and prevents duplicate punch-ins atomically.
- `attendance_logs.(date DESC, emp_code ASC)` — matches the sort order required by `GET /attendance`, so the list query is a covered index scan at any scale.

I considered a sparse index on `punch_in` to speed up the punch-out lookup ("most recent open record"). I rejected it because punch-out is a point-write on a single employee — the `(emp_code, date)` index already narrows the scan to that employee's records, and the typical employee has at most a handful of open records at once.

**2. Punch-in race**

Both requests hit `insert_one` with the same `(emp_code, date)` document at the same instant. MongoDB tries to insert both. Exactly one succeeds; the other hits the unique index and raises `DuplicateKeyError`. My code catches that exception and returns 409. No pre-check read is involved, so there is no window for a race.

**3. Ties**

I compute ranks in Python after pulling all late employees for the month, sorted by `total_late_minutes` DESC then `emp_code` ASC. I carry a `rank` counter that only advances when the current row's minutes are strictly less than the previous row's. If two employees share rank 3 and `limit=3`, both have `rank <= 3` and both appear in the response, so the response can have more than `limit` items.

**4. Headcount**

I run a separate aggregation on the `employees` collection (filtered to `joined_on <= month_end`) to get headcount per department before touching the logs. Employees with zero logs never appear in the logs aggregation but are still counted because headcount comes from the employees collection, not from the logs. I then merge the two results, defaulting all stats to 0 for departments with no logs that month.

**5. One thing I would change at 100× scale**

I would replace the Python-loop trend computation with a proper `$densify` + `$fill` MongoDB aggregation pipeline so the database handles gap-filling and windowed moving averages server-side. At 100× data the per-day Python loop is still fine (max 92 iterations), but pulling thousands of log rows per request would become the bottleneck. A server-side pipeline with a covering index on `(department, date)` would cut network round-trips and memory pressure significantly.
