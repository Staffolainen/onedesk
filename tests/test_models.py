"""
Unit tests for model logic — no HTTP involved.
These are the fastest tests and should cover all business logic.
"""
from datetime import date
from models import (
    db, Client, Project, PurchaseOrder, POHourType,
    TimeEntry, Invoice, MileageEntry, fiscal_year, fy_start, fy_end
)


# ── Fiscal year helpers ───────────────────────────────────────────────────────

def test_fiscal_year_before_start_month():
    # Jan 2026 is still FY2025 (start month = May)
    assert fiscal_year(date(2026, 1, 15)) == 2025

def test_fiscal_year_at_start_month():
    # May 2026 starts FY2026
    assert fiscal_year(date(2026, 5, 1)) == 2026

def test_fiscal_year_after_start_month():
    assert fiscal_year(date(2026, 8, 10)) == 2026

def test_fy_start():
    assert fy_start(2025) == date(2025, 5, 1)

def test_fy_end():
    assert fy_end(2025) == date(2026, 4, 30)


# ── Project rate resolution ───────────────────────────────────────────────────

def test_get_active_po_within_range(app, sample_project, sample_po):
    with app.app_context():
        p = Project.query.get(sample_project.id)
        po = p.get_active_po(date(2026, 6, 1))
        assert po is not None
        assert po.po_number == "PO-001"

def test_get_active_po_outside_range(app, sample_project, sample_po):
    with app.app_context():
        p = Project.query.get(sample_project.id)
        po = p.get_active_po(date(2025, 1, 1))  # before valid_from
        assert po is None

def test_get_hourly_rate_falls_back_to_client(app, sample_project, sample_client):
    with app.app_context():
        # No PO active for this date
        p = Project.query.get(sample_project.id)
        rate = p.get_hourly_rate(date(2025, 1, 1))
        assert rate == sample_client.hourly_rate

def test_get_hourly_rate_uses_po(app, sample_project, sample_po):
    with app.app_context():
        p = Project.query.get(sample_project.id)
        rate = p.get_hourly_rate(date(2026, 6, 1))
        assert rate == sample_po.hourly_rate

def test_get_km_rate_uses_po(app, sample_project, sample_po):
    with app.app_context():
        p = Project.query.get(sample_project.id)
        rate = p.get_km_rate(date(2026, 6, 1))
        assert rate == 30.0  # PO km_rate overrides client

def test_get_km_rate_falls_back_to_client(app, sample_project, sample_client):
    with app.app_context():
        p = Project.query.get(sample_project.id)
        rate = p.get_km_rate(date(2025, 1, 1))  # no PO active
        assert rate == sample_client.km_rate


# ── Invoice.line_groups() ─────────────────────────────────────────────────────

def _make_invoice_with_entries(db, sample_project, sample_po):
    """Helper: create an invoice with two time entries in the same month."""
    inv = Invoice(
        client_id=sample_project.client_id,
        project_id=sample_project.id,
        invoice_number="2025-001",
        period_start=date(2026, 3, 1),
        period_end=date(2026, 3, 31),
        subtotal=3000.0, vat_amount=750.0, total=3750.0,
    )
    db.session.add(inv)
    db.session.flush()

    ht = POHourType.query.filter_by(po_id=sample_po.id).first()
    e1 = TimeEntry(project_id=sample_project.id, entry_date=date(2026, 3, 1),
                   hours=8.0, hour_type_id=ht.id, invoice_id=inv.id)
    e2 = TimeEntry(project_id=sample_project.id, entry_date=date(2026, 3, 2),
                   hours=8.0, hour_type_id=ht.id, invoice_id=inv.id)
    db.session.add_all([e1, e2])
    db.session.commit()
    return Invoice.query.get(inv.id)


def test_line_groups_basic(app, db, sample_project, sample_po):
    with app.app_context():
        inv = _make_invoice_with_entries(db, sample_project, sample_po)
        groups = inv.line_groups()
        assert len(groups) == 1
        assert groups[0]["hours"] == 16.0
        assert groups[0]["year"] == 2026
        assert groups[0]["month"] == 3


def test_line_groups_show_po_false_when_single_po(app, db, sample_project, sample_po):
    with app.app_context():
        inv = _make_invoice_with_entries(db, sample_project, sample_po)
        groups = inv.line_groups()
        # Only one PO in the period — no need to show PO number
        assert groups[0]["show_po"] is False


def test_line_groups_amount(app, db, sample_project, sample_po):
    with app.app_context():
        inv = _make_invoice_with_entries(db, sample_project, sample_po)
        groups = inv.line_groups()
        assert groups[0]["amount"] == 16.0 * 1500.0


def test_line_groups_empty_invoice(app, db, sample_project):
    with app.app_context():
        inv = Invoice(
            client_id=sample_project.client_id,
            project_id=sample_project.id,
            invoice_number="2025-002",
            period_start=date(2026, 3, 1),
            period_end=date(2026, 3, 31),
            subtotal=0, vat_amount=0, total=0,
        )
        db.session.add(inv)
        db.session.commit()
        inv = Invoice.query.get(inv.id)
        assert inv.line_groups() == []


# ── MileageEntry.amount ───────────────────────────────────────────────────────

def test_mileage_amount(app, db, sample_project):
    with app.app_context():
        m = MileageEntry(
            project_id=sample_project.id,
            entry_date=date(2026, 3, 1),
            km=100.0, km_rate=30.0,
        )
        db.session.add(m)
        db.session.commit()
        assert m.amount == 3000.0


# ── Project.generate_number() ─────────────────────────────────────────────────

def test_generate_number_first(app, db):
    with app.app_context():
        num = Project.generate_number(year=2026)
        assert num == "P2601"

def test_generate_number_increments(app, db, sample_project):
    with app.app_context():
        # sample_project has P2601
        num = Project.generate_number(year=2026)
        assert num == "P2602"
