# Design Decisions

> These decisions describe this reference implementation. The HROne kit's contract was inaccessible, so validate the choices against its actual data model, OpenAPI document and templates before submission.

## 1. FastAPI + Pydantic

FastAPI provides request validation, typed response models, error responses and automatically generated OpenAPI/Swagger documentation. Pydantic v2 models reject unknown fields (`extra="forbid"`) so misspelled fields are not silently ignored. The synchronous route handlers are deliberate because PyMongo is a synchronous driver; calling blocking PyMongo methods from `async def` routes would block the event loop.

## 2. PyMongo instead of Motor

The API uses the official PyMongo synchronous client. It avoids making the route functions asynchronous while the database calls remain blocking. Keep a single `MongoClient` for the app lifecycle because the client owns connection pooling; do not create a new client per request.

## 3. Record granularity and uniqueness

Each attendance document represents one employee's attendance for one business-local date. A unique compound index on `employee_id` and `work_date` enforces this invariant at the database layer, including races between concurrent requests. The application also checks state to return readable conflict responses.

This document granularity is a design assumption; change it if the supplied sample data or contract uses event documents or a separate employee collection.

## 4. Business date and timestamp handling

Store timestamps in UTC to avoid DST and cross-time-zone comparison ambiguity. Derive `work_date` from `BUSINESS_TIMEZONE`; India defaults to `Asia/Kolkata`. A timezone-naive client timestamp is interpreted in that business timezone. The server uses current UTC time when callers omit a timestamp. Future timestamps are rejected. For real organizations with overnight shifts, the definition of work date should follow the supplied policy rather than blindly using calendar midnight.

## 5. Punch state transitions

- No record → `punch-in` creates a record.
- Existing record with no punch-in → punch-in can fill that missing punch.
- Existing punch-in → a second punch-in is a conflict.
- Punch-out requires a punch-in and no prior punch-out.
- Punch-out must be strictly later than punch-in.
- Corrections revalidate the final pair of timestamps and recalculate status and duration.

These are sensible business rules from the assignment email; compare each one with the detailed problem statement.

## 6. Auditable manual corrections

A correction requires a human-readable reason and an actor identifier. Each history entry records the timestamp plus before/after values. Audit events are embedded in the same attendance document and written using one atomic MongoDB update; this avoids a partial state where the attendance update succeeds but a separate audit insert fails on a standalone MongoDB deployment. The document version is incremented; a compare-and-set filter makes conflicting writes return `409` instead of silently overwriting each other.

A production implementation would normally source the actor from authenticated identity, restrict correction permissions, consider immutable external audit retention, and redact sensitive fields. The assignment email did not define authentication or authorization.

## 7. Analytics in MongoDB

Daily and date-range summaries use aggregation stages (`$match`, `$group`, `$sum`, `$avg`, `$cond`, `$ifNull`, `$project`, `$sort`) rather than retrieving all rows to aggregate in Python. Matching early reduces documents processed. Indexes support employee/date and date-range filters. `average_worked_minutes` ignores open shifts because those have a `null` duration; totals treat missing durations as zero. Ensure this precisely matches the assignment's analytics definitions before submission.

## 8. Error semantics

The reference implementation uses `409 Conflict` for invalid state transitions and optimistic concurrency conflicts, `404` for a correction target that does not exist, `422` for input/time validation, and `503` for database failures. API error codes and response structure must be reconciled with the published OpenAPI contract.

## 9. Configuration and security

Database URI/database name and business timezone are read from environment variables. Secrets are not stored in the source tree. The README intentionally uses shell environment variables rather than a committed `.env`. The API is unauthenticated because the email did not request authentication; it is not safe to expose as a public production employee-data service without identity, access control and further hardening.

## 10. Testing strategy

Unit tests validate state transitions and response shapes with a mock database. Before submission, also run contract tests against the exact OpenAPI examples and integration tests against MongoDB 6.0+; mocks do not prove server-side transaction/index/aggregation compatibility.
