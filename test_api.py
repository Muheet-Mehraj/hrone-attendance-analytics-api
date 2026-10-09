
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.main import app


client = TestClient(app)


# Helpers

def unique_emp_code() -> str:
    """Generate an employee code matching EMP followed by six digits."""
    return f"EMP{uuid4().int % 1_000_000:06d}"


def timestamp_ms(
    year: int,
    month: int,
    day: int,
    hour: int = 4,
    minute: int = 0,
) -> int:
    dt = datetime(
        year, month, day, hour, minute, tzinfo=timezone.utc
    )
    return int(dt.timestamp() * 1000)


def create_test_employee() -> str:
    emp_code = unique_emp_code()
    payload = {
        "emp_code": emp_code,
        "name": "Automated Test Employee",
        "email": f"{emp_code.lower()}@example.com",
        "department": "Testing",
        "joined_on": "2026-01-01",
    }

    response = client.post("/employees", json=payload)
    assert response.status_code == 201, response.text
    assert response.json()["emp_code"] == emp_code
    return emp_code


# Health and employee tests

def test_health_endpoint():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_employees_endpoint():
    response = client.get("/employees")
    assert response.status_code == 200
    assert isinstance(response.json(), (list, dict))


def test_employee_creation_accepts_valid_code():
    emp_code = create_test_employee()
    assert emp_code.startswith("EMP")
    assert len(emp_code) == 9


# Attendance tests

def test_attendance_punch_in_and_out():
    emp_code = create_test_employee()
    punch_in = timestamp_ms(2026, 10, 9, 4, 30)
    punch_out = punch_in + 8 * 60 * 60 * 1000

    in_response = client.post(
        "/attendance/punch-in",
        json={
            "emp_code": emp_code,
            "punched_at": punch_in,
            "status": "PRESENT",
        },
    )
    assert in_response.status_code == 201, in_response.text
    assert in_response.json()["punch_in"] == punch_in

    out_response = client.post(
        "/attendance/punch-out",
        json={"emp_code": emp_code, "punched_at": punch_out},
    )
    assert out_response.status_code == 200, out_response.text
    assert out_response.json()["punch_out"] == punch_out
    assert out_response.json()["work_hours"] == pytest.approx(8.0)


def test_punch_out_without_punch_in_is_rejected():
    response = client.post(
        "/attendance/punch-out",
        json={
            "emp_code": unique_emp_code(),
            "punched_at": timestamp_ms(2026, 10, 9, 12, 0),
        },
    )
    assert response.status_code == 404, response.text


def test_punch_in_for_unknown_employee_is_rejected():
    response = client.post(
        "/attendance/punch-in",
        json={
            "emp_code": unique_emp_code(),
            "punched_at": timestamp_ms(2026, 10, 9, 4, 30),
            "status": "PRESENT",
        },
    )
    assert response.status_code == 404, response.text


def test_duplicate_punch_in_returns_conflict():
    emp_code = create_test_employee()
    payload = {
        "emp_code": emp_code,
        "punched_at": timestamp_ms(2026, 10, 9, 4, 30),
        "status": "PRESENT",
    }

    first = client.post("/attendance/punch-in", json=payload)
    second = client.post("/attendance/punch-in", json=payload)

    assert first.status_code == 201, first.text
    assert second.status_code == 409, second.text


def test_regularization_appends_history():
    emp_code = create_test_employee()
    punch_in = timestamp_ms(2026, 10, 9, 4, 30)
    punch_out = punch_in + 8 * 60 * 60 * 1000

    response = client.post(
        "/attendance/punch-in",
        json={
            "emp_code": emp_code,
            "punched_at": punch_in,
            "status": "PRESENT",
        },
    )
    assert response.status_code == 201, response.text

    response = client.post(
        "/attendance/punch-out",
        json={"emp_code": emp_code, "punched_at": punch_out},
    )
    assert response.status_code == 200, response.text

    url = f"/attendance/{emp_code}/2026-10-09"

    first = client.patch(
        url,
        json={
            "status": "WFH",
            "reason": "First correction",
            "regularized_by": "Automated test",
        },
    )
    assert first.status_code == 200, first.text
    assert len(first.json()["history"]) == 1
    assert first.json()["history"][0]["changes"]["status"] == {
        "from": "PRESENT",
        "to": "WFH",
    }

    second = client.patch(
        url,
        json={
            "status": "ON_DUTY",
            "reason": "Second correction",
            "regularized_by": "Automated test",
        },
    )
    assert second.status_code == 200, second.text
    assert len(second.json()["history"]) == 2
    assert second.json()["history"][1]["changes"]["status"] == {
        "from": "WFH",
        "to": "ON_DUTY",
    }


