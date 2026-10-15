# DECISIONS.md

**1. Indexes**

The application creates six indexes:

- `employees.emp_code` (unique) — enforces uniqueness so concurrent POST `/employees` requests yield exactly one 201 and the rest 409, without a pre-check read.
- `employees.(department, emp_code)` — supports the department filter and employee-code sort in `GET /employees`.
- `employees.joined_on` — supports month-end eligibility filters in the department analytics.
- `attendance_logs.(emp_code, date)` (unique) — prevents duplicate attendance records for the same employee and date, and supports the regularization lookup.
- `attendance_logs.(date DESC, emp_code ASC)` — matches the sort order required by `GET /attendance`; the query still fetches documents, so it is not a covered query.
- `attendance_logs.date` — supports date-range analytics filters.

I did not add a partial index for open punch records. It could help the punch-out lookup at larger scale, but the expected per-employee history is small and the extra index would add write/storage cost.

**2. Punch-in race**

Both requests hit `insert_one` with the same `(emp_code, date)` document at the same instant. MongoDB tries to insert both. Exactly one succeeds; the other hits the unique index and raises `DuplicateKeyError`. My code catches that exception and returns 409. No pre-check read is involved, so there is no window for a race.

**3. Ties**

I compute ranks in Python after pulling all late employees for the month, sorted by `total_late_minutes` DESC then `emp_code` ASC. I carry a `rank` counter that only advances when the current row's minutes are strictly less than the previous row's. If two employees share rank 3 and `limit=3`, both have `rank <= 3` and both appear in the response, so the response can have more than `limit` items.

**4. Headcount**

I run a separate aggregation on the `employees` collection (filtered to `joined_on <= month_end`) to get headcount per department before touching the logs. Employees with zero logs never appear in the logs aggregation but are still counted because headcount comes from the employees collection, not from the logs. I then merge the two results, defaulting all stats to 0 for departments with no logs that month.

**5. One thing I would change at 100× scale**

I would avoid loading all matching attendance logs into the API process. The trend endpoint is currently computed in Python for a maximum 92-day range. At larger data volumes, I would move the employee/log join and daily calculations into MongoDB aggregation, then measure whether server-side gap filling and window calculations improve latency. The existing `(emp_code, date)` attendance index can support the employee/date lookup; there is no `department` field on attendance records to index directly.
