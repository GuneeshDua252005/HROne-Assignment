# Employee Attendance & Analytics API

Python 3.11+ · FastAPI · PyMongo · MongoDB 6.0+

> **Important contract limitation:** The HROne Google Drive kit could not be fetched in this working session. This is a runnable reference implementation based on the assignment email, not a verified exact match to the kit's OpenAPI contract, sample data, starter code, or review template. Before submission, compare the route names, request/response fields, status codes, collection names, and aggregation outputs with the actual kit. Do not claim the contract was verified if it was not.

## Features

- Punch in and punch out, one attendance record per employee per business-local work date.
- Duplicate punch events and punch-out-before-punch-in are rejected.
- Manual corrections capture editor, reason, timestamp, old/new values and version history in the same attendance document. Embedded audit history and the changed attendance record are written atomically in one MongoDB update.
- Optimistic version check prevents concurrent updates from silently overwriting one another.
- Analytics are calculated using MongoDB aggregation pipelines rather than loading all rows and aggregating in Python.
- UTC timestamps in storage; `BUSINESS_TIMEZONE` determines which work date a punch belongs to. The default is `Asia/Kolkata`.
- Unique compound index prevents duplicate attendance records for the same employee and work date.

## Repository layout

```text
app/main.py
requirements.txt
README.md
REVIEW.md
DECISIONS.md
tests/test_api.py
.gitignore
```

## Prerequisites

- Python 3.11 or newer
- MongoDB 6.0 or newer (local service or free Atlas cluster)
- Git

## Install and run (Windows PowerShell)

From this directory:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
$env:MONGODB_URI = "mongodb://127.0.0.1:27017"
$env:MONGODB_DATABASE = "hrone_attendance"
$env:BUSINESS_TIMEZONE = "Asia/Kolkata"
python -m uvicorn app.main:app --reload
```

Linux/macOS:

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
export MONGODB_URI='mongodb://127.0.0.1:27017'
export MONGODB_DATABASE='hrone_attendance'
export BUSINESS_TIMEZONE='Asia/Kolkata'
python -m uvicorn app.main:app --reload
```

On startup, the application pings MongoDB and creates indexes. Open `http://127.0.0.1:8000/docs` for interactive Swagger docs and `http://127.0.0.1:8000/health` for a database connectivity check. For Atlas, set `MONGODB_URI` in the shell/environment; never commit it to Git.

## Endpoints in this reference implementation

All paths below are provisional pending comparison with the supplied OpenAPI file.

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | Application/database health |
| POST | `/attendance/punch-in` | Create/update the day's punch-in |
| POST | `/attendance/punch-out` | Set punch-out for an existing open attendance record |
| POST | `/attendance/corrections` | Correct punch timestamps, retaining audit history |
| GET | `/attendance/{employee_id}` | List attendance with optional `start_date` and `end_date` |
| GET | `/analytics/daily?work_date=YYYY-MM-DD` | Daily employee and summary metrics |
| GET | `/analytics/summary?start_date=YYYY-MM-DD&end_date=YYYY-MM-DD` | Range summary by employee and overall |
| GET | `/analytics/employees/{employee_id}?start_date=...&end_date=...` | Employee-specific range metrics |

### Request examples

Punch in (server timestamp):

```json
{"employee_id":"EMP001"}
```

Punch out (server timestamp):

```json
{"employee_id":"EMP001"}
```

Optional explicit timestamps are supported for reproducible tests and controlled integrations; they must not be in the future. A timezone-naive timestamp is interpreted in `BUSINESS_TIMEZONE`.

Manual correction (provide one or both of `punch_in` and `punch_out`):

```json
{
  "employee_id": "EMP001",
  "work_date": "2026-10-14",
  "punch_in": "2026-10-14T09:15:00+05:30",
  "reason": "Manager-approved correction to missed punch-in",
  "corrected_by": "HR001",
  "expected_version": 2
}
```

An explicit `null` clears a timestamp. Omit a field to leave its current value unchanged. Corrections require a reason and actor; `expected_version` is optional but recommended. Recalculate all business rules after every correction.

## Data model

The `attendance` collection has one document for each `{employee_id, work_date}`:

- `employee_id`: stable employee identifier.
- `work_date`: business-local ISO date string (`YYYY-MM-DD`) so lexical range matching sorts correctly.
- `punch_in`, `punch_out`: BSON dates normalized to UTC.
- `status`: `not_started`, `checked_in`, or `completed`.
- `worked_minutes`: floor of elapsed seconds divided by 60 when both times exist; `null` otherwise.
- `version`: monotonically incremented for write-conflict detection.
- `audit_trail`: array of manual correction records with actor, reason, timestamp, and old/new values.
- `created_at`, `updated_at`: UTC BSON dates.

Indexes: unique `{employee_id: 1, work_date: 1}`, `{work_date: 1}`, `{employee_id: 1, work_date: -1}`, and a partial unique `{employee_id: 1}` index for `status: checked_in` to prevent two simultaneous open shifts.

## Testing

```bash
python -m pytest -q
```

The tests use `mongomock` for fast unit-level behavior checks. It is not a replacement for integration tests against MongoDB 6.0+; run an additional smoke test against a real MongoDB instance before submission. The current environment did not have a reachable MongoDB service, so do not describe real-server integration as verified.

## Error conventions

- `201`: initial punch-in creates a new record.
- `200`: punch-out, corrections, list, analytics.
- `409`: duplicate/incompatible punch, concurrent version conflict.
- `404`: correction target does not exist.
- `422`: invalid date order, invalid time ordering, malformed fields.
- `503`: database unavailable or operation failure.

These are reasonable defaults only; make them match the HROne OpenAPI contract before submission.

## Security and operational notes

- Do not commit secrets, `.env`, credentials, Atlas connection strings or private employee data.
- Prefer restricted database users and TLS for Atlas/production deployments.
- This assignment API intentionally has no authentication layer because the email did not specify one. Do not expose it publicly as a production employee-data service without authentication, authorization, request auditing, rate limits and a data-retention policy.
- Corrections currently trust the `corrected_by` request field because authentication/identity claims were not specified. In a production system, obtain this identity from a verified session/token rather than client input.
- Configure business timezone explicitly for the intended organization. A real overnight-shift policy may need a different work-date rule.

## What is not verified / must be reconciled with the kit

Because the linked kit could not be retrieved, the following may differ from HROne's evaluator contract: exact endpoint paths and methods, request names, schema/collection fields, whether employee master data is a separate collection, expected error payloads, analytics definitions, rounding rules, test seed records, and the specific code review task. Compare the supplied OpenAPI and templates with this implementation before pushing. The `REVIEW.md` explicitly does not pretend to have inspected unseen code.

## Submission reminders

1. Ensure `app/main.py`, `requirements.txt`, `REVIEW.md`, `DECISIONS.md` and `README.md` are committed.
2. Do not include `.env`, credentials/secrets, or a Dockerfile.
3. Push to a public Git repository and submit the URL through HROne's form using the same email used to apply.
4. Keep the repository public through 31 October 2026.
5. Do not post the problem statement or solution publicly before 15 November 2026.
6. Understand every line and be ready to explain and change the code in the live discussion.
