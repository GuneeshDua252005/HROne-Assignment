"""Employee Attendance & Analytics API.

Contract note: this implementation is based on the requirements included in the
assignment email. The assignment kit's OpenAPI contract and starter code were not
accessible during generation; reconcile route/field names with those files before
submission if they differ.
"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from typing import Any, Annotated
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import Depends, FastAPI, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.collection import Collection
from pymongo.database import Database
from pymongo.errors import DuplicateKeyError, PyMongoError


APP_VERSION = "1.0.0"
DEFAULT_DATABASE_NAME = "hrone_attendance"


def _business_timezone() -> ZoneInfo:
    name = os.getenv("BUSINESS_TIMEZONE", "Asia/Kolkata")
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError as exc:
        raise RuntimeError(f"Invalid BUSINESS_TIMEZONE: {name}") from exc


BUSINESS_TZ = _business_timezone()


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def as_utc(value: datetime) -> datetime:
    """Normalize internal or database timestamps to aware UTC.

    PyMongo commonly returns naive datetimes representing UTC unless tz_aware=True.
    Therefore, a naive value already stored/read from the database must be treated
    as UTC, not reinterpreted as business-local time.
    """
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def client_datetime_to_utc(value: datetime) -> datetime:
    """Interpret a naive client-supplied timestamp as business-local, then convert to UTC."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=BUSINESS_TZ)
    return value.astimezone(timezone.utc)


def work_date_for(value: datetime) -> str:
    return as_utc(value).astimezone(BUSINESS_TZ).date().isoformat()


def iso(value: Any) -> Any:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    return value


class APIModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class PunchRequest(APIModel):
    employee_id: str = Field(min_length=1, max_length=128, examples=["EMP001"])
    timestamp: datetime | None = Field(
        default=None,
        description="Optional event time for authorized/test clients. Omit to use server time.",
    )

    @field_validator("employee_id")
    @classmethod
    def employee_id_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("employee_id must not be blank")
        return value.strip()


