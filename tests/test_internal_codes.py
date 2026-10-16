"""
Internal time/cost codes: an assignment-shaped target that can never be invoiced.

The rules under test:
  - time, mileage, expenses and supplier invoices can all point at a code
  - a code and a client assignment are mutually exclusive on the same row
  - nothing tagged to a code can reach an invoice, and billable is forced off
  - the settings CRUD guards codes that are in use
"""
from datetime import date, timedelta

import pytest

from models import (db as _db, InternalCode, TimeEntry, Expense, MileageEntry,
                    SupplierInvoice, Invoice, DEFAULT_KM_RATE)


# ── Model ─────────────────────────────────────────────────────────────────────

def test_target_key_and_label_for_internal(db, sample_internal_code):
    e = TimeEntry(internal_code_id=sample_internal_code.id,
                  entry_date=date(2026, 3, 2), hours=4.0)
    _db.session.add(e)
    _db.session.commit()

    assert e.is_internal is True
    assert e.target_key == f"i:{sample_internal_code.id}"
    assert e.target_label == "SALES – Försäljning"


def test_target_key_and_label_for_assignment(db, sample_project):
    e = TimeEntry(project_id=sample_project.id, entry_date=date(2026, 3, 2), hours=4.0)
    _db.session.add(e)
    _db.session.commit()

    assert e.is_internal is False
    assert e.target_key == f"p:{sample_project.id}"
    assert e.target_label == "Test Client AB / Test Assignment"


def test_internal_time_has_no_rate(db, sample_internal_code):
    """Internal hours are never sold, so they are worth nothing on a rate basis."""
    e = TimeEntry(internal_code_id=sample_internal_code.id,
                  entry_date=date(2026, 3, 2), hours=8.0)
    _db.session.add(e)
    _db.session.commit()
    assert e.effective_rate == 0.0


def test_code_km_rate_falls_back_to_default(db):
    c = InternalCode(code="ADMIN", name="Administration")
    _db.session.add(c)
    _db.session.commit()
    assert c.get_km_rate() == DEFAULT_KM_RATE


# ── Time reporting ────────────────────────────────────────────────────────────

def test_save_row_against_internal_code(auth_client, db, sample_internal_code):
    week = date(2026, 3, 2)  # a Monday
    resp = auth_client.post("/time/save-row", data={
        "week": week.isoformat(),
        "target": f"i:{sample_internal_code.id}",
        f"hours_{week.isoformat()}": "6",
        "description": "Kundmöte prospekt",
    }, follow_redirects=True)
    assert resp.status_code == 200

    entry = TimeEntry.query.filter_by(internal_code_id=sample_internal_code.id).one()
    assert entry.hours == 6.0
    assert entry.project_id is None
    assert entry.hour_type_id is None
    assert entry.description == "Kundmöte prospekt"


def test_save_row_ignores_hour_type_on_internal_code(auth_client, db, sample_internal_code, sample_po):
    """PO hour types belong to client assignments; an internal row must not adopt one."""
    week = date(2026, 3, 2)
    ht_id = sample_po.hour_types[0].id
    auth_client.post("/time/save-row", data={
        "week": week.isoformat(),
        "target": f"i:{sample_internal_code.id}",
        "type_key": f"ht_{ht_id}",
        f"hours_{week.isoformat()}": "3",
    }, follow_redirects=True)

    entry = TimeEntry.query.filter_by(internal_code_id=sample_internal_code.id).one()
    assert entry.hour_type_id is None


def test_save_row_still_accepts_legacy_project_id(auth_client, db, sample_project):
    """Forms that predate the combined target select keep working."""
    week = date(2026, 3, 2)
    auth_client.post("/time/save-row", data={
        "week": week.isoformat(),
        "project_id": str(sample_project.id),
        f"hours_{week.isoformat()}": "7.5",
    }, follow_redirects=True)

    entry = TimeEntry.query.filter_by(project_id=sample_project.id).one()
    assert entry.hours == 7.5
    assert entry.internal_code_id is None


def test_save_row_requires_a_target(auth_client, db):
    resp = auth_client.post("/time/save-row", data={
        "week": "2026-03-02",
        "hours_2026-03-02": "4",
    }, follow_redirects=True)
    assert resp.status_code == 200
    assert TimeEntry.query.count() == 0


def test_save_row_rejects_inactive_code(auth_client, db, sample_internal_code):
    sample_internal_code.active = False
    _db.session.commit()

    auth_client.post("/time/save-row", data={
        "week": "2026-03-02",
        "target": f"i:{sample_internal_code.id}",
        "hours_2026-03-02": "4",
    }, follow_redirects=True)
    assert TimeEntry.query.count() == 0


