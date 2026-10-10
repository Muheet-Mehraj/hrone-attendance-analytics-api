# Employee Attendance & Analytics API

A FastAPI + MongoDB service for employee records, punch-in/punch-out, attendance corrections with an audit trail, and analytics built on MongoDB aggregation pipelines. Built for the HROne Software Engineer Trainee assignment.

**Status:** every endpoint in `openapi.yaml` is implemented. Nothing is missing.

## Quick start

Requires Python 3.11+ and MongoDB 6.0+ (local or Atlas).

```bash
python -m venv .venv
source .venv/bin/activate        # Windows PowerShell: .\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

export MONGO_URI="mongodb://localhost:27017"   # PowerShell: $env:MONGO_URI = "..."
export MONGO_DB="attendance_db"                # PowerShell: $env:MONGO_DB = "..."

uvicorn app.main:app --port 8000
```

Both variables are required. A local `.env` is loaded if present, but real environment variables take precedence. Indexes are created at startup. Swagger UI is at `http://127.0.0.1:8000/docs`.

## Endpoints

| Method | Route | What it does |
|---|---|---|
| GET | `/health` | Pings MongoDB; `503` until the database and indexes are ready |
| POST | `/employees` | Create an employee (`409` on a duplicate `emp_code`) |
| GET | `/employees` | List employees, optional `department`, paginated |
| POST | `/attendance/punch-in` | Create the day's record (`404` unknown employee, `409` duplicate) |
| POST | `/attendance/punch-out` | Close the latest open record; computes hours, overtime, half-day |
| GET | `/attendance` | List records with filters, sorted by date desc then `emp_code` |
| PATCH | `/attendance/{emp_code}/{date}` | Correct a record; recomputes derived fields and appends to `history` |
| GET | `/analytics/employees/{emp_code}/monthly` | Monthly summary for one employee |
| GET | `/analytics/departments/summary` | Per-department monthly summary |
| GET | `/analytics/leaderboard/late` | Late-minutes ranking with competition ranks |
| GET | `/analytics/departments/{department}/trend` | One row per calendar day with a 7-day moving average |
| GET | `/admin/explain/{endpoint}` | MongoDB `executionStats` for an endpoint's main query |

All instants are **epoch milliseconds**; calendar dates are `YYYY-MM-DD` and shift times `HH:MM` (IST). Errors use FastAPI's default `{"detail": ...}` body.

## Implementation notes

- **Time zones (R1):** instants are stored as UTC datetimes, truncated to whole seconds. Attendance dates and shifts are computed in IST. An overnight punch-in before `shift_end` belongs to the previous day's shift.
- **Derived fields (R2-R5):** late is counted only beyond a 10-minute grace period, measured from shift start; overtime only when at least 30 minutes; work hours use decimal half-up rounding; half-day is under 4.50 rounded hours.
- **Analytics rounding (R8):** inside the pipelines, half-up rounding is done in Decimal128 so values like 1.005 round correctly.
- **Concurrency:** one record per `(emp_code, date)` is guaranteed by a unique index, so exactly one of two simultaneous punch-ins succeeds. Punch-out and PATCH use conditional updates so a second writer cannot overwrite the first.
- **Analytics:** the monthly summary, department summary, leaderboard and trend all run as aggregation pipelines. The leaderboard applies `limit` to the rank, so ties at the cutoff are all returned.
- **Indexes:** see `DECISIONS.md` for what each index serves.

## Tests

```bash
pip install pytest httpx
python -m pytest -q
```

The tests need a running MongoDB and write uniquely named records to the configured database, so use a dedicated test database. They cover employee and attendance flows, duplicate and unknown-employee cases, timestamp and date-range validation, regularization history, overnight shifts, monthly analytics, leaderboard ties, trend gap-filling, weekend records, and half-up rounding.

## Documents

- [`REVIEW.md`](REVIEW.md): defects found in the starter code and how each was fixed.
- [`DECISIONS.md`](DECISIONS.md): design decisions (indexes, concurrency, ties, headcount, scaling).

## Notes

`/admin/explain` has no authentication, since the assignment has none. A real deployment would need access control or would remove it.