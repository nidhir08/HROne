# Employee Attendance & Analytics API

A FastAPI + MongoDB API for tracking employee punch-in/out and computing attendance analytics.

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # set MONGO_URI and MONGO_DB
python sample_seed.py
uvicorn app.main:app --port 8000 --reload
```

MongoDB **6.0 or newer** required (tested on 7.0). A free MongoDB Atlas M0 cluster works.

## Environment variables

| Variable    | Default                       | Description           |
|-------------|-------------------------------|-----------------------|
| `MONGO_URI` | `mongodb://localhost:27017`   | MongoDB connection    |
| `MONGO_DB`  | `attendance_db`               | Database name         |

## What is implemented

All endpoints in the OpenAPI contract:

- `GET  /health` – MongoDB ping probe
- `POST /employees` – create employee (unique index, concurrent-safe)
- `GET  /employees` – paginated list, sorted by emp_code
- `POST /attendance/punch-in` – punch in with R1/R2 rules, concurrent-safe via unique index
- `POST /attendance/punch-out` – punch out with R3/R4/R5, concurrent-safe via atomic update
- `GET  /attendance` – paginated list with filters, DB-side sort (no in-memory sort)
- `PATCH /attendance/{emp_code}/{date}` – regularize with full audit trail, atomic
- `GET  /analytics/employees/{emp_code}/monthly` – MongoDB aggregation, R7 working days
- `GET  /analytics/departments/summary` – per-department stats including zero-log headcount (R9)
- `GET  /analytics/leaderboard/late` – standard competition ranking with correct tie handling
- `GET  /analytics/departments/{department}/trend` – daily trend with 7-day moving average
- `GET  /admin/explain/{endpoint}` – runs `explain executionStats` for the named endpoint's query

## Notes

- All timestamps are stored as BSON dates (UTC) and returned as epoch milliseconds.
- Derived fields (`work_hours`, `late_minutes`, `overtime_minutes`, `half_day`) are always
  computed by the API and read back from the stored values in analytics (R4, R5 note).
- Half-up rounding is used everywhere (not Python's default banker's rounding).