def test_save_row_rejects_code_that_does_not_take_time(auth_client, db, sample_internal_code):
    sample_internal_code.track_time = False
    _db.session.commit()

    auth_client.post("/time/save-row", data={
        "week": "2026-03-02",
        "target": f"i:{sample_internal_code.id}",
        "hours_2026-03-02": "4",
    }, follow_redirects=True)
    assert TimeEntry.query.count() == 0


def test_same_week_rows_for_code_and_project_stay_separate(auth_client, db,
                                                           sample_internal_code, sample_project):
    """An internal row and an assignment row on the same day must not overwrite each other."""
    day = "2026-03-02"
    auth_client.post("/time/save-row", data={
        "week": day, "target": f"p:{sample_project.id}", f"hours_{day}": "5",
    }, follow_redirects=True)
    auth_client.post("/time/save-row", data={
        "week": day, "target": f"i:{sample_internal_code.id}", f"hours_{day}": "3",
    }, follow_redirects=True)

    assert TimeEntry.query.filter_by(project_id=sample_project.id).one().hours == 5.0
    assert TimeEntry.query.filter_by(internal_code_id=sample_internal_code.id).one().hours == 3.0


# ── Mileage ───────────────────────────────────────────────────────────────────

def test_mileage_against_internal_code_uses_code_rate(auth_client, db, sample_internal_code):
    auth_client.post("/mileage", data={
        "target": f"i:{sample_internal_code.id}",
        "entry_date": "2026-03-02",
        "km": "40",
        "description": "Parkering + resa mässa",
        "billable": "1",          # ignored: internal costs are never resold
    }, follow_redirects=True)

    m = MileageEntry.query.one()
    assert m.internal_code_id == sample_internal_code.id
    assert m.project_id is None
    assert m.km_rate == 19.0          # the code's own rate
    assert m.amount == 760.0
    assert m.billable is False


def test_mileage_rate_override_wins(auth_client, db, sample_internal_code):
    auth_client.post("/mileage", data={
        "target": f"i:{sample_internal_code.id}",
        "entry_date": "2026-03-02",
        "km": "10",
        "km_rate": "30",
    }, follow_redirects=True)
    assert MileageEntry.query.one().km_rate == 30.0


def test_mileage_requires_a_target(auth_client, db):
    auth_client.post("/mileage", data={
        "entry_date": "2026-03-02", "km": "10",
    }, follow_redirects=True)
    assert MileageEntry.query.count() == 0


# ── Invoicing is blocked ──────────────────────────────────────────────────────

def _make_invoice(auth_client, project, start, end):
    return auth_client.post("/invoices/new", data={
        "project_id": str(project.id),
        "period_start": start.isoformat(),
        "period_end": end.isoformat(),
    }, follow_redirects=True)


def test_internal_rows_never_reach_an_invoice(auth_client, db, sample_project,
                                              sample_po, sample_internal_code):
    start, end = date(2026, 3, 1), date(2026, 3, 31)
    ht = sample_po.hour_types[0]

    # One billable assignment row, plus internal time, mileage and an expense.
    _db.session.add_all([
        TimeEntry(project_id=sample_project.id, entry_date=date(2026, 3, 2),
                  hours=8.0, hour_type_id=ht.id),
        TimeEntry(internal_code_id=sample_internal_code.id,
                  entry_date=date(2026, 3, 3), hours=8.0),
        MileageEntry(internal_code_id=sample_internal_code.id,
                     entry_date=date(2026, 3, 4), km=100, km_rate=19.0,
                     billable=True, status="approved"),
        Expense(internal_code_id=sample_internal_code.id,
                expense_date=date(2026, 3, 5), merchant="P-hus",
                amount_excl_vat=80.0, vat_amount=20.0, amount_incl_vat=100.0,
                billable=True, status="approved"),
    ])
    _db.session.commit()

    resp = _make_invoice(auth_client, sample_project, start, end)
    assert resp.status_code == 200

    inv = Invoice.query.one()
    assert [e.hours for e in inv.time_entries] == [8.0]
    assert inv.expenses == []
    assert inv.mileage_entries == []
    # 8 h × 1500 and nothing else
    assert inv.subtotal == 12000.0

    internal_time = TimeEntry.query.filter_by(internal_code_id=sample_internal_code.id).one()
    assert internal_time.invoice_id is None
    assert internal_time.invoiced is False


# ── Settings CRUD ─────────────────────────────────────────────────────────────

