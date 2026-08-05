"""
Tests for supplier invoice VAT treatments and the voucher rows they produce.
"""
from datetime import date

import pytest

from bookkeeping import (VAT_DOMESTIC, VAT_REVERSE_CHARGE_DOMESTIC,
                         VAT_REVERSE_CHARGE_EU_SERVICES, supplier_invoice_rows,
                         vat_treatment_of)
from models import SupplierInvoice


def _invoice(**kwargs):
    """A transient supplier invoice — the row builder never touches the session."""
    defaults = dict(
        supplier_name="Bike Mobility Services Sweden AB",
        invoice_date=date(2026, 8, 1),
        due_date=date(2026, 8, 31),
        vat_amount=0.0,
        service_amount_excl_vat=0.0,
        account_code=None,
    )
    defaults.update(kwargs)
    return SupplierInvoice(**defaults)


def _eu_invoice(net, service=0.0):
    return _invoice(amount_excl_vat=net, amount_incl_vat=net,
                    service_amount_excl_vat=service,
                    reverse_charge=True,
                    vat_treatment=VAT_REVERSE_CHARGE_EU_SERVICES)


def _by_account(rows):
    return {r["account"]: (r["debit"], r["credit"]) for r in rows}


def _assert_balances(rows):
    debit = round(sum(r["debit"] for r in rows), 2)
    credit = round(sum(r["credit"] for r in rows), 2)
    assert debit == credit, f"voucher does not balance: {debit} D vs {credit} C"


# ── Reverse charge, EU services ───────────────────────────────────────────────

def test_eu_services_25_pct_only():
    """AC1 — a 342.00 SEK leasing invoice books exactly 4535 / 2440 / 2614 / 2645."""
    rows = supplier_invoice_rows(_eu_invoice(342.00))

    assert [r["account"] for r in rows] == [4535, 2440, 2614, 2645]
    assert _by_account(rows) == {
        4535: (342.00, 0.0),
        2440: (0.0, 342.00),
        2614: (0.0, 85.50),
        2645: (85.50, 0.0),
    }
    _assert_balances(rows)


def test_eu_services_25_and_12_pct_split():
    """AC2 — 300.00 leasing + 42.00 service splits across both rates."""
    rows = supplier_invoice_rows(_eu_invoice(342.00, service=42.00))

    assert _by_account(rows) == {
        4535: (300.00, 0.0),
        4536: (42.00, 0.0),
        2440: (0.0, 342.00),
        2614: (0.0, 75.00),
        2624: (0.0, 5.04),
        2645: (80.04, 0.0),
    }
    _assert_balances(rows)
    assert 3740 not in _by_account(rows)  # nothing to round off


def test_eu_services_never_uses_car_leasing_or_domestic_vat_accounts():
    """AC3 — 5615 (car leasing) and 2647 (domestic RC input VAT) are out."""
    for inv in (_eu_invoice(342.00), _eu_invoice(342.00, service=42.00)):
        accounts = {r["account"] for r in supplier_invoice_rows(inv)}
        assert 5615 not in accounts
        assert 2647 not in accounts


def test_eu_services_ignores_supplier_category_account():
    """The cost account follows the VAT treatment, not the category (5615 → 4535)."""
    inv = _eu_invoice(342.00)
    inv.account_code = "5615"
    accounts = {r["account"] for r in supplier_invoice_rows(inv)}
    assert 4535 in accounts and 5615 not in accounts


@pytest.mark.parametrize("net,service", [
    (333.33, 0.0),      # 25% of an odd net — 83.3325
    (100.00, 33.33),    # both rates land on a half öre
    (0.07, 0.07),       # 12% of 0.07 rounds to a single öre
    (999.99, 0.01),
])
def test_eu_services_rounding_balances(net, service):
    """AC5 — VAT is rounded per line and the voucher still balances."""
    rows = supplier_invoice_rows(_eu_invoice(net, service=service))
    _assert_balances(rows)
    for r in rows:
        assert r["debit"] == round(r["debit"], 2)
        assert r["credit"] == round(r["credit"], 2)
    by_account = _by_account(rows)
    output_vat = round(by_account.get(2614, (0, 0))[1] + by_account.get(2624, (0, 0))[1], 2)
    assert by_account[2645][0] == output_vat  # full deduction, box 48 == box 30+31


def test_eu_services_rounding_row_added_when_amounts_do_not_meet():
    """A net/total mismatch inside the form's 0.05 tolerance lands on 3740."""
    inv = _eu_invoice(342.00)
    inv.amount_incl_vat = 342.02
    rows = supplier_invoice_rows(inv)
    _assert_balances(rows)
    assert _by_account(rows)[3740] == (0.02, 0.0)


def test_eu_services_service_component_capped_at_net():
    inv = _eu_invoice(342.00, service=500.00)
    by_account = _by_account(supplier_invoice_rows(inv))
    assert 4535 not in by_account
    assert by_account[4536] == (342.00, 0.0)
    _assert_balances(supplier_invoice_rows(inv))


# ── Regression: the untouched treatments ──────────────────────────────────────

