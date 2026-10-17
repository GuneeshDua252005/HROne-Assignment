import mongomock
import pytest
from fastapi.testclient import TestClient

from app.main import create_app, get_database


@pytest.fixture()
def client_and_db():
    database = mongomock.MongoClient().hrone_test
    app = create_app(initialize_database_on_startup=False)
    app.dependency_overrides[get_database] = lambda: database
    with TestClient(app) as client:
        yield client, database
    app.dependency_overrides.clear()


def test_punch_out_requires_punch_in(client_and_db):
    client, _ = client_and_db
    response = client.post("/attendance/punch-out", json={"employee_id": "E100", "timestamp": "2024-08-03T18:00:00+05:30"})
    assert response.status_code == 409


def test_punch_in_and_out_and_duplicate_validation(client_and_db):
    client, db = client_and_db
    start = client.post("/attendance/punch-in", json={"employee_id": "E100", "timestamp": "2024-08-03T09:00:00+05:30"})
    assert start.status_code == 201
    assert start.json()["status"] == "checked_in"

    duplicate = client.post("/attendance/punch-in", json={"employee_id": "E100", "timestamp": "2024-08-03T09:10:00+05:30"})
    assert duplicate.status_code == 409

    end = client.post("/attendance/punch-out", json={"employee_id": "E100", "timestamp": "2024-08-03T17:00:00+05:30"})
    assert end.status_code == 200
    assert end.json()["status"] == "completed"
    assert end.json()["worked_minutes"] == 480
    assert db.attendance.count_documents({"employee_id": "E100"}) == 1


def test_manual_correction_records_audit_event(client_and_db):
    client, _ = client_and_db
    start = client.post("/attendance/punch-in", json={"employee_id": "E200", "timestamp": "2024-08-03T09:00:00+05:30"})
    assert start.status_code == 201
    end = client.post("/attendance/punch-out", json={"employee_id": "E200", "timestamp": "2024-08-03T17:00:00+05:30"})
    assert end.status_code == 200
    version = end.json()["version"]

    correction = client.post("/attendance/corrections", json={
        "employee_id": "E200",
        "work_date": "2024-08-03",
        "punch_out": "2024-08-03T17:30:00+05:30",
        "reason": "Approved correction",
        "corrected_by": "HR01",
        "expected_version": version,
    })
    assert correction.status_code == 200, correction.text
    body = correction.json()
    assert body["worked_minutes"] == 510
    assert body["version"] == version + 1
    assert len(body["audit_trail"]) == 1
    assert body["audit_trail"][0]["reason"] == "Approved correction"
    assert body["audit_trail"][0]["old_values"]["punch_out"] is not None


def test_analytics_use_mongodb_aggregation(client_and_db):
    client, _ = client_and_db
    for employee, start, end in [
        ("E300", "2024-08-03T09:00:00+05:30", "2024-08-03T17:00:00+05:30"),
        ("E400", "2024-08-03T10:00:00+05:30", "2024-08-03T18:00:00+05:30"),
    ]:
        assert client.post("/attendance/punch-in", json={"employee_id": employee, "timestamp": start}).status_code == 201
        assert client.post("/attendance/punch-out", json={"employee_id": employee, "timestamp": end}).status_code == 200

    daily = client.get("/analytics/daily", params={"work_date": "2024-08-03"})
    assert daily.status_code == 200, daily.text
    assert daily.json()["employee_count"] == 2
    assert daily.json()["completed_count"] == 2
    assert daily.json()["total_worked_minutes"] == 960

    summary = client.get("/analytics/summary", params={"start_date": "2024-08-03", "end_date": "2024-08-03"})
    assert summary.status_code == 200, summary.text
    assert summary.json()["total_attendance_records"] == 2
    assert summary.json()["completed_days"] == 2


def test_correction_rejects_invalid_time_order(client_and_db):
    client, _ = client_and_db
    client.post("/attendance/punch-in", json={"employee_id": "E500", "timestamp": "2024-08-03T09:00:00+05:30"})
    client.post("/attendance/punch-out", json={"employee_id": "E500", "timestamp": "2024-08-03T17:00:00+05:30"})
    response = client.post("/attendance/corrections", json={
        "employee_id": "E500", "work_date": "2024-08-03",
        "punch_out": "2024-08-03T08:00:00+05:30",
        "reason": "Test invalid order", "corrected_by": "HR01",
    })
    assert response.status_code == 422


def test_overnight_punch_out_closes_original_work_date(client_and_db):
    client, db = client_and_db
    start = client.post("/attendance/punch-in", json={"employee_id": "E600", "timestamp": "2024-08-03T23:30:00+05:30"})
    assert start.status_code == 201
    end = client.post("/attendance/punch-out", json={"employee_id": "E600", "timestamp": "2024-08-04T07:30:00+05:30"})
    assert end.status_code == 200
    assert end.json()["work_date"] == "2024-08-03"
    assert end.json()["worked_minutes"] == 480
    assert db.attendance.count_documents({"employee_id": "E600"}) == 1
