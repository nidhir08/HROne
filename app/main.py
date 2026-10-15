"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB  (loaded from .env for convenience)
"""

import os
import re
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from threading import Lock
from typing import Any, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pymongo import ASCENDING, DESCENDING, MongoClient
from pymongo.errors import DuplicateKeyError
from pydantic import BaseModel, field_validator, model_validator

load_dotenv()

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
_mongo_uri = os.getenv("MONGO_URI", "mongodb://localhost:27017")
_mongo_db  = os.getenv("MONGO_DB", "attendance_db")

client = MongoClient(_mongo_uri, tz_aware=True)
db     = client[_mongo_db]

IST = timezone(timedelta(hours=5, minutes=30))

# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------
app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    description="HROne technical assignment – punch-in/out, corrections, and analytics.",
)


# ---------------------------------------------------------------------------
# Index creation (idempotent; initialized at startup and retried as needed)
# ---------------------------------------------------------------------------
_indexes_ready = False
_index_lock = Lock()


@app.on_event("startup")
def create_indexes() -> None:
    """
    Try to create indexes on startup. If MongoDB is temporarily unavailable,
    write endpoints retry before relying on unique-index guarantees.
    """
    try:
        _ensure_indexes()
    except Exception:
        pass  # MongoDB may not be ready yet; indexes will be ensured lazily


def _ensure_indexes() -> None:
    global _indexes_ready
    if _indexes_ready:
        return

    with _index_lock:
        if _indexes_ready:
            return

        # employees – unique on emp_code so concurrent creates yield exactly one 201
        db.employees.create_index("emp_code", unique=True)
        db.employees.create_index([("department", ASCENDING), ("emp_code", ASCENDING)])
        db.employees.create_index("joined_on")

        # attendance_logs – unique on (emp_code, date)
        db.attendance_logs.create_index(
            [("emp_code", ASCENDING), ("date", ASCENDING)], unique=True
        )
        # Supports the list endpoint's date-descending, emp-code-ascending sort.
        db.attendance_logs.create_index(
            [("date", DESCENDING), ("emp_code", ASCENDING)]
        )
        db.attendance_logs.create_index("date")
        _indexes_ready = True


def _ensure_write_indexes() -> None:
    """Do not accept uniqueness-sensitive writes until indexes are available."""
    try:
        _ensure_indexes()
    except Exception as exc:
        raise HTTPException(503, detail="MongoDB indexes unavailable") from exc


# ---------------------------------------------------------------------------
# Business-rule helpers
# ---------------------------------------------------------------------------

def _to_ist(dt: datetime) -> datetime:
    """Convert any aware datetime to IST."""
    return dt.astimezone(IST)


def _truncate_seconds(dt: datetime) -> datetime:
    """Truncate to whole seconds (R1 – 09:40:00.900 → 09:40:00)."""
    return dt.replace(microsecond=0)


def _parse_iso_date(value: str, field_name: str) -> date:
    """Parse a strict YYYY-MM-DD date or return a client-facing 422 error."""
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise HTTPException(422, detail=f"{field_name} must use YYYY-MM-DD format")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise HTTPException(422, detail=f"{field_name} must be a valid date") from exc


def _month_bounds(month: str) -> tuple[str, str]:
    """Return inclusive ISO date bounds for YYYY-MM, rejecting year zero."""
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
        raise HTTPException(422, detail="month must use YYYY-MM format")
    year, month_number = map(int, month.split("-"))
    try:
        start = date(year, month_number, 1)
    except ValueError as exc:
        raise HTTPException(422, detail="month must be a valid calendar month") from exc
    if month_number == 12:
        if year == date.max.year:
            return start.isoformat(), date.max.isoformat()
        next_month = date(year + 1, 1, 1)
    else:
        next_month = date(year, month_number + 1, 1)
    return start.isoformat(), (next_month - timedelta(days=1)).isoformat()


def _attendance_date(punch_in_ist: datetime, shift_start: str, shift_end: str) -> str:
    """
    R1: Return the IST attendance date string YYYY-MM-DD.

    Overnight shift: shift_end <= shift_start (e.g. 22:00-06:00).
    A punch-in whose IST time is *earlier than* shift_end on a given calendar day
    belongs to the shift that started the *previous* day.
    """
    se_h, se_m = map(int, shift_end.split(":"))
    ss_h, ss_m = map(int, shift_start.split(":"))

    is_overnight = (se_h * 60 + se_m) <= (ss_h * 60 + ss_m)
    if is_overnight:
        punch_minutes = punch_in_ist.hour * 60 + punch_in_ist.minute
        end_minutes   = se_h * 60 + se_m
        if punch_minutes < end_minutes:
            # This punch-in is in the "morning tail" of an overnight shift
            # that started yesterday.
            return (punch_in_ist - timedelta(days=1)).date().isoformat()
    return punch_in_ist.date().isoformat()


def _compute_late_minutes(
    punch_in_ist: datetime, shift_start: str, attendance_date: str
) -> int:
    """
    R2: Grace period is 10 minutes (strictly more than 10:00 after shift_start).
    late_minutes = floor(elapsed since shift_start), not since grace end.
    """
    h, m = map(int, shift_start.split(":"))
    shift_date = _parse_iso_date(attendance_date, "attendance_date")
    shift_start_dt = datetime(
        shift_date.year, shift_date.month, shift_date.day,
        hour=h, minute=m, second=0, microsecond=0, tzinfo=IST,
    )
    elapsed = int((punch_in_ist - shift_start_dt).total_seconds())  # signed seconds
    if elapsed <= 600:  # <= 10:00 → not late
        return 0
    return elapsed // 60  # floor minutes from shift_start


def _compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """
    R4: (punch_out - punch_in) / 3600, rounded to 2 decimals half-up.
    Python's built-in round() uses banker's rounding; we need half-up.
    """
    seconds = Decimal(str((punch_out - punch_in).total_seconds()))
    hours = (seconds / Decimal(3600)).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )
    return float(hours)


def _compute_overtime_minutes(
    punch_out_ist: datetime, shift_end: str, attendance_date: str, shift_start: str = "09:30"
) -> int:
    """
    R3: floor minutes between shift_end and punch_out; 0 if < 30.
    For overnight shifts (shift_end <= shift_start) the shift_end falls on
    the day AFTER attendance_date.
    """
    se_h, se_m = map(int, shift_end.split(":"))
    ss_h, ss_m = map(int, shift_start.split(":"))
    base = datetime.fromisoformat(attendance_date).replace(tzinfo=IST)

    shift_end_dt = base.replace(hour=se_h, minute=se_m, second=0, microsecond=0)

    # Overnight shift: shift_end is on the next calendar day
    is_overnight = (se_h * 60 + se_m) <= (ss_h * 60 + ss_m)
    if is_overnight:
        shift_end_dt = shift_end_dt + timedelta(days=1)

    elapsed = int((punch_out_ist - shift_end_dt).total_seconds())
    if elapsed < 30 * 60:  # < 30 minutes

        return 0
    return elapsed // 60


def _round_half_up(value: float, decimals: int) -> float:
    """Round half-up without relying on binary floating-point tie behavior."""
    quantum = Decimal(1).scaleb(-decimals)
    return float(Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP))


# ---------------------------------------------------------------------------
# Serialisation helpers
# ---------------------------------------------------------------------------

def _dt_to_ms(dt: Optional[datetime]) -> Optional[int]:
    """Datetime → epoch milliseconds (or None)."""
    if dt is None:
        return None
    return int(dt.timestamp() * 1000)


def _ms_to_dt(ms: int) -> datetime:
    """Epoch milliseconds → UTC-aware datetime."""
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


def _format_record(doc: dict) -> dict:
    """Convert a MongoDB attendance document to the API response shape."""
    history = []
    for entry in doc.get("history") or []:
        changes = {}
        for field, val in (entry.get("changes") or {}).items():
            if field in ("punch_in", "punch_out"):
                changes[field] = {
                    "from": _dt_to_ms(val.get("from")),
                    "to":   _dt_to_ms(val.get("to")),
                }
            else:
                changes[field] = val
        history.append({
            "at":      _dt_to_ms(entry["at"]),
            "by":      entry["by"],
            "reason":  entry["reason"],
            "changes": changes,
        })

    return {
        "emp_code":          doc["emp_code"],
        "date":              doc["date"],
        "status":            doc["status"],
        "punch_in":          _dt_to_ms(doc.get("punch_in")),
        "punch_out":         _dt_to_ms(doc.get("punch_out")),
        "work_hours":        doc.get("work_hours"),
        "late_minutes":      doc.get("late_minutes", 0),
        "overtime_minutes":  doc.get("overtime_minutes", 0),
        "half_day":          doc.get("half_day", False),
        "history":           history,
    }


def _format_employee(doc: dict) -> dict:
    """Convert a MongoDB employee document to the API response shape."""
    return {
        "emp_code":    doc["emp_code"],
        "name":        doc["name"],
        "email":       doc["email"],
        "department":  doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end":   doc["shift_end"],
        "joined_on":   doc["joined_on"],
        "created_at":  _dt_to_ms(doc["created_at"]),
    }


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class EmployeeIn(BaseModel):
    emp_code:    str
    name:        str
    email:       str
    department:  str
    shift_start: str = "09:30"
    shift_end:   str = "18:30"
    joined_on:   str

    @field_validator("emp_code")
    @classmethod
    def validate_emp_code(cls, v: str) -> str:
        if not re.fullmatch(r"EMP\d{4,6}", v):
            raise ValueError("emp_code must match EMP followed by 4-6 digits")
        return v

    @field_validator("email")
    @classmethod
    def validate_email(cls, v: str) -> str:
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", v):
            raise ValueError("invalid email")
        return v

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: str) -> str:
        if not v or len(v) > 100:
            raise ValueError("name must be 1-100 characters")
        return v

    @field_validator("department")
    @classmethod
    def validate_department(cls, v: str) -> str:
        if not v or len(v) > 50:
            raise ValueError("department must be 1-50 characters")
        return v

    @field_validator("joined_on")
    @classmethod
    def validate_joined_on(cls, v: str) -> str:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):
            raise ValueError("joined_on must use YYYY-MM-DD format")
        try:
            date.fromisoformat(v)
        except ValueError as exc:
            raise ValueError("joined_on must be a valid date") from exc
        return v

    @field_validator("shift_start", "shift_end")
    @classmethod
    def validate_shift_time(cls, v: str) -> str:
        if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", v):
            raise ValueError("shift time must be HH:MM (24h)")
        return v

    @model_validator(mode="after")
    def shift_start_ne_end(self) -> "EmployeeIn":
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    emp_code:   str
    punched_at: Optional[int] = None
    status:     str = "PRESENT"

    @field_validator("punched_at")
    @classmethod
    def validate_epoch(cls, v: Optional[int]) -> Optional[int]:
        if v is not None:
            if v < 100_000_000_000 or v > 4_102_444_800_000:
                raise ValueError("punched_at out of valid epoch-ms range")
        return v

    @field_validator("status")
    @classmethod
    def validate_presence_status(cls, v: str) -> str:
        if v not in ("PRESENT", "WFH", "ON_DUTY"):
            raise ValueError("status must be PRESENT, WFH or ON_DUTY")
        return v


class PunchOutIn(BaseModel):
    emp_code:   str
    punched_at: Optional[int] = None

    @field_validator("punched_at")
    @classmethod
    def validate_epoch(cls, v: Optional[int]) -> Optional[int]:
        if v is not None:
            if v < 100_000_000_000 or v > 4_102_444_800_000:
                raise ValueError("punched_at out of valid epoch-ms range")
        return v


class RegularizeIn(BaseModel):
    status:          Optional[str] = None
    punch_in:        Optional[int] = None
    punch_out:       Optional[int] = None
    reason:          str
    regularized_by:  str

    @field_validator("punch_in", "punch_out")
    @classmethod
    def validate_epoch(cls, v: Optional[int]) -> Optional[int]:
        if v is not None:
            if v < 100_000_000_000 or v > 4_102_444_800_000:
                raise ValueError("epoch ms out of valid range")
        return v

    @field_validator("status")
    @classmethod
    def validate_status(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and v not in ("PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"):
            raise ValueError("invalid status")
        return v

    @field_validator("reason")
    @classmethod
    def validate_reason(cls, v: str) -> str:
        if len(v) < 5 or len(v) > 200:
            raise ValueError("reason must be 5-200 characters")
        return v

    @field_validator("regularized_by")
    @classmethod
    def validate_regularized_by(cls, v: str) -> str:
        if not v or len(v) > 50:
            raise ValueError("regularized_by must be 1-50 characters")
        return v


# ---------------------------------------------------------------------------
# System
# ---------------------------------------------------------------------------

@app.get("/health", tags=["system"])
def health():
    """Returns 200 if MongoDB answers a ping, otherwise 503."""
    try:
        db.command("ping")
        # Ensure indexes exist (idempotent; no-ops after first call)
        _ensure_indexes()
        return {"status": "ok"}
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(503, detail="MongoDB unavailable")


# ---------------------------------------------------------------------------
# Employees
# ---------------------------------------------------------------------------

@app.post("/employees", status_code=201, tags=["employees"])
def create_employee(body: EmployeeIn):
    """Create an employee. emp_code uniqueness is enforced by a DB index."""
    _ensure_write_indexes()
    doc = body.model_dump()
    doc["created_at"] = datetime.now(tz=timezone.utc)
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, detail="emp_code already exists")
    return _format_employee(doc)


@app.get("/employees", tags=["employees"])
def list_employees(
    department: Optional[str] = None,
    page:       int = Query(default=1, ge=1),
    page_size:  int = Query(default=20, ge=1, le=100),
):
    """List employees sorted by emp_code ascending. total counts the filtered set."""
    q: dict[str, Any] = {}
    if department:
        q["department"] = department

    total = db.employees.count_documents(q)
    skip  = (page - 1) * page_size
    docs  = list(
        db.employees.find(q, {"_id": 0})
        .sort("emp_code", ASCENDING)
        .skip(skip)
        .limit(page_size)
    )
    items = [_format_employee(d) for d in docs]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


# ---------------------------------------------------------------------------
# Attendance – punch-in
# ---------------------------------------------------------------------------

@app.post("/attendance/punch-in", status_code=201, tags=["attendance"])
def punch_in(body: PunchInIn):
    """
    Record a punch-in. Uses a unique index on (emp_code, date) to prevent
    duplicate inserts even under concurrent identical requests.
    """
    _ensure_write_indexes()
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, detail="employee not found")

    if body.punched_at is not None:
        ts_utc = _ms_to_dt(body.punched_at)
    else:
        ts_utc = datetime.now(tz=timezone.utc)

    ts_utc = _truncate_seconds(ts_utc)
    ts_ist = _to_ist(ts_utc)

    date_str = _attendance_date(ts_ist, emp["shift_start"], emp["shift_end"])
    late_min = _compute_late_minutes(ts_ist, emp["shift_start"], date_str)

    doc = {
        "emp_code":         body.emp_code,
        "date":             date_str,
        "status":           body.status,
        "punch_in":         ts_utc,
        "punch_out":        None,
        "work_hours":       None,
        "late_minutes":     late_min,
        "overtime_minutes": 0,
        "half_day":         False,
        "history":          [],
    }
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, detail="already punched in for this date")

    return _format_record(doc)


# ---------------------------------------------------------------------------
# Attendance – punch-out
# ---------------------------------------------------------------------------

@app.post("/attendance/punch-out", tags=["attendance"])
def punch_out(body: PunchOutIn):
    """
    Close the most recent open record whose punch_in <= punched_at.
    Concurrent punch-outs: exactly one succeeds (findOneAndUpdate atomicity).
    """
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, detail="employee not found")

    if body.punched_at is not None:
        ts_utc = _ms_to_dt(body.punched_at)
    else:
        ts_utc = datetime.now(tz=timezone.utc)

    ts_utc = _truncate_seconds(ts_utc)

    # Find the most recent open record whose punch_in <= punched_at
    record = db.attendance_logs.find_one(
        {
            "emp_code":  body.emp_code,
            "punch_in":  {"$lte": ts_utc},
            "punch_out": None,
        },
        sort=[("punch_in", DESCENDING)],
    )
    if not record:
        raise HTTPException(404, detail="no open punch-in found for this employee")

    punch_in_dt = record["punch_in"]

    # Validate punch-out constraints
    if ts_utc <= punch_in_dt:
        raise HTTPException(422, detail="punched_at must be after punch_in")
    if (ts_utc - punch_in_dt).total_seconds() > 86400:
        raise HTTPException(422, detail="punch-out cannot be more than 24 hours after punch_in")

    ts_ist = _to_ist(ts_utc)
    work_h = _compute_work_hours(punch_in_dt, ts_utc)
    ot_min = _compute_overtime_minutes(ts_ist, emp["shift_end"], record["date"], emp["shift_start"])
    half   = work_h < 4.50

    # Atomic update – only succeeds if punch_out is still None (concurrent safety)
    updated = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},
        {
            "$set": {
                "punch_out":        ts_utc,
                "work_hours":       work_h,
                "overtime_minutes": ot_min,
                "half_day":         half,
            }
        },
        return_document=True,
    )
    if updated is None:
        raise HTTPException(409, detail="already punched out")

    return _format_record(updated)


# ---------------------------------------------------------------------------
# Attendance – list
# ---------------------------------------------------------------------------

@app.get("/attendance", tags=["attendance"])
def list_attendance(
    emp_code:  Optional[str] = None,
    date_from: Optional[str] = None,
    date_to:   Optional[str] = None,
    status:    Optional[str] = None,
    page:      int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    """
    Sorted by date descending then emp_code ascending.
    Uses cursor-based DB queries (not in-memory). date_from > date_to → 422.
    """
    if date_from is not None:
        _parse_iso_date(date_from, "date_from")
    if date_to is not None:
        _parse_iso_date(date_to, "date_to")
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, detail="date_from must not be after date_to")

    q: dict[str, Any] = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        q["status"] = status

    total = db.attendance_logs.count_documents(q)
    skip  = (page - 1) * page_size
    docs  = list(
        db.attendance_logs.find(q)
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip(skip)
        .limit(page_size)
    )
    items = [_format_record(d) for d in docs]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


# ---------------------------------------------------------------------------
# Attendance – regularize (PATCH)
# ---------------------------------------------------------------------------

@app.patch("/attendance/{emp_code}/{date}", tags=["attendance"])
def regularize(emp_code: str, date: str, body: RegularizeIn):
    """
    Manually correct a record. Appends one history entry with only changed
    fields. Concurrent edits: atomic findOneAndUpdate prevents silent loss.
    """
    _parse_iso_date(date, "date")
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, detail="employee not found")

    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not record:
        raise HTTPException(404, detail="attendance record not found")

    new_status    = body.status    if body.status    is not None else record["status"]
    new_punch_in  = _ms_to_dt(body.punch_in)  if body.punch_in  is not None else record.get("punch_in")
    new_punch_out = _ms_to_dt(body.punch_out) if body.punch_out is not None else record.get("punch_out")

    # Truncate to whole seconds
    if new_punch_in  is not None:
        new_punch_in  = _truncate_seconds(new_punch_in)
    if new_punch_out is not None:
        new_punch_out = _truncate_seconds(new_punch_out)

    # --- Validation ---
    absent_or_leave = new_status in ("ABSENT", "LEAVE")

    if absent_or_leave:
        # Cannot supply punch times with ABSENT/LEAVE
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(422, detail="punch_in/punch_out cannot be set for ABSENT or LEAVE status")
        # Clear punch times
        new_punch_in  = None
        new_punch_out = None
    else:
        # Presence status requires a punch_in
        if new_punch_in is None:
            raise HTTPException(422, detail="presence status requires punch_in")

        # punch_in must stay on the attendance date (R1)
        pi_ist = _to_ist(new_punch_in)
        pi_date = _attendance_date(pi_ist, emp["shift_start"], emp["shift_end"])
        if pi_date != date:
            raise HTTPException(422, detail="punch_in must remain on the record's attendance date")

        # punch_out constraints
        if new_punch_out is not None:
            if new_punch_out <= new_punch_in:
                raise HTTPException(422, detail="punch_out must be after punch_in")
            if (new_punch_out - new_punch_in).total_seconds() > 86400:
                raise HTTPException(422, detail="punch_out cannot be more than 24 hours after punch_in")

    # --- Recompute derived fields ---
    if absent_or_leave or new_punch_in is None:
        new_late      = 0
        new_work_h    = None
        new_ot        = 0
        new_half      = False
    else:
        pi_ist = _to_ist(new_punch_in)
        new_late = _compute_late_minutes(pi_ist, emp["shift_start"], date)
        if new_punch_out is not None:
            new_work_h = _compute_work_hours(new_punch_in, new_punch_out)
            po_ist     = _to_ist(new_punch_out)
            new_ot     = _compute_overtime_minutes(po_ist, emp["shift_end"], date, emp["shift_start"])
            new_half   = new_work_h < 4.50
        else:
            new_work_h = None
            new_ot     = 0
            new_half   = False

    # --- Build changes dict (only actually-changed fields) ---
    changes: dict[str, Any] = {}

    old_status    = record["status"]
    old_punch_in  = record.get("punch_in")
    old_punch_out = record.get("punch_out")
    old_work_h    = record.get("work_hours")
    old_late      = record.get("late_minutes", 0)
    old_ot        = record.get("overtime_minutes", 0)
    old_half      = record.get("half_day", False)

    if new_status != old_status:
        changes["status"] = {"from": old_status, "to": new_status}
    if new_punch_in != old_punch_in:
        changes["punch_in"] = {"from": old_punch_in, "to": new_punch_in}
    if new_punch_out != old_punch_out:
        changes["punch_out"] = {"from": old_punch_out, "to": new_punch_out}
    if new_work_h != old_work_h:
        changes["work_hours"] = {"from": old_work_h, "to": new_work_h}
    if new_late != old_late:
        changes["late_minutes"] = {"from": old_late, "to": new_late}
    if new_ot != old_ot:
        changes["overtime_minutes"] = {"from": old_ot, "to": new_ot}
    if new_half != old_half:
        changes["half_day"] = {"from": old_half, "to": new_half}

    if not changes:
        raise HTTPException(422, detail="request changes nothing")

    history_entry = {
        "at":      datetime.now(tz=timezone.utc),
        "by":      body.regularized_by,
        "reason":  body.reason,
        "changes": changes,
    }

    # Atomic: match on _id + history array size to prevent silent loss from
    # concurrent edits. If another request appended to history between our
    # find_one above and this update, the $size filter won't match → None
    # is returned → we return 409.
    current_history_len = len(record.get("history") or [])

    updated = db.attendance_logs.find_one_and_update(
        {
            "_id":     record["_id"],
            "history": {"$size": current_history_len},
        },
        {
            "$set": {
                "status":            new_status,
                "punch_in":          new_punch_in,
                "punch_out":         new_punch_out,
                "work_hours":        new_work_h,
                "late_minutes":      new_late,
                "overtime_minutes":  new_ot,
                "half_day":          new_half,
            },
            "$push": {"history": history_entry},
        },
        return_document=True,
    )

    if updated is None:
        raise HTTPException(409, detail="concurrent edit conflict, please retry")

    return _format_record(updated)


# ---------------------------------------------------------------------------
# Analytics – employee monthly
# ---------------------------------------------------------------------------

@app.get("/analytics/employees/{emp_code}/monthly", tags=["analytics"])
def employee_monthly(
    emp_code: str,
    month:    str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    """
    Monthly summary for one employee via MongoDB aggregation.
    working_days per R7 (Mon-Fri, from joined_on if mid-month).
    present_days per R5-R7 (half day = 0.5, Mon-Fri only).
    """
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, detail="employee not found")

    month_start, month_end_str = _month_bounds(month)

    joined_on = emp["joined_on"]  # YYYY-MM-DD string
    # Effective start for working days is max(month_start, joined_on)
    eff_start = max(month_start, joined_on)

    # Count working days (Mon-Fri) from eff_start to month_end (inclusive)
    working_days = _count_weekdays(eff_start, month_end_str)

    pipeline = [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {"$gte": month_start, "$lte": month_end_str},
            }
        },
        {
            "$addFields": {
                "is_weekday": {
                    "$and": [
                        {"$gte": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 2]},
                        {"$lte": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 6]},
                    ]
                },
                "is_presence": {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                "is_leave":    {"$eq": ["$status", "LEAVE"]},
                "is_late":     {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]},
                # half_day might be missing in legacy records → default false
                "hd": {"$ifNull": ["$half_day", False]},
            }
        },
        {
            "$addFields": {
                # present contribution: weekday presence → 0.5 if half_day else 1.0
                "present_contribution": {
                    "$cond": [
                        {"$and": ["$is_presence", "$is_weekday"]},
                        {"$cond": ["$hd", 0.5, 1.0]},
                        0.0,
                    ]
                }
            }
        },
        {
            "$group": {
                "_id": None,
                "present_days":          {"$sum": "$present_contribution"},
                "leave_days":            {"$sum": {"$cond": ["$is_leave", 1, 0]}},
                "late_count":            {"$sum": {"$cond": ["$is_late", 1, 0]}},
                "total_late_minutes":    {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "total_overtime_minutes":{"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
            }
        },
    ]

    rows = list(db.attendance_logs.aggregate(pipeline))
    if rows:
        row = rows[0]
        present_days           = row["present_days"]
        leave_days             = int(row["leave_days"])
        late_count             = int(row["late_count"])
        total_late_minutes     = int(row["total_late_minutes"])
        total_overtime_minutes = int(row["total_overtime_minutes"])
    else:
        present_days           = 0.0
        leave_days             = 0
        late_count             = 0
        total_late_minutes     = 0
        total_overtime_minutes = 0

    present_days = _round_half_up(present_days, 2)

    if working_days > 0:
        attendance_pct = _round_half_up(present_days / working_days * 100, 2)
    else:
        attendance_pct = None

    return {
        "emp_code":               emp_code,
        "month":                  month,
        "working_days":           working_days,
        "present_days":           present_days,
        "leave_days":             leave_days,
        "late_count":             late_count,
        "total_late_minutes":     total_late_minutes,
        "total_overtime_minutes": total_overtime_minutes,
        "attendance_pct":         attendance_pct,
    }


# ---------------------------------------------------------------------------
# Analytics – department summary
# ---------------------------------------------------------------------------

@app.get("/analytics/departments/summary", tags=["analytics"])
def department_summary(
    month:      str            = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    """
    One row per department. Headcount includes employees with zero logs (R9).
    avg_work_hours: mean over presence records that have work_hours (R8 rounding).
    """
    month_start, month_end_str = _month_bounds(month)

    emp_match: dict[str, Any] = {"joined_on": {"$lte": month_end_str}}
    if department:
        emp_match["department"] = department

    # Step 1: per-department headcount from employees (R9 – joined on or before end of month)
    headcount_pipe = [
        {"$match": emp_match},
        {
            "$group": {
                "_id":       "$department",
                "headcount": {"$sum": 1},
                "emp_codes": {"$push": "$emp_code"},
            }
        },
    ]
    dept_headcounts = {r["_id"]: r for r in db.employees.aggregate(headcount_pipe)}

    if not dept_headcounts:
        return {"month": month, "items": []}

    # Step 2: aggregate attendance stats per department
    log_match: dict[str, Any] = {"date": {"$gte": month_start, "$lte": month_end_str}}
    # Only look at employees in scope
    all_emp_codes = []
    for r in dept_headcounts.values():
        all_emp_codes.extend(r["emp_codes"])
    log_match["emp_code"] = {"$in": all_emp_codes}

    # We need to join logs with employees to get department
    agg_pipe = [
        {"$match": log_match},
        # Lookup employee to get department
        {
            "$lookup": {
                "from":         "employees",
                "localField":   "emp_code",
                "foreignField": "emp_code",
                "as":           "emp_info",
            }
        },
        {"$unwind": "$emp_info"},
        {
            "$addFields": {
                "dept":        "$emp_info.department",
                "is_presence": {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                "is_leave":    {"$eq": ["$status", "LEAVE"]},
                "is_on_duty":  {"$eq": ["$status", "ON_DUTY"]},
                "is_late":     {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]},
                "is_weekday": {
                    "$and": [
                        {"$gte": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 2]},
                        {"$lte": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 6]},
                    ]
                },
                "hd": {"$ifNull": ["$half_day", False]},
                # work_hours only for presence records that have it
                "wh_for_avg": {
                    "$cond": [
                        {
                            "$and": [
                                {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                {"$ne": ["$work_hours", None]},
                                {"$gt": ["$work_hours", None]},
                            ]
                        },
                        "$work_hours",
                        None,
                    ]
                },
            }
        },
        {
            "$addFields": {
                "present_contribution": {
                    "$cond": [
                        {"$and": ["$is_presence", "$is_weekday"]},
                        {"$cond": ["$hd", 0.5, 1.0]},
                        0.0,
                    ]
                }
            }
        },
        {
            "$group": {
                "_id":                "$dept",
                "present_days":       {"$sum": "$present_contribution"},
                "late_count":         {"$sum": {"$cond": ["$is_late", 1, 0]}},
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "leave_count":        {"$sum": {"$cond": ["$is_leave", 1, 0]}},
                "on_duty_count":      {"$sum": {"$cond": ["$is_on_duty", 1, 0]}},
                "wh_values":          {"$push": "$wh_for_avg"},
            }
        },
    ]

    if department:
        # emp_info only exists after $lookup and $unwind (stages 1 and 2).
        agg_pipe.insert(3, {"$match": {"emp_info.department": department}})

    log_stats = {r["_id"]: r for r in db.attendance_logs.aggregate(agg_pipe)}

    items = []
    for dept_name in sorted(dept_headcounts.keys()):
        hc   = dept_headcounts[dept_name]["headcount"]
        stat = log_stats.get(dept_name, {})

        # avg_work_hours: mean over presence records that have work_hours
        wh_values = [v for v in (stat.get("wh_values") or []) if v is not None]
        if wh_values:
            avg_wh = _round_half_up(sum(wh_values) / len(wh_values), 2)
        else:
            avg_wh = None

        items.append({
            "department":        dept_name,
            "headcount":         hc,
            "present_days":      _round_half_up(stat.get("present_days", 0.0), 2),
            "avg_work_hours":    avg_wh,
            "late_count":        int(stat.get("late_count", 0)),
            "total_late_minutes":int(stat.get("total_late_minutes", 0)),
            "leave_count":       int(stat.get("leave_count", 0)),
            "on_duty_count":     int(stat.get("on_duty_count", 0)),
        })

    return {"month": month, "items": items}


# ---------------------------------------------------------------------------
# Analytics – late leaderboard
# ---------------------------------------------------------------------------

@app.get("/analytics/leaderboard/late", tags=["analytics"])
def late_leaderboard(
    month:      str            = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit:      int            = Query(default=10, ge=1, le=50),
    department: Optional[str] = None,
):
    """
    Standard competition ranking by total_late_minutes descending.
    limit applies after ranking (ties at the cutoff are all included).
    """
    month_start, month_end_str = _month_bounds(month)

    match_stage: dict[str, Any] = {
        "date":         {"$gte": month_start, "$lte": month_end_str},
        "late_minutes": {"$gt": 0},
    }

    pipeline = [
        {"$match": match_stage},
        {
            "$group": {
                "_id":                "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count":         {"$sum": 1},
            }
        },
        # Join with employees to get name/department, also filter by department if given
        {
            "$lookup": {
                "from":         "employees",
                "localField":   "_id",
                "foreignField": "emp_code",
                "as":           "emp_info",
            }
        },
        # Ignore logs whose employee doesn't exist
        {"$match": {"emp_info": {"$ne": []}}},
        {"$unwind": "$emp_info"},
    ]

    if department:
        pipeline.append({"$match": {"emp_info.department": department}})

    pipeline += [
        {"$sort": {"total_late_minutes": DESCENDING, "_id": ASCENDING}},
    ]

    rows = list(db.attendance_logs.aggregate(pipeline))

    # Apply standard competition ranking in Python (simple, small result set)
    ranked = []
    rank = 1
    prev_minutes = None
    for i, row in enumerate(rows):
        if prev_minutes is not None and row["total_late_minutes"] < prev_minutes:
            rank = i + 1
        ranked.append({
            "rank":               rank,
            "emp_code":           row["_id"],
            "name":               row["emp_info"]["name"],
            "department":         row["emp_info"]["department"],
            "total_late_minutes": int(row["total_late_minutes"]),
            "late_count":         int(row["late_count"]),
        })
        prev_minutes = row["total_late_minutes"]

    # limit: include all rows whose rank <= limit
    result = [r for r in ranked if r["rank"] <= limit]

    return {"month": month, "items": result}


# ---------------------------------------------------------------------------
# Analytics – department daily trend
# ---------------------------------------------------------------------------

@app.get("/analytics/departments/{department}/trend", tags=["analytics"])
def department_trend(
    department: str,
    from_date:  str = Query(..., alias="from"),
    to_date:    str = Query(..., alias="to"),
):
    """
    Daily attendance trend for a department. Fills missing dates in Python.
    Returns exactly one row per calendar day from from_date to to_date inclusive.
    """
    # Validation
    from_d = _parse_iso_date(from_date, "from")
    to_d = _parse_iso_date(to_date, "to")
    if to_d < from_d:
        raise HTTPException(422, detail="'to' must not be before 'from'")

    if (to_d - from_d).days > 91:  # 92 days means max range = 92 calendar days
        raise HTTPException(422, detail="date range cannot exceed 92 days")

    # Check department exists
    if not db.employees.find_one({"department": department}):
        raise HTTPException(404, detail="department not found")

    # We'll compute per-day in Python over the range (up to 92 days – tiny loop)
    # For each day, headcount = employees with joined_on <= that day and department = dept
    # This is cheaper than a complex $densify aggregation and avoids $densify version issues.

    # Pull all employees in the department
    emps = list(db.employees.find(
        {"department": department},
        {"emp_code": 1, "joined_on": 1, "_id": 0}
    ))

    # Pull all attendance logs in the range for these employees
    emp_codes = [e["emp_code"] for e in emps]
    logs = list(db.attendance_logs.find(
        {
            "emp_code": {"$in": emp_codes},
            "date":     {"$gte": from_date, "$lte": to_date},
        },
        {"_id": 0, "date": 1, "status": 1, "late_minutes": 1, "half_day": 1},
    ))

    # Index logs by date
    from collections import defaultdict
    logs_by_date: dict[str, list] = defaultdict(list)
    for log in logs:
        logs_by_date[log["date"]].append(log)

    items = []
    non_null_rates: list[Optional[float]] = []  # window for moving avg

    current = from_d
    while current <= to_d:
        day_str   = current.isoformat()
        weekday   = current.weekday()  # 0=Mon ... 6=Sun
        is_wd     = weekday < 5

        # headcount: employees joined on or before this day
        headcount = sum(1 for e in emps if e["joined_on"] <= day_str)

        day_logs    = logs_by_date.get(day_str, [])
        present_cnt = 0.0
        late_cnt    = 0
        for log in day_logs:
            if log["status"] in ("PRESENT", "WFH", "ON_DUTY"):
                present_cnt += 0.5 if log.get("half_day") else 1.0
            if (log.get("late_minutes") or 0) > 0:
                late_cnt += 1

        # attendance_rate: only on working days with headcount > 0
        if is_wd and headcount > 0:
            att_rate: Optional[float] = _round_half_up(present_cnt / headcount, 4)
        else:
            att_rate = None

        non_null_rates.append(att_rate)

        # moving_avg_7d: mean of non-null rates in current window (up to 7 rows)
        window_size = min(7, len(non_null_rates))
        window      = non_null_rates[-window_size:]
        valid_rates = [r for r in window if r is not None]
        if valid_rates:
            moving_avg: Optional[float] = _round_half_up(sum(valid_rates) / len(valid_rates), 4)
        else:
            moving_avg = None

        items.append({
            "date":           day_str,
            "is_working_day": is_wd,
            "headcount":      headcount,
            "present_count":  present_cnt,
            "late_count":     late_cnt,
            "attendance_rate": att_rate,
            "moving_avg_7d":  moving_avg,
        })

        current += timedelta(days=1)

    return {"department": department, "items": items}


# ---------------------------------------------------------------------------
# Admin – explain
# ---------------------------------------------------------------------------

@app.get("/admin/explain/{endpoint}", tags=["admin"])
def explain_endpoint(
    endpoint:   str,
    emp_code:   Optional[str] = None,
    month:      Optional[str] = Query(default=None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
    limit:      int           = Query(default=10, ge=1, le=50),
    date_from:  Optional[str] = None,
    date_to:    Optional[str] = None,
    status:     Optional[str] = None,
    from_date:  Optional[str] = Query(default=None, alias="from"),
    to_date:    Optional[str] = Query(default=None, alias="to"),
    page:       int           = Query(default=1, ge=1),
    page_size:  int           = Query(default=20, ge=1, le=100),
):
    """Return MongoDB explain output for the main query of the named endpoint."""

    valid_endpoints = {
        "attendance_list",
        "employee_monthly",
        "department_summary",
        "late_leaderboard",
        "department_trend",
    }
    if endpoint not in valid_endpoints:
        raise HTTPException(422, detail=f"endpoint must be one of {sorted(valid_endpoints)}")

    if endpoint == "attendance_list":
        if date_from is not None:
            _parse_iso_date(date_from, "date_from")
        if date_to is not None:
            _parse_iso_date(date_to, "date_to")
        if date_from and date_to and date_from > date_to:
            raise HTTPException(422, detail="date_from must not be after date_to")
        q: dict[str, Any] = {}
        if emp_code:
            q["emp_code"] = emp_code
        if date_from or date_to:
            q["date"] = {}
            if date_from:
                q["date"]["$gte"] = date_from
            if date_to:
                q["date"]["$lte"] = date_to
        if status:
            q["status"] = status
        skip = (page - 1) * page_size
        cmd = db.command(
            "explain",
            {
                "find":   "attendance_logs",
                "filter": q,
                "sort":   {"date": -1, "emp_code": 1},
                "skip":   skip,
                "limit":  page_size,
            },
            verbosity="executionStats",
        )
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": cmd}

    elif endpoint == "employee_monthly":
        if not month:
            raise HTTPException(422, detail="month is required for employee_monthly")
        if not emp_code:
            raise HTTPException(422, detail="emp_code is required for employee_monthly")
        ms, me = _month_bounds(month)
        pipeline = [
            {"$match": {"emp_code": emp_code, "date": {"$gte": ms, "$lte": me}}}
        ]
        cmd = db.command(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": cmd}

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(422, detail="month is required for department_summary")
        _, me = _month_bounds(month)
        emp_match: dict[str, Any] = {"joined_on": {"$lte": me}}
        if department:
            emp_match["department"] = department
        pipeline = [{"$match": emp_match}]
        cmd = db.command(
            "explain",
            {"aggregate": "employees", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        return {"endpoint": endpoint, "collection": "employees", "explain": cmd}

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(422, detail="month is required for late_leaderboard")
        ms, me = _month_bounds(month)
        match_stage: dict[str, Any] = {
            "date":         {"$gte": ms, "$lte": me},
            "late_minutes": {"$gt": 0},
        }
        pipeline = [{"$match": match_stage}]
        cmd = db.command(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": cmd}

    elif endpoint == "department_trend":
        if not from_date or not to_date:
            raise HTTPException(422, detail="'from' and 'to' are required for department_trend")
        if not department:
            raise HTTPException(422, detail="department is required for department_trend")
        from_d = _parse_iso_date(from_date, "from")
        to_d = _parse_iso_date(to_date, "to")
        if to_d < from_d:
            raise HTTPException(422, detail="'to' must not be before 'from'")
        if (to_d - from_d).days > 91:
            raise HTTPException(422, detail="date range cannot exceed 92 days")

        employees = list(db.employees.find(
            {"department": department},
            {"emp_code": 1, "joined_on": 1, "_id": 0},
        ))
        emp_codes = [employee["emp_code"] for employee in employees]
        employee_explain = db.command(
            "explain",
            {
                "find": "employees",
                "filter": {"department": department},
                "projection": {"emp_code": 1, "joined_on": 1, "_id": 0},
            },
            verbosity="executionStats",
        )
        attendance_explain = db.command(
            "explain",
            {
                "find": "attendance_logs",
                "filter": {
                    "emp_code": {"$in": emp_codes},
                    "date": {"$gte": from_date, "$lte": to_date},
                },
                "projection": {
                    "date": 1,
                    "status": 1,
                    "late_minutes": 1,
                    "half_day": 1,
                    "_id": 0,
                },
            },
            verbosity="executionStats",
        )
        return {
            "endpoint": endpoint,
            "explain": {
                "employees": employee_explain,
                "attendance_logs": attendance_explain,
            },
        }


# ---------------------------------------------------------------------------
# Utility – count weekdays between two date strings inclusive
# ---------------------------------------------------------------------------

def _count_weekdays(start_str: str, end_str: str) -> int:
    """Count Mon-Fri days in [start, end] inclusive."""
    start = datetime.fromisoformat(start_str).date()
    end   = datetime.fromisoformat(end_str).date()
    if start > end:
        return 0
    total_days = (end - start).days + 1
    # Complete weeks
    full_weeks, rem = divmod(total_days, 7)
    weekdays = full_weeks * 5
    # Remaining days
    start_wd = start.weekday()  # 0=Mon
    for i in range(rem):
        if (start_wd + i) % 7 < 5:
            weekdays += 1
    return weekdays
