# Employee Attendance & Analytics API

FastAPI + MongoDB implementation for the HROne Software Engineer Trainee assignment. Application code is in `app/main.py`.

## Run locally

Use Python 3.11 or newer, Git, and MongoDB 6.0 or newer. Install dependencies and configure your own environment variables (do not commit a `.env` file):

```bash
python -m venv .venv
# Windows PowerShell: .venv\Scripts\Activate.ps1
# macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt

# PowerShell
$env:MONGO_URI = "mongodb://localhost:27017"
$env:MONGO_DB = "attendance_db"

# macOS/Linux alternatives:
# export MONGO_URI="mongodb://localhost:27017"
# export MONGO_DB="attendance_db"

uvicorn app.main:app --port 8000
```

The app creates its indexes idempotently and `/health` pings MongoDB. The API contract uses epoch milliseconds for instants and `YYYY-MM-DD` / `YYYY-MM` strings for calendar values. Read `DECISIONS.md` and `REVIEW.md` before the live walkthrough; be prepared to explain and change the implementation yourself.

## Submission notes

- Required files: `app/main.py`, `requirements.txt`, `REVIEW.md`, `DECISIONS.md`, and this `README.md`.
- No `.env`, secrets, virtual environment or Dockerfile should be committed.
- Functional testing against MongoDB is still required before submitting. In particular, test concurrent punch-in/punch-out requests, correction history races, mid-month joiners, employees with no logs, tied leaderboard ranks and trend gaps.