class CorrectionRequest(APIModel):
    employee_id: str = Field(min_length=1, max_length=128)
    work_date: date
    # `model_fields_set` distinguishes omitted fields from an explicit null (clear).
    punch_in: datetime | None = None
    punch_out: datetime | None = None
    reason: str = Field(min_length=5, max_length=1000)
    corrected_by: str = Field(min_length=1, max_length=128)
    expected_version: int | None = Field(default=None, ge=1)

    @field_validator("employee_id", "corrected_by")
    @classmethod
    def nonblank_identifiers(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("value must not be blank")
        return value.strip()

    @field_validator("reason")
    @classmethod
    def reason_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("reason must not be blank")
        return value.strip()

    @model_validator(mode="after")
    def at_least_one_timestamp(self) -> "CorrectionRequest":
        if not ({"punch_in", "punch_out"} & self.model_fields_set):
            raise ValueError("Provide punch_in and/or punch_out to correct")
        return self


class AttendanceOut(BaseModel):
    employee_id: str
    work_date: str
    punch_in: datetime | None = None
    punch_out: datetime | None = None
    status: str
    worked_minutes: int | None = None
    version: int = 1
    audit_trail: list[dict[str, Any]] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


class MessageOut(BaseModel):
    message: str


def get_database(request: Request) -> Database:
    """Dependency designed to be overridden in tests."""
    db = getattr(request.app.state, "db", None)
    if db is None:
        raise HTTPException(status_code=503, detail="Database is not initialized")
    return db


def initialize_indexes(db: Database) -> None:
    attendance: Collection = db["attendance"]
    attendance.create_index(
        [("employee_id", ASCENDING), ("work_date", ASCENDING)], unique=True,
        name="uniq_employee_work_date",
    )
    attendance.create_index([("work_date", ASCENDING)], name="work_date_idx")
    attendance.create_index([("employee_id", ASCENDING), ("work_date", DESCENDING)], name="employee_date_idx")
    attendance.create_index(
        [("employee_id", ASCENDING)], unique=True,
        partialFilterExpression={"status": "checked_in"},
        name="uniq_employee_open_shift",
    )


def mongo_document(document: dict[str, Any] | None) -> dict[str, Any] | None:
    if document is None:
        return None
    document = dict(document)
    document.pop("_id", None)
    return document


def derive_status(punch_in: datetime | None, punch_out: datetime | None) -> str:
    if punch_in is None:
        return "not_started"
    if punch_out is None:
        return "checked_in"
    return "completed"


def derive_minutes(punch_in: datetime | None, punch_out: datetime | None) -> int | None:
    if punch_in is None or punch_out is None:
        return None
    seconds = (as_utc(punch_out) - as_utc(punch_in)).total_seconds()
    if seconds < 0:
        raise HTTPException(status_code=422, detail="punch_out must be later than punch_in")
    return int(seconds // 60)


def event_time(timestamp: datetime | None) -> datetime:
    value = client_datetime_to_utc(timestamp) if timestamp is not None else utc_now()
    # Reject client timestamps that are ahead of the server clock.
    if value > utc_now():
        raise HTTPException(status_code=422, detail="timestamp cannot be in the future")
    return value


def _record(db: Database, employee_id: str, work_date: str) -> dict[str, Any] | None:
    return db["attendance"].find_one({"employee_id": employee_id, "work_date": work_date})


def _public_record(record: dict[str, Any]) -> dict[str, Any]:
    clean = mongo_document(record) or {}
    # BSON datetimes may be naive when read back. Pydantic serializes them as timestamps;
    # normalize in output to avoid ambiguous local-time values.
    for key in ("punch_in", "punch_out", "created_at", "updated_at"):
        if isinstance(clean.get(key), datetime) and clean[key].tzinfo is None:
            clean[key] = clean[key].replace(tzinfo=timezone.utc)
    for event in clean.get("audit_trail", []):
        if isinstance(event.get("corrected_at"), datetime) and event["corrected_at"].tzinfo is None:
            event["corrected_at"] = event["corrected_at"].replace(tzinfo=timezone.utc)
    return clean


@asynccontextmanager
async def lifespan(app: FastAPI):
    # MongoClient is lazy; production startup verifies connectivity and creates indexes.
    client = MongoClient(
        os.getenv("MONGODB_URI", "mongodb://127.0.0.1:27017"),
        serverSelectionTimeoutMS=int(os.getenv("MONGODB_SERVER_SELECTION_TIMEOUT_MS", "3000")),
        tz_aware=True,
        appname="hrone-attendance-api",
    )
    db = client[os.getenv("MONGODB_DATABASE", DEFAULT_DATABASE_NAME)]
    app.state.mongo_client = client
    app.state.db = db
    if getattr(app.state, "initialize_database_on_startup", True):
        try:
            client.admin.command("ping")
            initialize_indexes(db)
        except Exception as exc:
            client.close()
            raise RuntimeError(
                "Cannot connect to MongoDB. Configure MONGODB_URI and ensure MongoDB 6.0+ is running."
            ) from exc
    try:
        yield
    finally:
        client.close()


def create_app(*, initialize_database_on_startup: bool = True) -> FastAPI:
    app = FastAPI(
        title="Employee Attendance & Analytics API",
        description=(
            "Punch-in/out, audited manual corrections, and MongoDB-aggregation-based analytics. "
            "All event timestamps are stored in UTC; the work date is based on BUSINESS_TIMEZONE."
        ),
        version=APP_VERSION,
        lifespan=lifespan,
    )
    app.state.initialize_database_on_startup = initialize_database_on_startup

    @app.get("/", tags=["system"])
    def root() -> dict[str, str]:
        return {"name": "Employee Attendance & Analytics API", "version": APP_VERSION, "docs": "/docs"}

    @app.get("/health", tags=["system"])
    def health(db: Database = Depends(get_database)) -> dict[str, str]:
        try:
            db.command("ping")
        except PyMongoError as exc:
            raise HTTPException(status_code=503, detail="MongoDB unavailable") from exc
        return {"status": "ok", "database": "connected"}

    @app.post("/attendance/punch-in", response_model=AttendanceOut, status_code=status.HTTP_201_CREATED, tags=["attendance"])
    def punch_in(body: PunchRequest, db: Database = Depends(get_database)) -> dict[str, Any]:
        when = event_time(body.timestamp)
        local_date = work_date_for(when)
        coll = db["attendance"]
        existing = _record(db, body.employee_id, local_date)
        if existing and existing.get("punch_in") is not None:
            raise HTTPException(status_code=409, detail="Employee has already punched in for this work date")
        if existing and existing.get("punch_out") is not None:
            raise HTTPException(status_code=409, detail="Cannot punch in after punch-out; submit a manual correction")
        # Do not open a second shift while an earlier shift remains unclosed.
        active_record = coll.find_one({"employee_id": body.employee_id, "status": "checked_in", "punch_out": None})
        if active_record is not None and (existing is None or active_record.get("_id") != existing.get("_id")):
            raise HTTPException(status_code=409, detail="An earlier attendance record is still checked in")
        now = utc_now()
        if existing:
            result = coll.find_one_and_update(
                {"_id": existing["_id"], "version": existing.get("version", 1)},
                {"$set": {"punch_in": when, "status": "checked_in", "worked_minutes": None, "updated_at": now},
                 "$inc": {"version": 1}},
                return_document=ReturnDocument.AFTER,
            )
            if result is None:
                raise HTTPException(status_code=409, detail="Attendance changed concurrently; retry the request")
            return _public_record(result)
        doc = {
            "employee_id": body.employee_id, "work_date": local_date,
            "punch_in": when, "punch_out": None, "status": "checked_in",
            "worked_minutes": None, "version": 1, "audit_trail": [],
            "created_at": now, "updated_at": now,
        }
        try:
            coll.insert_one(doc)
        except DuplicateKeyError as exc:
            raise HTTPException(status_code=409, detail="Attendance already exists; retry after refreshing") from exc
        return _public_record(doc)

    @app.post("/attendance/punch-out", response_model=AttendanceOut, tags=["attendance"])
    def punch_out(body: PunchRequest, db: Database = Depends(get_database)) -> dict[str, Any]:
        when = event_time(body.timestamp)
        coll = db["attendance"]
        # Match the currently open shift instead of today's date so overnight shifts work.
        existing = coll.find_one(
            {"employee_id": body.employee_id, "status": "checked_in", "punch_in": {"$ne": None}, "punch_out": None},
            sort=[("work_date", DESCENDING)],
        )
        if not existing:
            raise HTTPException(status_code=409, detail="Cannot punch out without an open punch-in")
        if as_utc(when) <= as_utc(existing["punch_in"]):
            raise HTTPException(status_code=422, detail="punch_out must be later than punch_in")
        now = utc_now()
        minutes = derive_minutes(existing["punch_in"], when)
        result = coll.find_one_and_update(
            {"_id": existing["_id"], "version": existing.get("version", 1), "punch_out": None},
            {"$set": {"punch_out": when, "status": "completed", "worked_minutes": minutes, "updated_at": now},
             "$inc": {"version": 1}},
            return_document=ReturnDocument.AFTER,
        )
        if result is None:
            raise HTTPException(status_code=409, detail="Attendance changed concurrently; retry the request")
        return _public_record(result)

    @app.post("/attendance/corrections", response_model=AttendanceOut, tags=["attendance"])
    def correct_attendance(body: CorrectionRequest, db: Database = Depends(get_database)) -> dict[str, Any]:
        coll = db["attendance"]
        work_date = body.work_date.isoformat()
        existing = _record(db, body.employee_id, work_date)
        if not existing:
            raise HTTPException(status_code=404, detail="Attendance record not found for employee and work date")
        current_version = existing.get("version", 1)
        if body.expected_version is not None and body.expected_version != current_version:
            raise HTTPException(status_code=409, detail="expected_version does not match current attendance version")
        old_values: dict[str, Any] = {}
        new_values: dict[str, Any] = {}
        updates: dict[str, Any] = {}
        for key in ("punch_in", "punch_out"):
            if key in body.model_fields_set:
                value = getattr(body, key)
                normalized = client_datetime_to_utc(value) if value is not None else None
                old_values[key] = existing.get(key)
                new_values[key] = normalized
                updates[key] = normalized
        final_in = updates.get("punch_in", existing.get("punch_in"))
        final_out = updates.get("punch_out", existing.get("punch_out"))
        if final_out is not None and final_in is None:
            raise HTTPException(status_code=422, detail="punch_out cannot exist without punch_in")
        if final_in is not None and final_out is not None and as_utc(final_out) <= as_utc(final_in):
            raise HTTPException(status_code=422, detail="punch_out must be later than punch_in")
        now = utc_now()
        new_status = derive_status(final_in, final_out)
        updates.update({"status": new_status, "worked_minutes": derive_minutes(final_in, final_out), "updated_at": now})
        audit_event = {
            "action": "manual_correction", "corrected_by": body.corrected_by,
            "reason": body.reason, "corrected_at": now,
            "old_values": old_values, "new_values": new_values,
        }
        result = coll.find_one_and_update(
            {"_id": existing["_id"], "version": current_version},
            {"$set": updates, "$push": {"audit_trail": audit_event}, "$inc": {"version": 1}},
            return_document=ReturnDocument.AFTER,
        )
        if result is None:
            raise HTTPException(status_code=409, detail="Attendance changed concurrently; retry the correction")
        return _public_record(result)

    @app.get("/attendance/{employee_id}", response_model=list[AttendanceOut], tags=["attendance"])
    def get_employee_attendance(
        employee_id: str,
        start_date: date | None = None,
        end_date: date | None = None,
        db: Database = Depends(get_database),
    ) -> list[dict[str, Any]]:
        if start_date and end_date and start_date > end_date:
            raise HTTPException(status_code=422, detail="start_date must be on or before end_date")
        query: dict[str, Any] = {"employee_id": employee_id}
        date_filter: dict[str, str] = {}
        if start_date:
            date_filter["$gte"] = start_date.isoformat()
        if end_date:
            date_filter["$lte"] = end_date.isoformat()
        if date_filter:
            query["work_date"] = date_filter
        return [_public_record(d) for d in db["attendance"].find(query).sort("work_date", ASCENDING)]

    @app.get("/analytics/daily", tags=["analytics"])
    def daily_analytics(
        work_date: date = Query(..., description="Business-local work date, YYYY-MM-DD"),
        db: Database = Depends(get_database),
    ) -> dict[str, Any]:
        match = {"$match": {"work_date": work_date.isoformat()}}
        pipeline = [
            match,
            {"$group": {
                "_id": "$employee_id",
                "records": {"$sum": 1},
                "completed_days": {"$sum": {"$cond": [{"$eq": ["$status", "completed"]}, 1, 0]}},
                "checked_in": {"$sum": {"$cond": [{"$eq": ["$status", "checked_in"]}, 1, 0]}},
                "worked_minutes": {"$sum": {"$ifNull": ["$worked_minutes", 0]}},
            }},
            {"$sort": {"_id": 1}},
            {"$project": {"_id": 0, "employee_id": "$_id", "records": 1, "completed_days": 1,
                           "checked_in": 1, "worked_minutes": 1}},
        ]
        rows = list(db["attendance"].aggregate(pipeline))
        total = sum(row["records"] for row in rows)
        completed = sum(row["completed_days"] for row in rows)
        checked_in = sum(row["checked_in"] for row in rows)
        minutes = sum(row["worked_minutes"] for row in rows)
        return {"work_date": work_date.isoformat(), "employee_count": total,
                "completed_count": completed, "checked_in_count": checked_in,
                "total_worked_minutes": minutes, "employees": rows}

    @app.get("/analytics/summary", tags=["analytics"])
    def analytics_summary(
        start_date: date = Query(...),
        end_date: date = Query(...),
        db: Database = Depends(get_database),
    ) -> dict[str, Any]:
        if start_date > end_date:
            raise HTTPException(status_code=422, detail="start_date must be on or before end_date")
        pipeline = [
            {"$match": {"work_date": {"$gte": start_date.isoformat(), "$lte": end_date.isoformat()}}},
            {"$group": {
                "_id": "$employee_id",
                "work_days": {"$sum": 1},
                "completed_days": {"$sum": {"$cond": [{"$eq": ["$status", "completed"]}, 1, 0]}},
                "open_days": {"$sum": {"$cond": [{"$eq": ["$status", "checked_in"]}, 1, 0]}},
                "total_worked_minutes": {"$sum": {"$ifNull": ["$worked_minutes", 0]}},
                "average_worked_minutes": {"$avg": "$worked_minutes"},
            }},
            {"$sort": {"_id": 1}},
            {"$project": {"_id": 0, "employee_id": "$_id", "work_days": 1,
                           "completed_days": 1, "open_days": 1,
                           "total_worked_minutes": 1, "average_worked_minutes": 1}},
        ]
        employees = list(db["attendance"].aggregate(pipeline))
        return {
            "start_date": start_date.isoformat(), "end_date": end_date.isoformat(),
            "employee_count": len(employees),
            "total_attendance_records": sum(row["work_days"] for row in employees),
            "completed_days": sum(row["completed_days"] for row in employees),
            "open_days": sum(row["open_days"] for row in employees),
            "total_worked_minutes": sum(row["total_worked_minutes"] for row in employees),
            "employees": employees,
        }

    @app.get("/analytics/employees/{employee_id}", tags=["analytics"])
    def employee_analytics(
        employee_id: str,
        start_date: date = Query(...),
        end_date: date = Query(...),
        db: Database = Depends(get_database),
    ) -> dict[str, Any]:
        if start_date > end_date:
            raise HTTPException(status_code=422, detail="start_date must be on or before end_date")
        pipeline = [
            {"$match": {"employee_id": employee_id,
                        "work_date": {"$gte": start_date.isoformat(), "$lte": end_date.isoformat()}}},
            {"$group": {
                "_id": "$employee_id", "work_days": {"$sum": 1},
                "completed_days": {"$sum": {"$cond": [{"$eq": ["$status", "completed"]}, 1, 0]}},
                "open_days": {"$sum": {"$cond": [{"$eq": ["$status", "checked_in"]}, 1, 0]}},
                "total_worked_minutes": {"$sum": {"$ifNull": ["$worked_minutes", 0]}},
                "average_worked_minutes": {"$avg": "$worked_minutes"},
            }},
            {"$project": {"_id": 0, "employee_id": "$_id", "work_days": 1,
                           "completed_days": 1, "open_days": 1,
                           "total_worked_minutes": 1, "average_worked_minutes": 1}},
        ]
        results = list(db["attendance"].aggregate(pipeline))
        if not results:
            return {"employee_id": employee_id, "start_date": start_date.isoformat(),
                    "end_date": end_date.isoformat(), "work_days": 0, "completed_days": 0,
                    "open_days": 0, "total_worked_minutes": 0, "average_worked_minutes": None}
        return {**results[0], "start_date": start_date.isoformat(), "end_date": end_date.isoformat()}

    @app.exception_handler(PyMongoError)
    async def mongo_error_handler(request: Request, exc: PyMongoError):
        # Do not leak credentials, URIs or internal database diagnostics to callers.
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=503, content={"detail": "Database operation failed"})

    return app


app = create_app()
