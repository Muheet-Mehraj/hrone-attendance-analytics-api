"""HROne Employee Attendance & Analytics API.
Run from the repository root with:
    uvicorn app.main:app --port 8000
Configuration is read from MONGO_URI and MONGO_DB. A local .env is convenient for
manual development, but real environment variables take precedence.
"""
from __future__ import annotations

import calendar
import os
import re
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Annotated, Any, Literal, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query
from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError
from pymongo.collection import Collection
from bson import json_util

load_dotenv()
MONGO_URI = os.getenv("MONGO_URI")
MONGO_DB = os.getenv("MONGO_DB")
if not MONGO_URI or not MONGO_DB:
    raise RuntimeError("Both MONGO_URI and MONGO_DB environment variables must be set")
client = MongoClient(MONGO_URI, tz_aware=True, serverSelectionTimeoutMS=5000)
db = client[MONGO_DB]
_indexes_ready = False
UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
PRESENCE_STATUSES = ("PRESENT", "WFH", "ON_DUTY")
ALL_STATUSES = ("PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY")
EPOCH_MS_MIN = 100_000_000_000
EPOCH_MS_MAX = 4_102_444_800_000


@asynccontextmanager
async def lifespan(_: FastAPI):
    """Prepare MongoDB indexes when the API starts."""
    ensure_indexes()
    yield


app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    lifespan=lifespan,
)


# ---------------------------------------------------------------------------
# Validation and time helpers


