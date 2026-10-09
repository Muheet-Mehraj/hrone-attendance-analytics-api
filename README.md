# Employee Attendance & Analytics API

A REST API for employee management, daily attendance tracking, attendance regularization, and monthly/department analytics. The service is built with **FastAPI** and **MongoDB** and was developed for the HROne Software Engineer Trainee assessment.

- **Language:** Python 3.11+
- **API framework:** FastAPI
- **Database:** MongoDB 6.0+
- **API documentation:** Interactive Swagger UI at `/docs`
- **Automated tests:** `pytest` with FastAPI's `TestClient`

> This repository is an assessment project. Review the API contract and the assessment instructions before deploying it beyond a local development environment.

## Contents

- [Features](#features)
- [Project structure](#project-structure)
- [Prerequisites](#prerequisites)
- [Run locally](#run-locally)
- [Environment configuration](#environment-configuration)
- [API reference](#api-reference)
- [Attendance calculation rules](#attendance-calculation-rules)
- [MongoDB indexes and aggregation](#mongodb-indexes-and-aggregation)
- [Run tests](#run-tests)
- [Troubleshooting](#troubleshooting)
- [Project decisions and review notes](#project-decisions-and-review-notes)
- [Security and deployment notes](#security-and-deployment-notes)

## Features

- Create employees and list employees with department filtering and pagination.
- Record punch-in and punch-out times for an employee's attendance day.
- Validate timestamps, shift times, date ranges, and attendance statuses.
- Calculate late minutes, work hours, overtime, and half-day status.
- Correct attendance records through regularization while retaining an audit history.
- Return employee monthly attendance metrics.
- Summarize attendance by department.
- Build a late-arrival leaderboard that preserves ties at the requested rank cutoff.
- Generate department trends with a continuous daily date series, including weekends and dates with no attendance records.
- Expose an explain endpoint to inspect MongoDB execution statistics for supported list and analytics queries.
- Create idempotent indexes at application startup and check database readiness through `/health`.

## Project structure

```text
hrone-attendance-analytics-api/
├── app/
│   ├── __init__.py
│   └── main.py             # API routes, validation, calculations, and MongoDB pipelines
├── test_api.py             # Automated API tests
├── requirements.txt        # Runtime dependencies
├── README.md               # Setup and usage guide
├── REVIEW.md               # Review of starter-code issues and corresponding fixes
├── DECISIONS.md            # Design choices and trade-offs
└── .gitignore              # Excludes local configuration and generated files
```

## Prerequisites

Install the following before running the API:

1. **Python 3.11 or newer**
2. **MongoDB 6.0 or newer**, running locally or at a reachable MongoDB URI
3. **Git**, to clone the repository

The application reads its database configuration from environment variables. It does not require a committed `.env` file.

## Run locally

### 1. Clone the repository

```bash
git clone https://github.com/Muheet-Mehraj/hrone-attendance-analytics-api.git
cd hrone-attendance-analytics-api
```

### 2. Create and activate a virtual environment

**Windows PowerShell**

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
```

If PowerShell blocks activation in the current terminal, you can allow script execution for this process only:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy RemoteSigned
.\.venv\Scripts\Activate.ps1
```

**macOS / Linux**

```bash
python3 -m venv .venv
source .venv/bin/activate
```

### 3. Install dependencies

```bash
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

The runtime requirements are listed in `requirements.txt`. To run the automated tests, install the test tools in the same environment if they are not already present:

```bash
python -m pip install pytest httpx
```

### 4. Start MongoDB

Start MongoDB using your existing local installation or configured MongoDB service. If you use Docker and do not already have a MongoDB container, a local development instance can be started with:

```bash
docker run -d --name hrone-mongo -p 27017:27017 mongo:6
```

This command is optional; Docker is not required if MongoDB is already running locally or remotely. The repository intentionally does not include a Dockerfile.

### 5. Configure the environment

Set the MongoDB connection and database name in the same terminal used to launch the API.

**Windows PowerShell**

```powershell
$env:MONGO_URI = "mongodb://localhost:27017"
$env:MONGO_DB = "attendance_db"
```

**macOS / Linux**

```bash
export MONGO_URI="mongodb://localhost:27017"
export MONGO_DB="attendance_db"
```

For a remote database, use the connection URI supplied by your database provider. Do not commit credentials or connection strings to Git.

### 6. Start the API

Run from the repository root, the directory containing `app/`:

```bash
python -m uvicorn app.main:app --reload --port 8000
```

On startup, the application attempts to create its MongoDB indexes. Open these URLs once the server is running:

- **Swagger UI:** <http://127.0.0.1:8000/docs>
- **ReDoc:** <http://127.0.0.1:8000/redoc>
- **Health check:** <http://127.0.0.1:8000/health>

The health endpoint pings MongoDB and checks that index initialization has succeeded. A successful response is expected to look like:

```json
{
  "status": "ok"
}
```

If MongoDB is unavailable or the indexes are not ready, the endpoint returns HTTP `503`.

## Environment configuration

| Variable | Required | Description | Local example |
|---|---|---|---|
| `MONGO_URI` | Yes | MongoDB connection URI | `mongodb://localhost:27017` |
| `MONGO_DB` | Yes | Database name used by the application | `attendance_db` |

The application calls `load_dotenv()` for local development convenience, but existing environment variables take precedence. Keep any `.env` file local and untracked.

## API reference

The Swagger UI at `/docs` provides request schemas and interactive execution. All routes below are relative to `http://127.0.0.1:8000`.

### Health

| Method | Route | Purpose |
|---|---|---|
| `GET` | `/health` | Checks MongoDB connectivity and index readiness |

### Employees

| Method | Route | Purpose |
|---|---|---|
| `POST` | `/employees` | Creates an employee; returns `201` on success and `409` for a duplicate employee code |
| `GET` | `/employees` | Lists employees, optionally filtered by department, with pagination |

Example: create an employee

```http
POST /employees
Content-Type: application/json
```

```json
{
  "emp_code": "EMP1001",
  "name": "Aarav Sharma",
  "email": "aarav.sharma@example.com",
  "department": "Engineering",
  "shift_start": "09:30",
  "shift_end": "18:30",
  "joined_on": "2026-01-12"
}
```

`shift_start` and `shift_end` default to `09:30` and `18:30` if omitted. Shift values use 24-hour `HH:MM` format and cannot be identical. Employee codes must match `EMP` followed by 4–6 digits.

Example: list employees

```http
GET /employees?department=Engineering&page=1&page_size=20
```

Pagination is one-based. `page` must be at least `1`; `page_size` must be between `1` and `100`. The response includes `items`, `total`, `page`, and `page_size`.

### Attendance

| Method | Route | Purpose |
|---|---|---|
| `POST` | `/attendance/punch-in` | Records an employee's punch-in for the relevant attendance date |
| `POST` | `/attendance/punch-out` | Records punch-out and recalculates derived attendance values |
| `GET` | `/attendance` | Lists attendance records with filters and pagination |
| `PATCH` | `/attendance/{emp_code}/{date}` | Corrects an existing record and appends an audit-history entry |

Allowed attendance statuses are `PRESENT`, `ABSENT`, `LEAVE`, `WFH`, and `ON_DUTY`. Punch-in accepts only `PRESENT`, `WFH`, or `ON_DUTY`.

Example: punch in at an explicit time

```http
POST /attendance/punch-in
Content-Type: application/json
```

```json
{
  "emp_code": "EMP1001",
  "punched_at": 1791518400000,
  "status": "PRESENT"
}
```

`punched_at` is an **epoch-millisecond integer**, not epoch seconds. It can be omitted to use the current time. The example timestamp is illustrative; use a timestamp appropriate for the scenario being tested.

Example: punch out

```json
{
  "emp_code": "EMP1001",
  "punched_at": 1791550800000
}
```

The API rejects an unknown employee with `404`, a second punch-in for the same employee and attendance date with `409`, and invalid punch-out scenarios with appropriate `4xx` responses. Punch-out must occur after punch-in and within 24 hours.

Example: list attendance

```http
GET /attendance?emp_code=EMP1001&date_from=2026-10-01&date_to=2026-10-31&page=1&page_size=20
```

Optional filters include `emp_code`, `date_from`, `date_to`, and `status`. Date values use `YYYY-MM-DD`, and `date_from` cannot be after `date_to`.

Example: regularize an attendance record

```http
PATCH /attendance/EMP1001/2026-10-09
Content-Type: application/json
```

```json
{
  "status": "PRESENT",
  "punch_in": 1791518400000,
  "punch_out": 1791550800000,
  "reason": "Corrected after manager review",
  "regularized_by": "hr.admin"
}
```

Regularization requires a reason and the identity of the person making the correction. Changes are recorded in the record's `history`. Presence records require a punch-in; `ABSENT` and `LEAVE` records cannot carry punch times. A regularization request must change at least one field.

### Analytics

| Method | Route | Required parameters | Purpose |
|---|---|---|---|
| `GET` | `/analytics/employees/{emp_code}/monthly` | `month=YYYY-MM` | Monthly metrics for one employee |
| `GET` | `/analytics/departments/summary` | `month=YYYY-MM` | Department-level attendance and work-hour summary; optional `department` filter |
| `GET` | `/analytics/leaderboard/late` | `month=YYYY-MM` | Ranks employees by total late minutes; optional `limit` and `department` |
| `GET` | `/analytics/departments/{department}/trend` | `from=YYYY-MM-DD`, `to=YYYY-MM-DD` | Daily department attendance trend for a date range |

Example requests:

```http
GET /analytics/employees/EMP1001/monthly?month=2026-10
GET /analytics/departments/summary?month=2026-10
GET /analytics/leaderboard/late?month=2026-10&limit=10&department=Engineering
GET /analytics/departments/Engineering/trend?from=2026-10-01&to=2026-10-14
```

The late leaderboard limit must be between `1` and `50`. The department trend range must be valid, ordered, and no longer than 92 calendar days. The trend includes dates with no attendance records and weekends. The `attendance_rate` is represented as a ratio (for example, `0.95` means 95%), not a percentage from 0 to 100.

### Query explain endpoint

| Method | Route | Purpose |
|---|---|---|
| `GET` | `/admin/explain/{endpoint}` | Returns MongoDB `executionStats` explain output for a supported query/pipeline |

Supported `endpoint` values are `attendance_list`, `employee_monthly`, `department_summary`, `late_leaderboard`, and `department_trend`. Supply the parameters relevant to the selected endpoint, such as `emp_code` and `month` for `employee_monthly`, or `department`, `from`, and `to` for `department_trend`.

This route is intended for assessment/debugging and query-plan review. It is not authentication-protected in the current assessment implementation; do not expose it to untrusted clients in a production deployment without adding appropriate access controls or disabling it.

## Attendance calculation rules

The implementation centralizes attendance calculations in helper functions and uses MongoDB aggregation pipelines for analytics. Key behaviors include:

- **Timezone handling:** Instants are normalized to UTC for storage and interpreted using Indian Standard Time (IST, UTC+05:30) for attendance-date and shift calculations.
- **Epoch timestamps:** API timestamp inputs are epoch milliseconds and are truncated to whole seconds before storage.
- **Overnight shifts:** If a shift crosses midnight, early-morning punch-ins can be assigned to the date on which the shift started.
- **Late arrival:** A punch-in is late only when it is more than 10 minutes after the scheduled start. The reported lateness is whole minutes.
- **Work hours:** Work duration is rounded to two decimal places using decimal `ROUND_HALF_UP` semantics.
- **Overtime:** Overtime is counted only when time after the scheduled shift end reaches at least 30 minutes; counted overtime uses whole minutes.
- **Half-day:** A presence record is marked as half-day when its calculated work duration is below 4.50 hours.
- **Monthly attendance:** Working days exclude weekends. Half-day presence contributes `0.5` to present days; other presence statuses contribute `1`. Attendance percentage is based on present days divided by working days and is returned on a 0–100 scale.
- **Department trend:** The trend reports one row per requested calendar date. Attendance rates are ratios on a 0–1 scale and are `null` for non-working days or days with no eligible headcount. The seven-day moving average uses the current day and up to the preceding six daily rows, ignoring null attendance-rate values as MongoDB `$avg` does.

The exact response schema and validation errors are available through `/docs` and the route definitions in `app/main.py`.

## MongoDB indexes and aggregation

The application creates named indexes idempotently during startup. The main indexes include:

### `employees`

- Unique index on `emp_code` for employee identity and duplicate prevention.
- Index on `(department, emp_code)` for department-scoped employee queries.
- Index on `(department, joined_on)` and `(joined_on, department)` to support department and join-date access patterns.

### `attendance_logs`

- Unique compound index on `(emp_code, date)` to enforce one attendance record per employee per attendance date.
- Index on `(date DESC, emp_code ASC)` for attendance list sorting.
- Index on `(emp_code, punch_in DESC)` for punch-out record lookup.
- Indexes on date/status and employee/status/date combinations for common filters.

The monthly employee and department summaries, leaderboard, and department trend use MongoDB aggregation pipelines rather than fetching the entire attendance dataset into Python for calculation. The leaderboard uses MongoDB ranking so employees tied at the cutoff rank are retained. The department trend builds a calendar date series before joining daily data, so missing dates are represented in the output.

Index presence alone does not establish that every pipeline is optimally indexed. Use the explain endpoint and realistic data volumes to inspect execution plans and examined-document counts before drawing performance conclusions.

## Run tests

Make sure MongoDB is running and set the same environment variables used by the application. For example, in PowerShell:

```powershell
$env:MONGO_URI = "mongodb://localhost:27017"
$env:MONGO_DB = "attendance_db"
python -m pytest -v test_api.py
```

Or run the full discovered test suite with:

```bash
python -m pytest -q
```

The current test suite covers health and employee endpoints, valid employee creation, punch-in/out behavior, invalid punch-out cases, unknown employees, duplicate punch-in, regularization history, timestamp validation, date-range validation, monthly analytics, leaderboard ties, trend date filling, overnight shifts, and recalculation of derived fields during regularization.

Tests write test data to the configured MongoDB database. Use a dedicated local/test database rather than a database containing important data. The suite creates uniquely named employees for many test cases, so test records may remain in the database after a run.

## Troubleshooting

### `ModuleNotFoundError: No module named 'app'`

Run Uvicorn from the repository root—the directory containing the `app/` folder:

```bash
python -m uvicorn app.main:app --reload --port 8000
```

If the project is nested in another folder, change into the inner directory first.

### `MONGO_URI` or `MONGO_DB` is missing

Set both environment variables in the same terminal session in which you start Uvicorn or run the tests. Restart the process after changing the variables.

### `/health` returns `503`

Confirm MongoDB is running, the URI is reachable, and the configured database user has permission to create indexes. The API may start while MongoDB is unavailable so that `/health` can report the readiness failure.

### Port `8000` is already in use

Stop the process using the port or start Uvicorn on a different port, for example `--port 8001`.

### A test fails after earlier test runs

Check that the test environment variables point to the intended test database. Review the individual assertion and the API response before clearing database records; do not delete data from a database that contains information you need.

## Project decisions and review notes

- [`DECISIONS.md`](DECISIONS.md) records the main implementation choices, including index selection, punch-in concurrency, ranking ties, employees without logs, and possible scaling directions.
- [`REVIEW.md`](REVIEW.md) documents issues found in the starter implementation, how to reproduce them, and the corresponding fixes.
- [`test_api.py`](test_api.py) contains the automated API tests.

These documents are intended to support the assessment walkthrough. Be prepared to explain the implementation and make a small change to it yourself.

## Security and deployment notes

- Do not commit `.env` files, database credentials, access tokens, local virtual environments, cache files, or database dumps.
- Set `MONGO_URI` and `MONGO_DB` through the environment or a deployment secret manager.
- Use a restricted MongoDB user rather than an administrative account for an actual deployment.
- Add authentication and authorization before exposing employee data or regularization operations to real users.
- Protect or disable `/admin/explain/{endpoint}` outside controlled development/assessment environments.
- Configure transport security, logging, monitoring, and backup/retention policies before production use. This repository is not, by itself, a claim of production security certification.

## Assessment repository

Source repository: <https://github.com/Muheet-Mehraj/hrone-attendance-analytics-api>