def test_timestamp_in_seconds_is_rejected():
    emp_code = create_test_employee()

    response = client.post(
        "/attendance/punch-in",
        json={
            "emp_code": emp_code,
            "punched_at": 1791547800,
            "status": "PRESENT",
        },
    )
    assert response.status_code == 422, response.text


def test_invalid_attendance_date_range_is_rejected():
    response = client.get(
        "/attendance",
        params={
            "date_from": "2026-10-10",
            "date_to": "2026-10-09",
        },
    )
    assert response.status_code == 422, response.text


def test_monthly_analytics_for_one_full_attendance_day():
    emp_code = create_test_employee()

    # October 9, 2026: 10:00 AM IST = 04:30 UTC.
    punch_in = timestamp_ms(2026, 10, 9, 4, 30)

    # 6:00 PM IST = 12:30 UTC: exactly 8 hours worked.
    punch_out = punch_in + 8 * 60 * 60 * 1000

    in_response = client.post(
        "/attendance/punch-in",
        json={
            "emp_code": emp_code,
            "punched_at": punch_in,
            "status": "PRESENT",
        },
    )
    assert in_response.status_code == 201, in_response.text

    out_response = client.post(
        "/attendance/punch-out",
        json={
            "emp_code": emp_code,
            "punched_at": punch_out,
        },
    )
    assert out_response.status_code == 200, out_response.text

    response = client.get(
        f"/analytics/employees/{emp_code}/monthly",
        params={"month": "2026-10"},
    )

    assert response.status_code == 200, response.text

    data = response.json()

    # October 2026 has 22 weekdays.
    assert data["emp_code"] == emp_code
    assert data["month"] == "2026-10"
    assert data["working_days"] == 22

    # One full present day, with a late arrival.
    assert data["present_days"] == 1.0
    assert data["late_count"] == 1
    assert data["total_late_minutes"] == 30
    assert data["total_overtime_minutes"] == 0
    assert data["leave_days"] == 0
    assert data["attendance_pct"] == 4.5455

# -------------------------------------------------------------------
# Contract tests: ranking, trend, overnight shifts, regularization
# -------------------------------------------------------------------

def create_employee_for_contract_case(
    department: str,
    joined_on: str = "2026-01-01",
    shift_start: str = "09:30",
    shift_end: str = "18:30",
) -> str:
    """Create a unique employee with configurable department and shift."""
    emp_code = unique_emp_code()

    response = client.post(
        "/employees",
        json={
            "emp_code": emp_code,
            "name": "Contract Test Employee",
            "email": f"{emp_code.lower()}@example.com",
            "department": department,
            "joined_on": joined_on,
            "shift_start": shift_start,
            "shift_end": shift_end,
        },
    )
    assert response.status_code == 201, response.text
    return emp_code


def test_late_leaderboard_includes_ties_at_limit():
    department = f"RankTest{uuid4().hex[:8]}"

    top_code = create_employee_for_contract_case(department)
    tie_code_a = create_employee_for_contract_case(department)
    tie_code_b = create_employee_for_contract_case(department)

    # 10:00 IST = 30 minutes late; 09:50 IST = 20 minutes late.
    punches = [
        (top_code, timestamp_ms(2026, 10, 9, 4, 30)),
        (tie_code_a, timestamp_ms(2026, 10, 9, 4, 20)),
        (tie_code_b, timestamp_ms(2026, 10, 9, 4, 20)),
    ]

    for emp_code, punched_at in punches:
        response = client.post(
            "/attendance/punch-in",
            json={
                "emp_code": emp_code,
                "punched_at": punched_at,
                "status": "PRESENT",
            },
        )
        assert response.status_code == 201, response.text

    response = client.get(
        "/analytics/leaderboard/late",
        params={
            "month": "2026-10",
            "limit": 2,
            "department": department,
        },
    )
    assert response.status_code == 200, response.text

    items = response.json()["items"]

    # Limit applies after ranking, so rank-2 ties are both returned.
    assert len(items) == 3
    assert [item["rank"] for item in items] == [1, 2, 2]
    assert [item["total_late_minutes"] for item in items] == [30, 20, 20]
    assert [item["emp_code"] for item in items] == [
        top_code,
        *sorted([tie_code_a, tie_code_b]),
    ]


