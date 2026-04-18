"""
Tests for invoice creation, approval, and locking.
"""
from datetime import date
from models import TimeEntry, Invoice, db as _db


def _seed_time_entries(db, sample_project, sample_po):
    ht = sample_po.hour_types[0]
    entries = [
        TimeEntry(project_id=sample_project.id,
                  entry_date=date(2026, 3, d),
                  hours=8.0, hour_type_id=ht.id)
        for d in [2, 3, 4, 5, 6]
    ]
    db.session.add_all(entries)
    db.session.commit()


def test_invoices_index_loads(auth_client):
    r = auth_client.get("/invoices")
    assert r.status_code == 200


def test_invoice_new_page_loads(auth_client):
    r = auth_client.get("/invoices/new")
    assert r.status_code == 200


def test_create_invoice(auth_client, app, db, sample_project, sample_po):
    _seed_time_entries(db, sample_project, sample_po)
    r = auth_client.post("/invoices/new", data={
        "project_id": sample_project.id,
        "period_start": "2026-03-01",
        "period_end": "2026-03-31",
    }, follow_redirects=True)
    assert r.status_code == 200
    inv = Invoice.query.first()
    assert inv is not None
    assert inv.project_id == sample_project.id


def test_invoice_contains_time_entries(auth_client, app, db, sample_project, sample_po):
    _seed_time_entries(db, sample_project, sample_po)
    auth_client.post("/invoices/new", data={
        "project_id": sample_project.id,
        "period_start": "2026-03-01",
        "period_end": "2026-03-31",
    }, follow_redirects=True)
    inv = Invoice.query.first()
    assert inv is not None
    assert len(inv.time_entries) == 5
    assert inv.subtotal == 5 * 8.0 * 1500.0


def test_approve_invoice_locks_time_entries(auth_client, app, db, sample_project, sample_po):
    _seed_time_entries(db, sample_project, sample_po)
    auth_client.post("/invoices/new", data={
        "project_id": sample_project.id,
        "period_start": "2026-03-01",
        "period_end": "2026-03-31",
    }, follow_redirects=True)
    inv = Invoice.query.first()
    assert inv is not None
    auth_client.post(f"/invoices/{inv.id}/approve", follow_redirects=True)
    db.session.refresh(inv)
    entries = TimeEntry.query.filter_by(invoice_id=inv.id).all()
    assert all(e.invoiced for e in entries), "All entries should be locked after approval"


def test_approve_invoice_changes_status(auth_client, app, db, sample_project, sample_po):
    _seed_time_entries(db, sample_project, sample_po)
    auth_client.post("/invoices/new", data={
        "project_id": sample_project.id,
        "period_start": "2026-03-01",
        "period_end": "2026-03-31",
    }, follow_redirects=True)
    inv = Invoice.query.first()
    assert inv is not None
    auth_client.post(f"/invoices/{inv.id}/approve", follow_redirects=True)
    db.session.refresh(inv)
    assert inv.status == "approved"


def test_invoice_number_format(auth_client, app, db, sample_project, sample_po):
    _seed_time_entries(db, sample_project, sample_po)
    auth_client.post("/invoices/new", data={
        "project_id": sample_project.id,
        "period_start": "2026-03-01",
        "period_end": "2026-03-31",
    }, follow_redirects=True)
    inv = Invoice.query.first()
    assert inv is not None
    # March 2026 is FY2025 (fiscal year starts May)
    assert inv.invoice_number.startswith("2025-")


def test_create_invoice_invalid_dates(auth_client, sample_project):
    r = auth_client.post("/invoices/new", data={
        "project_id": sample_project.id,
        "period_start": "not-a-date",
        "period_end": "2026-03-31",
    }, follow_redirects=True)
    assert r.status_code == 200  # should handle gracefully, not crash