def test_domestic_invoice_with_deductible_vat_unchanged():
    inv = _invoice(amount_excl_vat=800.00, vat_amount=200.00, amount_incl_vat=1000.00,
                   account_code="6540", vat_treatment=VAT_DOMESTIC)
    rows = supplier_invoice_rows(inv)
    assert [r["account"] for r in rows] == [6540, 2641, 2440]
    assert _by_account(rows) == {
        6540: (800.00, 0.0),
        2641: (200.00, 0.0),
        2440: (0.0, 1000.00),
    }
    _assert_balances(rows)


def test_domestic_reverse_charge_still_books_2614_2647():
    inv = _invoice(amount_excl_vat=342.00, amount_incl_vat=342.00, account_code="5615",
                   reverse_charge=True, vat_treatment=VAT_REVERSE_CHARGE_DOMESTIC)
    assert _by_account(supplier_invoice_rows(inv)) == {
        5615: (342.00, 0.0),
        2647: (85.50, 0.0),
        2614: (0.0, 85.50),
        2440: (0.0, 342.00),
    }


def test_legacy_row_without_treatment_falls_back_to_domestic():
    """Rows predating the column carry only the boolean flag."""
    plain = _invoice(amount_excl_vat=100.0, vat_amount=25.0, amount_incl_vat=125.0,
                     vat_treatment=None)
    rc = _invoice(amount_excl_vat=342.0, amount_incl_vat=342.0,
                  reverse_charge=True, vat_treatment=None)
    assert vat_treatment_of(plain) == VAT_DOMESTIC
    assert vat_treatment_of(rc) == VAT_REVERSE_CHARGE_DOMESTIC
    assert 2647 in _by_account(supplier_invoice_rows(rc))


# ── Review form ───────────────────────────────────────────────────────────────

def _review_form(**overrides):
    data = {
        "supplier_name": "Bike Mobility Services Sweden AB",
        "invoice_date": "2026-08-01",
        "due_date": "2026-08-31",
        "amount_excl_vat": "342.00",
        "vat_amount": "0",
        "amount_incl_vat": "342.00",
        "payment_ref": "1234567890",
        "payment_type": "bg",
        "payment_account": "121-8114",
        "currency": "SEK",
        "vat_treatment": VAT_REVERSE_CHARGE_EU_SERVICES,
    }
    data.update(overrides)
    return data


@pytest.fixture
def pending_invoice(db):
    inv = SupplierInvoice(supplier_name="Bike Mobility Services Sweden AB",
                          amount_incl_vat=342.00, status="pending")
    db.session.add(inv)
    db.session.commit()
    return inv


def test_review_page_offers_all_three_treatments(auth_client, db, pending_invoice):
    r = auth_client.get(f"/supplier-invoices/{pending_invoice.id}/review")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    for treatment in (VAT_DOMESTIC, VAT_REVERSE_CHARGE_DOMESTIC, VAT_REVERSE_CHARGE_EU_SERVICES):
        assert f'value="{treatment}"' in body
    assert 'name="service_amount_excl_vat"' in body


def test_review_saves_eu_services_treatment(auth_client, db, pending_invoice):
    auth_client.post(f"/supplier-invoices/{pending_invoice.id}/review",
                     data=_review_form(service_amount_excl_vat="42.00"),
                     follow_redirects=True)
    inv = SupplierInvoice.query.get(pending_invoice.id)
    assert inv.vat_treatment == VAT_REVERSE_CHARGE_EU_SERVICES
    assert inv.reverse_charge is True
    assert inv.service_amount_excl_vat == 42.00
    assert inv.amount_excl_vat == 342.00 and inv.vat_amount == 0.0
    assert inv.status == "approved"


def test_review_rejects_service_amount_above_net(auth_client, db, pending_invoice):
    r = auth_client.post(f"/supplier-invoices/{pending_invoice.id}/review",
                         data=_review_form(service_amount_excl_vat="400.00"),
                         follow_redirects=True)
    assert r.status_code == 200
    inv = SupplierInvoice.query.get(pending_invoice.id)
    assert inv.status == "pending"  # not saved


def test_review_clears_service_amount_when_not_eu_services(auth_client, db, pending_invoice):
    auth_client.post(f"/supplier-invoices/{pending_invoice.id}/review",
                     data=_review_form(vat_treatment=VAT_REVERSE_CHARGE_DOMESTIC,
                                       service_amount_excl_vat="42.00"),
                     follow_redirects=True)
    inv = SupplierInvoice.query.get(pending_invoice.id)
    assert inv.vat_treatment == VAT_REVERSE_CHARGE_DOMESTIC
    assert inv.service_amount_excl_vat == 0.0


def test_review_accepts_legacy_reverse_charge_checkbox(auth_client, db, pending_invoice):
    form = _review_form()
    form.pop("vat_treatment")
    form["reverse_charge"] = "1"
    auth_client.post(f"/supplier-invoices/{pending_invoice.id}/review",
                     data=form, follow_redirects=True)
    inv = SupplierInvoice.query.get(pending_invoice.id)
    assert inv.vat_treatment == VAT_REVERSE_CHARGE_DOMESTIC
    assert inv.reverse_charge is True