def test_department_trend_fills_weekends_and_missing_days():
    department = f"TrendTest{uuid4().hex[:8]}"
    emp_code = create_employee_for_contract_case(department)

    # One record on Friday at 09:30 IST; no records on Saturday,
    # Sunday, or Monday.
    response = client.post(
        "/attendance/punch-in",
        json={
            "emp_code": emp_code,
            "punched_at": timestamp_ms(2026, 10, 9, 4, 0),
            "status": "PRESENT",
        },
    )
    assert response.status_code == 201, response.text

    response = client.get(
        f"/analytics/departments/{department}/trend",
        params={"from": "2026-10-09", "to": "2026-10-12"},
    )
    assert response.status_code == 200, response.text

    items = response.json()["items"]

    assert [item["date"] for item in items] == [
        "2026-10-09",
        "2026-10-10",
        "2026-10-11",
        "2026-10-12",
    ]
    assert all(item["headcount"] == 1 for item in items)

    friday, saturday, sunday, monday = items

    assert friday["is_working_day"] is True
    assert friday["present_count"] == 1
    assert friday["attendance_rate"] == 1.0

    for item in (saturday, sunday):
        assert item["is_working_day"] is False
        assert item["attendance_rate"] is None

    assert monday["is_working_day"] is True
    assert monday["present_count"] == 0
    assert monday["attendance_rate"] == 0.0

    # The moving average ignores null rates in its window.
    assert friday["moving_avg_7d"] == 1.0
    assert saturday["moving_avg_7d"] == 1.0
    assert sunday["moving_avg_7d"] == 1.0
    assert monday["moving_avg_7d"] == 0.5


def test_overnight_shift_uses_shift_start_attendance_date():
    department = f"NightTest{uuid4().hex[:8]}"
    emp_code = create_employee_for_contract_case(
        department,
        shift_start="22:00",
        shift_end="06:00",
    )

    # 00:30 IST on October 10 belongs to the October 9 overnight shift.
    punch_in = timestamp_ms(2026, 10, 9, 19, 0)
    punch_out = timestamp_ms(2026, 10, 10, 1, 10)

    response = client.post(
        "/attendance/punch-in",
        json={
            "emp_code": emp_code,
            "punched_at": punch_in,
            "status": "PRESENT",
        },
    )
    assert response.status_code == 201, response.text
    assert response.json()["date"] == "2026-10-09"
    assert response.json()["late_minutes"] == 150

    response = client.post(
        "/attendance/punch-out",
        json={"emp_code": emp_code, "punched_at": punch_out},
    )
    assert response.status_code == 200, response.text

    record = response.json()
    assert record["date"] == "2026-10-09"
    assert record["work_hours"] == pytest.approx(6.17)
    assert record["overtime_minutes"] == 40
    assert record["half_day"] is False


def test_regularization_recalculates_derived_fields():
    department = f"RegTest{uuid4().hex[:8]}"
    emp_code = create_employee_for_contract_case(department)

    # Original: 10:05-19:10 IST = 9.08 hours, 35 minutes late.
    original_in = timestamp_ms(2026, 10, 9, 4, 35)
    punch_out = timestamp_ms(2026, 10, 9, 13, 40)

    response = client.post(
        "/attendance/punch-in",
        json={
            "emp_code": emp_code,
            "punched_at": original_in,
            "status": "PRESENT",
        },
    )
    assert response.status_code == 201, response.text

    response = client.post(
        "/attendance/punch-out",
        json={"emp_code": emp_code, "punched_at": punch_out},
    )
    assert response.status_code == 200, response.text
    assert response.json()["late_minutes"] == 35
    assert response.json()["work_hours"] == pytest.approx(9.08)

    # Correct punch-in to 09:28 IST.
    corrected_in = timestamp_ms(2026, 10, 9, 3, 58)

    response = client.patch(
        f"/attendance/{emp_code}/2026-10-09",
        json={
            "punch_in": corrected_in,
            "reason": "Corrected biometric timestamp",
            "regularized_by": "Automated test",
        },
    )
    assert response.status_code == 200, response.text

    record = response.json()
    assert record["punch_in"] == corrected_in
    assert record["late_minutes"] == 0
    assert record["work_hours"] == pytest.approx(9.70)
    assert record["overtime_minutes"] == 40
    assert len(record["history"]) == 1

    changes = record["history"][0]["changes"]
    assert changes["punch_in"] == {
        "from": original_in,
        "to": corrected_in,
    }
    assert changes["late_minutes"] == {"from": 35, "to": 0}
    assert changes["work_hours"] == {"from": 9.08, "to": 9.7}