# ---------------------------------------------------------------------------
EpochMillis = Annotated[StrictInt, Field(ge=EPOCH_MS_MIN, le=EPOCH_MS_MAX)]
MonthParam = Annotated[str, Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")]
DateString = Annotated[str, Field(pattern=r"^\d{4}-\d{2}-\d{2}$")]
ShiftTime = Annotated[str, Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")]


def _parse_date(value: str, *, field_name: str = "date") -> date:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise HTTPException(422, f"{field_name} must be YYYY-MM-DD")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise HTTPException(422, f"{field_name} must be a valid calendar date") from exc
    if parsed.isoformat() != value:
        raise HTTPException(422, f"{field_name} must be YYYY-MM-DD")
    return parsed


def _month_bounds(month: str) -> tuple[str, str]:
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month or ""):
        raise HTTPException(422, "month must be YYYY-MM")
    year, mon = map(int, month.split("-"))
    last_day = calendar.monthrange(year, mon)[1]
    return f"{month}-01", f"{month}-{last_day:02d}"


def _parse_time(value: str) -> time:
    # Pydantic validates incoming shift formats; stored data is validated here too.
    if not isinstance(value, str) or not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", value):
        raise HTTPException(422, "shift times must use HH:MM")
    hour, minute = map(int, value.split(":"))
    return time(hour, minute)


def _now_utc_seconds() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def _from_epoch_ms(value: int) -> datetime:
    # Store instants at whole-second precision for consistent comparisons.
    try:
        return datetime.fromtimestamp(value / 1000, tz=UTC).replace(microsecond=0)
    except (OverflowError, OSError, ValueError) as exc:
        raise HTTPException(422, "timestamp is outside the supported range") from exc


def _as_utc(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).replace(microsecond=0)


def _to_epoch_ms(value: Optional[datetime]) -> Optional[int]:
    value = _as_utc(value)
    if value is None:
        return None
    return int(value.timestamp()) * 1000


def _as_ist(value: datetime) -> datetime:
    normalized = _as_utc(value)
    assert normalized is not None
    return normalized.astimezone(IST)


def _is_overnight(employee: dict) -> bool:
    return _parse_time(employee["shift_end"]) <= _parse_time(employee["shift_start"])


def _attendance_date(punch_in: datetime, employee: dict) -> str:
    local = _as_ist(punch_in)
    if _is_overnight(employee) and local.timetz().replace(tzinfo=None) < _parse_time(employee["shift_end"]):
        return (local.date() - timedelta(days=1)).isoformat()
    return local.date().isoformat()


def _shift_start_datetime(attendance_date: str, employee: dict) -> datetime:
    day = _parse_date(attendance_date, field_name="attendance date")
    return datetime.combine(day, _parse_time(employee["shift_start"]), tzinfo=IST)


def _shift_end_datetime(attendance_date: str, employee: dict) -> datetime:
    day = _parse_date(attendance_date, field_name="attendance date")
    start = _parse_time(employee["shift_start"])
    end = _parse_time(employee["shift_end"])
    if end <= start:
        day += timedelta(days=1)
    return datetime.combine(day, end, tzinfo=IST)


def _compute_late_minutes(punch_in: Optional[datetime], attendance_date: str, employee: dict) -> int:
    if punch_in is None:
        return 0
    local_in = _as_ist(punch_in)
    expected_start = _shift_start_datetime(attendance_date, employee)
    elapsed_seconds = int((local_in - expected_start).total_seconds())
    # A punch-in is late only strictly after the full ten-minute grace period.
    if elapsed_seconds > 10 * 60:
        return max(0, elapsed_seconds // 60)
    return 0


def _round_half_up(value: Decimal, places: int = 2) -> Decimal:
    quantum = Decimal("1").scaleb(-places)
    return value.quantize(quantum, rounding=ROUND_HALF_UP)


def _compute_work_hours(punch_in: Optional[datetime], punch_out: Optional[datetime]) -> Optional[float]:
    if punch_in is None or punch_out is None:
        return None
    seconds = int((punch_out - punch_in).total_seconds())
    return float(_round_half_up(Decimal(seconds) / Decimal(3600), 2))


def _compute_overtime_minutes(punch_out: Optional[datetime], attendance_date: str, employee: dict) -> int:
    if punch_out is None:
        return 0
    expected_end = _shift_end_datetime(attendance_date, employee).astimezone(UTC)
    elapsed_seconds = int((_as_utc(punch_out) - expected_end).total_seconds())  # type: ignore[operator]
    whole_minutes = elapsed_seconds // 60
    return whole_minutes if whole_minutes >= 30 else 0


def _derived_values(
    status: str,
    attendance_date: str,
    punch_in: Optional[datetime],
    punch_out: Optional[datetime],
    employee: dict,
) -> dict:
    if status not in PRESENCE_STATUSES:
        return {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    late = _compute_late_minutes(punch_in, attendance_date, employee)
    hours = _compute_work_hours(punch_in, punch_out)
    overtime = _compute_overtime_minutes(punch_out, attendance_date, employee) if punch_out is not None else 0
    half_day = hours is not None and Decimal(str(hours)) < Decimal("4.50")
    return {
        "work_hours": hours,
        "late_minutes": late,
        "overtime_minutes": overtime,
        "half_day": bool(half_day),
    }


def _employee_response(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": _to_epoch_ms(doc["created_at"]),
    }


def _attendance_response(doc: dict) -> dict:
    output = {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": _to_epoch_ms(doc.get("punch_in")),
        "punch_out": _to_epoch_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": int(doc.get("late_minutes", 0) or 0),
        "overtime_minutes": int(doc.get("overtime_minutes", 0) or 0),
        "half_day": bool(doc.get("half_day", False)),
        "history": [],
    }
    for entry in doc.get("history", []) or []:
        changes = {}
        for field, change in (entry.get("changes") or {}).items():
            old_value, new_value = change.get("from"), change.get("to")
            if field in ("punch_in", "punch_out"):
                old_value = _to_epoch_ms(old_value) if isinstance(old_value, datetime) else old_value
                new_value = _to_epoch_ms(new_value) if isinstance(new_value, datetime) else new_value
            changes[field] = {"from": old_value, "to": new_value}
        output["history"].append({
            "at": _to_epoch_ms(entry.get("at")),
            "by": entry.get("by", ""),
            "reason": entry.get("reason", ""),
            "changes": changes,
        })
    return output


def _round_expr(expr, places: int):
    """Mongo expression for non-negative ROUND_HALF_UP (R8).

    Doubles cannot represent values like 1.005 exactly, so floor(x*100 + 0.5) can round
    the wrong way. The arithmetic is done in Decimal128 (which holds 1.005 as 1.005)
    and only the final result is converted back to a double for the JSON response.
    """
    scale = 10 ** places
    shifted = {"$add": [{"$multiply": [{"$toDecimal": expr}, scale]}, {"$toDecimal": "0.5"}]}
    return {"$toDouble": {"$divide": [{"$trunc": shifted}, scale]}}


def _working_weekday_expr(date_expr):
    weekday = {"$dayOfWeek": date_expr}  # MongoDB: Sunday=1, Saturday=7
    return {"$and": [{"$ne": [weekday, 1]}, {"$ne": [weekday, 7]}]}


# ---------------------------------------------------------------------------
# Request models


# ---------------------------------------------------------------------------


class EmployeeCreate(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: ShiftTime = "09:30"
    shift_end: ShiftTime = "18:30"
    joined_on: DateString
    @field_validator("joined_on")
    @classmethod
    def validate_joined_on(cls, value: str) -> str:
        try:
            date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("joined_on must be a valid calendar date") from exc
        return value
    @model_validator(mode="after")
    def validate_shift(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str
    punched_at: Optional[EpochMillis] = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"
    @model_validator(mode="before")
    @classmethod
    def reject_explicit_null_timestamp(cls, values):
        if isinstance(values, dict) and "punched_at" in values and values["punched_at"] is None:
            raise ValueError("punched_at must be an epoch-millisecond integer when supplied")
        return values


class PunchOutRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str
    punched_at: Optional[EpochMillis] = None
    @model_validator(mode="before")
    @classmethod
    def reject_explicit_null_timestamp(cls, values):
        if isinstance(values, dict) and "punched_at" in values and values["punched_at"] is None:
            raise ValueError("punched_at must be an epoch-millisecond integer when supplied")
        return values


class RegularizeRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None
    punch_in: Optional[EpochMillis] = None
    punch_out: Optional[EpochMillis] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)
    @model_validator(mode="before")
    @classmethod
    def reject_null_patch_fields(cls, values):
        if isinstance(values, dict):
            for field_name in ("status", "punch_in", "punch_out"):
                if field_name in values and values[field_name] is None:
                    raise ValueError(f"{field_name} cannot be null when supplied")
        return values


# ---------------------------------------------------------------------------
# Response models (documentation + output validation; shapes follow openapi.yaml)
# ---------------------------------------------------------------------------


class ErrorBody(BaseModel):
    detail: str


class EmployeeOut(BaseModel):
    emp_code: str
    name: str
    email: str
    department: str
    shift_start: str
    shift_end: str
    joined_on: str
    created_at: int


class EmployeePage(BaseModel):
    items: list[EmployeeOut]
    total: int
    page: int
    page_size: int


class HistoryEntryOut(BaseModel):
    at: int
    by: str
    reason: str
    changes: dict[str, dict[str, Any]]


class AttendanceOut(BaseModel):
    emp_code: str
    date: str
    status: str
    punch_in: Optional[int]
    punch_out: Optional[int]
    work_hours: Optional[float]
    late_minutes: int
    overtime_minutes: int
    half_day: bool
    history: list[HistoryEntryOut]


class AttendancePage(BaseModel):
    items: list[AttendanceOut]
    total: int
    page: int
    page_size: int


class EmployeeMonthlyOut(BaseModel):
    emp_code: str
    month: str
    working_days: int
    present_days: float
    leave_days: int
    late_count: int
    total_late_minutes: int
    total_overtime_minutes: int
    attendance_pct: Optional[float]


class DepartmentSummaryItem(BaseModel):
    department: str
    headcount: int
    present_days: float
    avg_work_hours: Optional[float]
    late_count: int
    total_late_minutes: int
    leave_count: int
    on_duty_count: int


class DepartmentSummaryOut(BaseModel):
    month: str
    items: list[DepartmentSummaryItem]


class LeaderboardItem(BaseModel):
    rank: int
    emp_code: str
    name: str
    department: str
    total_late_minutes: int
    late_count: int


class LeaderboardOut(BaseModel):
    month: str
    items: list[LeaderboardItem]


class TrendItem(BaseModel):
    date: str
    is_working_day: bool
    headcount: int
    present_count: float
    late_count: int
    attendance_rate: Optional[float]
    moving_avg_7d: Optional[float]


class TrendOut(BaseModel):
    department: str
    items: list[TrendItem]


class ExplainOut(BaseModel):
    endpoint: str
    collection: str
    explain: dict[str, Any]


# ---------------------------------------------------------------------------
# Indexes and readiness


# ---------------------------------------------------------------------------


def ensure_indexes() -> None:
    """Create the required indexes; let /health report when MongoDB is unavailable."""
    global _indexes_ready
    try:
        db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="ux_employees_emp_code")
        db.employees.create_index([("department", ASCENDING), ("emp_code", ASCENDING)], name="ix_employees_department_emp")
        db.employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)], name="ix_employees_department_joined")
        db.employees.create_index([("joined_on", ASCENDING), ("department", ASCENDING)], name="ix_employees_joined_department")
        db.attendance_logs.create_index(
            [("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="ux_attendance_emp_date"
        )
        db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="ix_attendance_date_emp")
        db.attendance_logs.create_index([("emp_code", ASCENDING), ("punch_in", DESCENDING)], name="ix_attendance_emp_punchin")
        db.attendance_logs.create_index(
            [("date", ASCENDING), ("status", ASCENDING), ("emp_code", ASCENDING)], name="ix_attendance_date_status_emp"
        )
        db.attendance_logs.create_index(
            [("emp_code", ASCENDING), ("status", ASCENDING), ("date", DESCENDING)], name="ix_attendance_emp_status_date"
        )
        _indexes_ready = True
    except PyMongoError:
        # /health retries index creation once MongoDB becomes reachable.
        _indexes_ready = False


@app.get(
    "/health",
    responses={503: {"model": ErrorBody, "description": "MongoDB is not ready"}},
)

def health():
    try:
        db.command("ping")
        if not _indexes_ready:
            ensure_indexes()
        if not _indexes_ready:
            raise HTTPException(503, "MongoDB indexes are not ready")
    except PyMongoError as exc:
        raise HTTPException(503, "MongoDB is not ready") from exc
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Employee endpoints


# ---------------------------------------------------------------------------


@app.post(
    "/employees",
    status_code=201,
    response_model=EmployeeOut,
    responses={409: {"model": ErrorBody, "description": "Employee code already exists"}},
)

def create_employee(body: EmployeeCreate):
    doc = body.model_dump()
    doc["created_at"] = _now_utc_seconds()
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError as exc:
        raise HTTPException(409, "emp_code already exists") from exc
    return _employee_response(doc)


@app.get("/employees", response_model=EmployeePage)

def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    query = {"department": department} if department is not None else {}
    total = db.employees.count_documents(query)
    docs = db.employees.find(query, {"_id": 0}).sort("emp_code", ASCENDING).skip((page - 1) * page_size).limit(page_size)
    return {"items": [_employee_response(d) for d in docs], "total": total, "page": page, "page_size": page_size}


# ---------------------------------------------------------------------------
# Attendance endpoints


# ---------------------------------------------------------------------------


def _attendance_query(
    emp_code: Optional[str], date_from: Optional[str], date_to: Optional[str], status: Optional[str]
) -> dict:
    query: dict = {}
    if emp_code is not None:
        query["emp_code"] = emp_code
    if date_from is not None:
        _parse_date(date_from, field_name="date_from")
    if date_to is not None:
        _parse_date(date_to, field_name="date_to")
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from must be on or before date_to")
    if date_from or date_to:
        date_query = {}
        if date_from:
            date_query["$gte"] = date_from
        if date_to:
            date_query["$lte"] = date_to
        query["date"] = date_query
    if status is not None:
        query["status"] = status
    return query


def _attendance_sort() -> list[tuple[str, int]]:
    return [("date", DESCENDING), ("emp_code", ASCENDING)]


@app.post(
    "/attendance/punch-in",
    status_code=201,
    response_model=AttendanceOut,
    responses={
        404: {"model": ErrorBody, "description": "Employee not found"},
        409: {"model": ErrorBody, "description": "Already punched in for this date"},
    },
)

def punch_in(body: PunchInRequest):
    employee = db.employees.find_one({"emp_code": body.emp_code})
    if employee is None:
        raise HTTPException(404, "employee not found")
    punched_at = _from_epoch_ms(body.punched_at) if body.punched_at is not None else _now_utc_seconds()
    attendance_day = _attendance_date(punched_at, employee)
    doc = {
        "emp_code": body.emp_code,
        "date": attendance_day,
        "status": body.status,
        "punch_in": punched_at,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": _compute_late_minutes(punched_at, attendance_day, employee),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError as exc:
        # The unique (emp_code, date) index ensures only one concurrent request wins.
        raise HTTPException(409, "already punched in for this date") from exc
    return _attendance_response(doc)


@app.post(
    "/attendance/punch-out",
    response_model=AttendanceOut,
    responses={
        404: {"model": ErrorBody, "description": "Employee or punch-in record not found"},
        409: {"model": ErrorBody, "description": "Attendance record already punched out"},
    },
)

def punch_out(body: PunchOutRequest):
    employee = db.employees.find_one({"emp_code": body.emp_code})
    if employee is None:
        raise HTTPException(404, "employee not found")
    punched_at = _from_epoch_ms(body.punched_at) if body.punched_at is not None else _now_utc_seconds()
    # Find the latest punch-in that occurred at or before this punch-out.
    record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$ne": None, "$lte": punched_at}},
        sort=[("punch_in", DESCENDING)],
    )
    if record is None:
        # Distinguish an invalid earlier timestamp from an employee with no punches at all.
        latest = db.attendance_logs.find_one(
            {"emp_code": body.emp_code, "punch_in": {"$ne": None}}, sort=[("punch_in", DESCENDING)]
        )
        if latest is not None and punched_at <= _as_utc(latest["punch_in"]):
            raise HTTPException(422, "punched_at must be after punch_in")
        raise HTTPException(404, "no punch-in found")
    if record.get("punch_out") is not None:
        raise HTTPException(409, "record is already punched out")
    punch_in_at = _as_utc(record["punch_in"])
    assert punch_in_at is not None
    elapsed = punched_at - punch_in_at
    if elapsed.total_seconds() <= 0:
        raise HTTPException(422, "punched_at must be after punch_in")
    if elapsed > timedelta(hours=24):
        raise HTTPException(422, "punch-out cannot be more than 24 hours after punch-in")
    derived = _derived_values(record["status"], record["date"], punch_in_at, punched_at, employee)
    updated = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},
        {"$set": {"punch_out": punched_at, **derived}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        # A simultaneous request won the atomic update; never overwrite its punch-out.
        raise HTTPException(409, "record is already punched out")
    return _attendance_response(updated)


@app.get("/attendance", response_model=AttendancePage)

def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    query = _attendance_query(emp_code, date_from, date_to, status)
    total = db.attendance_logs.count_documents(query)
    docs = db.attendance_logs.find(query).sort(_attendance_sort()).skip((page - 1) * page_size).limit(page_size)
    return {"items": [_attendance_response(d) for d in docs], "total": total, "page": page, "page_size": page_size}


@app.patch(
    "/attendance/{emp_code}/{date}",
    response_model=AttendanceOut,
    responses={
        404: {"model": ErrorBody, "description": "Employee or attendance record not found"},
        409: {"model": ErrorBody, "description": "Concurrent regularization conflict"},
    },
)

def regularize_attendance(
    emp_code: str,
    date: str = Path(pattern=r"^\d{4}-\d{2}-\d{2}$"),  # must match {date} in the route
    body: RegularizeRequest = ...,
):
    # NOTE: `date` shadows datetime.date inside this function only; use _parse_date() here.
    _parse_date(date, field_name="date")
    employee = db.employees.find_one({"emp_code": emp_code})
    if employee is None:
        raise HTTPException(404, "employee not found")
    current = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if current is None:
        raise HTTPException(404, "attendance record not found")
    old_status = current.get("status")
    new_status = body.status if body.status is not None else old_status
    old_in = _as_utc(current.get("punch_in"))
    old_out = _as_utc(current.get("punch_out"))
    new_in = _from_epoch_ms(body.punch_in) if body.punch_in is not None else old_in
    new_out = _from_epoch_ms(body.punch_out) if body.punch_out is not None else old_out
    requested_time_supplied = body.punch_in is not None or body.punch_out is not None
    if new_status in ("ABSENT", "LEAVE"):
        if requested_time_supplied:
            raise HTTPException(422, "ABSENT and LEAVE records cannot be given punch times")
        new_in = None
        new_out = None
    else:
        if new_in is None:
            raise HTTPException(422, "a presence status requires punch_in")
        if _attendance_date(new_in, employee) != date:
            raise HTTPException(422, "punch_in must belong to the record's attendance date")
        if new_out is not None:
            elapsed_seconds = (new_out - new_in).total_seconds()
            if elapsed_seconds <= 0:
                raise HTTPException(422, "punch_out must be after punch_in")
            if elapsed_seconds > 24 * 60 * 60:
                raise HTTPException(422, "punch_out cannot be more than 24 hours after punch_in")
    computed = _derived_values(new_status, date, new_in, new_out, employee)
    old_values = {
        "status": old_status,
        "punch_in": old_in,
        "punch_out": old_out,
        "work_hours": current.get("work_hours"),
        "late_minutes": int(current.get("late_minutes", 0) or 0),
        "overtime_minutes": int(current.get("overtime_minutes", 0) or 0),
        "half_day": bool(current.get("half_day", False)),
    }
    new_values = {
        "status": new_status,
        "punch_in": new_in,
        "punch_out": new_out,
        **computed,
    }
    changes: dict = {}
    for field_name, new_value in new_values.items():
        old_value = old_values[field_name]
        # Compare normalized BSON datetimes, not their API representations.
        if field_name in ("punch_in", "punch_out"):
            old_value = _as_utc(old_value) if old_value is not None else None
            new_value = _as_utc(new_value) if new_value is not None else None
        if old_value != new_value:
            changes[field_name] = {"from": old_value, "to": new_value}
    if not changes:
        raise HTTPException(422, "regularization must change at least one field")
    at = _now_utc_seconds()
    history_entry = {
        "at": at,
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }
    set_values = {field_name: new_values[field_name] for field_name in new_values}
    # The $expr compares history length atomically. A concurrent PATCH increments the
    # length, so another request based on the stale document cannot lose an audit entry.
    expected_history_length = len(current.get("history", []) or [])
    update_filter = {
        "_id": current["_id"],
        "status": current.get("status"),
        "punch_in": current.get("punch_in"),
        "punch_out": current.get("punch_out"),
        "$expr": {"$eq": [{"$size": {"$ifNull": ["$history", []]}}, expected_history_length]},
    }
    updated = db.attendance_logs.find_one_and_update(
        update_filter,
        {"$set": set_values, "$push": {"history": history_entry}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "record was changed by another regularization; retry")
    return _attendance_response(updated)


# ---------------------------------------------------------------------------
# MongoDB aggregation pipelines for analytics


# ---------------------------------------------------------------------------


def _employee_monthly_pipeline(emp_code: str, start: str, end: str) -> list[dict]:
    working_start_expr = {
        "$dateFromString": {"dateString": {"$cond": [{"$gt": ["$joined_on", start]}, "$joined_on", start]}}
    }
    month_end_exclusive = {"$dateAdd": {"startDate": {"$dateFromString": {"dateString": end}}, "unit": "day", "amount": 1}}
    day_count = {"$max": [0, {"$dateDiff": {"startDate": "$__working_start", "endDate": "$__month_end_exclusive", "unit": "day"}}]}
    working_dates = {"$map": {
        "input": {"$range": [0, day_count]}, "as": "n",
        "in": {"$dateAdd": {"startDate": "$__working_start", "unit": "day", "amount": "$$n"}},
    }}
    log_date = {"$dateFromString": {"dateString": "$$log.date"}}
    present_condition = {"$and": [{"$in": ["$$log.status", list(PRESENCE_STATUSES)]}, _working_weekday_expr(log_date)]}
    return [
        {"$match": {"emp_code": emp_code}},
        {"$lookup": {
            "from": "attendance_logs",
            "let": {"code": "$emp_code"},
            "pipeline": [{"$match": {"$expr": {"$and": [
                {"$eq": ["$emp_code", "$$code"]}, {"$gte": ["$date", start]}, {"$lte": ["$date", end]},
            ]}}}],
            "as": "__logs",
        }},
        {"$addFields": {"__working_start": working_start_expr, "__month_end_exclusive": month_end_exclusive}},
        {"$addFields": {
            "working_days": {"$size": {"$filter": {
                "input": working_dates, "as": "day", "cond": _working_weekday_expr("$$day"),
            }}},
            "present_days": {"$sum": {"$map": {
                "input": "$__logs", "as": "log",
                "in": {"$cond": [present_condition, {"$cond": [{"$eq": [{"$ifNull": ["$$log.half_day", False]}, True]}, 0.5, 1]}, 0]},
            }}},
            "leave_days": {"$size": {"$filter": {"input": "$__logs", "as": "log", "cond": {"$eq": ["$$log.status", "LEAVE"]}}}},
            "late_count": {"$sum": {"$map": {"input": "$__logs", "as": "log", "in": {"$cond": [{"$gt": [{"$ifNull": ["$$log.late_minutes", 0]}, 0]}, 1, 0]}}}},
            "total_late_minutes": {"$sum": {"$map": {"input": "$__logs", "as": "log", "in": {"$ifNull": ["$$log.late_minutes", 0]}}}},
            "total_overtime_minutes": {"$sum": {"$map": {"input": "$__logs", "as": "log", "in": {"$ifNull": ["$$log.overtime_minutes", 0]}}}},
        }},
        {"$addFields": {"attendance_pct": {"$cond": [
            {"$gt": ["$working_days", 0]},
            _round_expr({"$multiply": [{"$divide": ["$present_days", "$working_days"]}, 100]}, 4),
            None,
        ]}}},
        {"$project": {
            "_id": 0, "emp_code": 1, "working_days": 1,
            "present_days": _round_expr("$present_days", 2),
            "leave_days": 1, "late_count": 1, "total_late_minutes": 1,
            "total_overtime_minutes": 1, "attendance_pct": 1,
        }},
    ]


def _department_summary_pipeline(start: str, end: str, department: Optional[str]) -> list[dict]:
    root_match: dict = {"joined_on": {"$lte": end}}
    if department is not None:
        root_match["department"] = department
    lookup_logs = {"$lookup": {
        "from": "attendance_logs",
        "let": {"code": "$emp_code"},
        "pipeline": [{"$match": {"$expr": {"$and": [
            {"$eq": ["$emp_code", "$$code"]}, {"$gte": ["$date", start]}, {"$lte": ["$date", end]},
        ]}}}],
        "as": "__logs",
    }}
    weekday_date = {"$dateFromString": {"dateString": "$$log.date"}}
    present_cond = {"$and": [{"$in": ["$$log.status", list(PRESENCE_STATUSES)]}, _working_weekday_expr(weekday_date)]}
    hours_filter = {"$filter": {"input": "$__logs", "as": "log", "cond": {
        "$and": [
            {"$in": ["$$log.status", list(PRESENCE_STATUSES)]},
            {"$ne": [{"$ifNull": ["$$log.work_hours", None]}, None]},
        ]
    }}}
    stages = [
        {"$match": root_match},
        lookup_logs,
        {"$addFields": {
            "__present_days": {"$sum": {"$map": {
                "input": "$__logs", "as": "log",
                "in": {"$cond": [present_cond, {"$cond": [{"$eq": [{"$ifNull": ["$$log.half_day", False]}, True]}, 0.5, 1]}, 0]},
            }}},
            "__hours_values": {"$map": {"input": hours_filter, "as": "log", "in": "$$log.work_hours"}},
            "__late_count": {"$sum": {"$map": {"input": "$__logs", "as": "log", "in": {"$cond": [{"$gt": [{"$ifNull": ["$$log.late_minutes", 0]}, 0]}, 1, 0]}}}},
            "__late_total": {"$sum": {"$map": {"input": "$__logs", "as": "log", "in": {"$ifNull": ["$$log.late_minutes", 0]}}}},
            "__leave_count": {"$sum": {"$map": {"input": "$__logs", "as": "log", "in": {"$cond": [{"$eq": ["$$log.status", "LEAVE"]}, 1, 0]}}}},
            "__on_duty_count": {"$sum": {"$map": {"input": "$__logs", "as": "log", "in": {"$cond": [{"$eq": ["$$log.status", "ON_DUTY"]}, 1, 0]}}}},
            "__hours_sum": {"$sum": {"$map": {"input": hours_filter, "as": "log", "in": "$$log.work_hours"}}},
            "__hours_count": {"$size": hours_filter},
        }},
        {"$group": {
            "_id": "$department", "headcount": {"$sum": 1}, "present_days": {"$sum": "$__present_days"},
            "hours_sum": {"$sum": "$__hours_sum"}, "hours_count": {"$sum": "$__hours_count"},
            "late_count": {"$sum": "$__late_count"}, "total_late_minutes": {"$sum": "$__late_total"},
            "leave_count": {"$sum": "$__leave_count"}, "on_duty_count": {"$sum": "$__on_duty_count"},
        }},
        {"$project": {
            "_id": 0, "department": "$_id", "headcount": 1,
            "present_days": _round_expr("$present_days", 2),
            "avg_work_hours": {"$cond": [
                {"$gt": ["$hours_count", 0]},
                _round_expr({"$divide": ["$hours_sum", "$hours_count"]}, 2),
                None,
            ]},
            "late_count": 1, "total_late_minutes": 1, "leave_count": 1, "on_duty_count": 1,
        }},
        {"$sort": {"department": ASCENDING}},
    ]
    return stages


def _late_leaderboard_pipeline(start: str, end: str, limit: int, department: Optional[str]) -> list[dict]:
    pipeline: list[dict] = [
        {"$match": {"date": {"$gte": start, "$lte": end}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "__employee"}},
        {"$unwind": "$__employee"},  # Removes orphaned logs as required by the contract.
    ]
    if department is not None:
        pipeline.append({"$match": {"__employee.department": department}})
    pipeline.extend([
        {"$setWindowFields": {
            "sortBy": {"total_late_minutes": DESCENDING},
            "output": {"rank": {"$rank": {}}},
        }},
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": DESCENDING, "_id": ASCENDING}},
        {"$project": {
            "_id": 0, "rank": 1, "emp_code": "$_id", "name": "$__employee.name",
            "department": "$__employee.department", "total_late_minutes": 1, "late_count": 1,
        }},
    ])
    return pipeline


def _department_trend_pipeline(department: str, from_date: str, to_date: str) -> list[dict]:
    start_date_expr = {"$dateFromString": {"dateString": from_date}}
    end_date_expr = {"$dateFromString": {"dateString": to_date}}
    number_of_days = {"$add": [{"$dateDiff": {"startDate": start_date_expr, "endDate": end_date_expr, "unit": "day"}}, 1]}
    daily_logs_pipeline = [
        {"$match": {"$expr": {"$eq": ["$date", "$$day"]}}},
        {"$lookup": {"from": "employees", "localField": "emp_code", "foreignField": "emp_code", "as": "__employee"}},
        {"$match": {"$expr": {"$and": [
            {"$gt": [{"$size": "$__employee"}, 0]},
            {"$eq": [{"$arrayElemAt": ["$__employee.department", 0]}, "$$dept"]},
        ]}}},
        {"$group": {
            "_id": None,
            "present_count": {"$sum": {"$cond": [
                {"$in": ["$status", list(PRESENCE_STATUSES)]},
                {"$cond": [{"$eq": [{"$ifNull": ["$half_day", False]}, True]}, 0.5, 1]},
                0,
            ]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
        }},
    ]
    return [
        # Use one employee to seed the date series, even when no attendance logs exist.
        {"$match": {"department": department}},
        {"$limit": 1},
        {"$project": {"_id": 0, "department": 1, "__start": start_date_expr, "__day_count": number_of_days}},
        {"$addFields": {"__dates": {"$map": {
            "input": {"$range": [0, "$__day_count"]}, "as": "n",
            "in": {"$dateAdd": {"startDate": "$__start", "unit": "day", "amount": "$$n"}},
        }}}},
        {"$unwind": "$__dates"},
        {"$addFields": {"date": {"$dateToString": {"date": "$__dates", "format": "%Y-%m-%d"}}}},
        {"$lookup": {
            "from": "employees",
            "let": {"dept": "$department", "day": "$date"},
            "pipeline": [
                {"$match": {"$expr": {"$and": [
                    {"$eq": ["$department", "$$dept"]}, {"$lte": ["$joined_on", "$$day"]},
                ]}}},
                {"$count": "count"},
            ],
            "as": "__headcount_result",
        }},
        {"$addFields": {"headcount": {"$ifNull": [{"$arrayElemAt": ["$__headcount_result.count", 0]}, 0]}}},
        {"$lookup": {"from": "attendance_logs", "let": {"day": "$date", "dept": "$department"}, "pipeline": daily_logs_pipeline, "as": "__daily_metrics"}},
        {"$addFields": {
            "is_working_day": _working_weekday_expr("$__dates"),
            "present_count": {"$ifNull": [{"$arrayElemAt": ["$__daily_metrics.present_count", 0]}, 0]},
            "late_count": {"$ifNull": [{"$arrayElemAt": ["$__daily_metrics.late_count", 0]}, 0]},
        }},
        {"$addFields": {"attendance_rate": {"$cond": [
            {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
            _round_expr({"$divide": ["$present_count", "$headcount"]}, 4),
            None,
        ]}}},
        {"$sort": {"date": ASCENDING}},
        {"$setWindowFields": {
            "sortBy": {"date": ASCENDING},
            "output": {"__moving_avg": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}},
        }},
        {"$addFields": {"moving_avg_7d": {"$cond": [
            {"$ne": [{"$ifNull": ["$__moving_avg", None]}, None]}, _round_expr("$__moving_avg", 4), None,
        ]}}},
        {"$project": {
            "_id": 0, "date": 1, "is_working_day": 1, "headcount": 1, "present_count": 1,
            "late_count": 1, "attendance_rate": 1, "moving_avg_7d": 1,
        }},
    ]


@app.get(
    "/analytics/employees/{emp_code}/monthly",
    response_model=EmployeeMonthlyOut,
    responses={404: {"model": ErrorBody, "description": "Employee not found"}},
)

def employee_monthly(emp_code: str, month: MonthParam):
    start, end = _month_bounds(month)
    if db.employees.find_one({"emp_code": emp_code}, {"_id": 1}) is None:
        raise HTTPException(404, "employee not found")
    result = list(db.employees.aggregate(_employee_monthly_pipeline(emp_code, start, end), allowDiskUse=True))
    if not result:
        raise HTTPException(404, "employee not found")
    return {"emp_code": emp_code, "month": month, **result[0]}


@app.get("/analytics/departments/summary", response_model=DepartmentSummaryOut)

def department_summary(month: MonthParam, department: Optional[str] = None):
    start, end = _month_bounds(month)
    items = list(db.employees.aggregate(_department_summary_pipeline(start, end, department), allowDiskUse=True))
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late", response_model=LeaderboardOut)

def late_leaderboard(
    month: MonthParam,
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    start, end = _month_bounds(month)
    items = list(db.attendance_logs.aggregate(_late_leaderboard_pipeline(start, end, limit, department), allowDiskUse=True))
    return {"month": month, "items": items}


@app.get(
    "/analytics/departments/{department}/trend",
    response_model=TrendOut,
    responses={404: {"model": ErrorBody, "description": "Department not found"}},
)

def department_trend(
    department: str,
    from_date: str = Query(..., alias="from"),
    to_date: str = Query(..., alias="to"),
):
    start_day = _parse_date(from_date, field_name="from")
    end_day = _parse_date(to_date, field_name="to")
    if end_day < start_day:
        raise HTTPException(422, "to must be on or after from")
    if (end_day - start_day).days + 1 > 92:
        raise HTTPException(422, "date range cannot exceed 92 calendar days")
    if db.employees.find_one({"department": department}, {"_id": 1}) is None:
        raise HTTPException(404, "department not found")
    pipeline = _department_trend_pipeline(department, start_day.isoformat(), end_day.isoformat())
    items = list(db.employees.aggregate(pipeline, allowDiskUse=True))
    return {"department": department, "items": items}


# ---------------------------------------------------------------------------
# Explain endpoint - runs executionStats for the same query/pipeline as the API


# ---------------------------------------------------------------------------


def _explain_find(collection_name: str, query: dict, sort: list[tuple[str, int]], page: int, page_size: int) -> dict:
    command = {
        "explain": {
            "find": collection_name,
            "filter": query,
            "sort": dict(sort),
            "skip": (page - 1) * page_size,
            "limit": page_size,
        },
        "verbosity": "executionStats",
    }
    return db.command(command)


def _explain_aggregate(collection_name: str, pipeline: list[dict]) -> dict:
    command = {"explain": {"aggregate": collection_name, "pipeline": pipeline, "cursor": {}, "allowDiskUse": True}, "verbosity": "executionStats"}
    return db.command(command)


@app.get("/admin/explain/{endpoint}", response_model=ExplainOut)

def explain_endpoint(
    endpoint: Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"],
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to"),
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if endpoint == "attendance_list":
        query = _attendance_query(emp_code, date_from, date_to, status)
        explain = _explain_find("attendance_logs", query, _attendance_sort(), page, page_size)
        collection_name = "attendance_logs"
    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(422, "employee_monthly requires emp_code and month")
        start, end = _month_bounds(month)
        explain = _explain_aggregate("employees", _employee_monthly_pipeline(emp_code, start, end))
        collection_name = "employees"
    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(422, "department_summary requires month")
        start, end = _month_bounds(month)
        explain = _explain_aggregate("employees", _department_summary_pipeline(start, end, department))
        collection_name = "employees"
    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(422, "late_leaderboard requires month")
        start, end = _month_bounds(month)
        explain = _explain_aggregate("attendance_logs", _late_leaderboard_pipeline(start, end, limit, department))
        collection_name = "attendance_logs"
    else:
        if not department or not from_date or not to_date:
            raise HTTPException(422, "department_trend requires department, from, and to")
        from_day = _parse_date(from_date, field_name="from")
        to_day = _parse_date(to_date, field_name="to")
        if to_day < from_day or (to_day - from_day).days + 1 > 92:
            raise HTTPException(422, "department_trend requires a valid range of at most 92 days")
        explain = _explain_aggregate("employees", _department_trend_pipeline(department, from_day.isoformat(), to_day.isoformat()))
        collection_name = "employees"
    # Convert BSON-only values into JSON-safe Extended JSON objects while preserving raw explain structure.
    safe_explain = json_util.loads(json_util.dumps(explain))
    return {"endpoint": endpoint, "collection": collection_name, "explain": safe_explain}