def test_create_internal_code(auth_client, db):
    auth_client.post("/settings/internal-codes", data={
        "action": "new",
        "code": "utb",                # normalised to upper case
        "name": "Utbildning",
        "kind": "admin",
        "track_time": "1",
    }, follow_redirects=True)

    c = InternalCode.query.filter_by(code="UTB").one()
    assert c.name == "Utbildning"
    assert c.track_time is True
    assert c.track_cost is False
    assert c.km_rate is None


def test_duplicate_code_is_rejected(auth_client, db, sample_internal_code):
    auth_client.post("/settings/internal-codes", data={
        "action": "new", "code": "sales", "name": "Något annat",
    }, follow_redirects=True)
    assert InternalCode.query.filter_by(code="SALES").count() == 1


def test_code_in_use_cannot_be_deleted(auth_client, db, sample_internal_code):
    _db.session.add(TimeEntry(internal_code_id=sample_internal_code.id,
                              entry_date=date(2026, 3, 2), hours=1.0))
    _db.session.commit()

    auth_client.post("/settings/internal-codes", data={
        "action": "delete", "code_id": str(sample_internal_code.id),
    }, follow_redirects=True)
    assert InternalCode.query.get(sample_internal_code.id) is not None


def test_unused_code_can_be_deleted(auth_client, db, sample_internal_code):
    auth_client.post("/settings/internal-codes", data={
        "action": "delete", "code_id": str(sample_internal_code.id),
    }, follow_redirects=True)
    assert InternalCode.query.get(sample_internal_code.id) is None


def test_deactivate_keeps_history(auth_client, db, sample_internal_code):
    _db.session.add(TimeEntry(internal_code_id=sample_internal_code.id,
                              entry_date=date(2026, 3, 2), hours=1.0))
    _db.session.commit()

    auth_client.post("/settings/internal-codes", data={
        "action": "deactivate", "code_id": str(sample_internal_code.id),
    }, follow_redirects=True)

    c = InternalCode.query.get(sample_internal_code.id)
    assert c.active is False
    assert TimeEntry.query.filter_by(internal_code_id=c.id).count() == 1


# ── Pages render ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("path", [
    "/", "/time", "/time/monthly", "/mileage", "/expenses",
    "/internal", "/settings", "/settings/internal-codes",
])
def test_pages_render_with_internal_data(auth_client, db, sample_internal_code,
                                         sample_project, path):
    _db.session.add_all([
        TimeEntry(internal_code_id=sample_internal_code.id,
                  entry_date=date.today(), hours=4.0, description="Prospektering"),
        MileageEntry(internal_code_id=sample_internal_code.id,
                     entry_date=date.today(), km=25, km_rate=19.0,
                     billable=False, status="approved"),
        Expense(internal_code_id=sample_internal_code.id,
                expense_date=date.today(), merchant="P-hus Centralen",
                amount_excl_vat=80.0, vat_amount=20.0, amount_incl_vat=100.0,
                billable=False, status="approved"),
    ])
    _db.session.commit()

    resp = auth_client.get(path)
    assert resp.status_code == 200, f"{path} returned {resp.status_code}"


def test_internal_report_totals(auth_client, db, sample_internal_code):
    today = date.today()
    _db.session.add_all([
        TimeEntry(internal_code_id=sample_internal_code.id, entry_date=today, hours=4.0),
        MileageEntry(internal_code_id=sample_internal_code.id, entry_date=today,
                     km=100, km_rate=19.0, billable=False, status="approved"),
        Expense(internal_code_id=sample_internal_code.id, expense_date=today,
                merchant="P-hus", amount_excl_vat=80.0, vat_amount=20.0,
                amount_incl_vat=100.0, billable=False, status="approved"),
        SupplierInvoice(internal_code_id=sample_internal_code.id, supplier_name="Mässbolaget",
                        invoice_date=today, amount_excl_vat=2000.0, vat_amount=500.0,
                        amount_incl_vat=2500.0, status="approved"),
    ])
    _db.session.commit()

    resp = auth_client.get(f"/internal?from={today.isoformat()}&to={today.isoformat()}")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "SALES" in body
    # 1900 mileage + 80 expense + 2000 supplier, all excl. VAT
    assert "3 980" in body


def test_pending_expenses_excluded_from_report(auth_client, db, sample_internal_code):
    today = date.today()
    _db.session.add(Expense(internal_code_id=sample_internal_code.id, expense_date=today,
                            merchant="Okänd", amount_excl_vat=500.0, vat_amount=125.0,
                            amount_incl_vat=625.0, billable=False, status="pending"))
    _db.session.commit()

    resp = auth_client.get(f"/internal?from={today.isoformat()}&to={today.isoformat()}")
    assert "Ingen intern tid" in resp.get_data(as_text=True) \
        or "No internal time" in resp.get_data(as_text=True)
