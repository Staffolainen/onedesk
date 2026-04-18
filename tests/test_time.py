"""
Tests for time tracking — week view, saving entries, invoiced entry protection.
"""
from datetime import date
from models import TimeEntry


def test_time_index_loads(auth_client):
    r = auth_client.get("/time")
    assert r.status_code == 200


def test_time_index_week_navigation(auth_client):
    r = auth_client.get("/time?week=2026-03-02")
    assert r.status_code == 200


def test_time_save_row_creates_entry(auth_client, db, sample_project, sample_po):
    ht = sample_po.hour_types[0]
    r = auth_client.post("/time/save-row", data={
        "week": "2026-03-02",
        "project_id": sample_project.id,
        "hour_type_id": ht.id,
        "hours_2026-03-02": "8",
    }, follow_redirects=True)
    assert r.status_code == 200
    entry = TimeEntry.query.filter_by(
        project_id=sample_project.id,
        entry_date=date(2026, 3, 2)
    ).first()
    assert entry is not None
    assert entry.hours == 8.0


def test_time_save_row_zero_clears_entry(auth_client, db, sample_project, sample_po):
    ht = sample_po.hour_types[0]
    # Create entry first
    e = TimeEntry(project_id=sample_project.id, entry_date=date(2026, 3, 3),
                  hours=8.0, hour_type_id=ht.id)
    db.session.add(e)
    db.session.commit()

    auth_client.post("/time/save-row", data={
        "week": "2026-03-02",
        "project_id": sample_project.id,
        "hour_type_id": ht.id,
        "hours_2026-03-03": "0",
    }, follow_redirects=True)

    entry = TimeEntry.query.filter_by(
        project_id=sample_project.id,
        entry_date=date(2026, 3, 3)
    ).first()
    assert entry is None


def test_time_monthly_loads(auth_client):
    r = auth_client.get("/time/monthly?year=2026&month=3")
    assert r.status_code == 200


def test_cannot_edit_invoiced_entry(auth_client, db, sample_project, sample_po):
    ht = sample_po.hour_types[0]
    e = TimeEntry(project_id=sample_project.id, entry_date=date(2026, 3, 4),
                  hours=8.0, hour_type_id=ht.id, invoiced=True)
    db.session.add(e)
    db.session.commit()
    entry_id = e.id

    # Attempt to overwrite invoiced entry
    auth_client.post("/time/save-row", data={
        "week": "2026-03-02",
        "project_id": sample_project.id,
        "hour_type_id": ht.id,
        "hours_2026-03-04": "4",
    })

    db.session.refresh(e)
    assert e.hours == 8.0  # unchanged
