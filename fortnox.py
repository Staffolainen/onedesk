"""
Fortnox REST API client.
Docs: https://developer.fortnox.se/documentation/
"""
import os
import base64
import json
import logging
import requests
from urllib.parse import urlencode
from models import Settings

logger = logging.getLogger(__name__)


class FortnoxClient:
    BASE_URL = "https://api.fortnox.se/3"
    AUTH_URL = "https://apps.fortnox.se/oauth-v1/auth"
    TOKEN_URL = "https://apps.fortnox.se/oauth-v1/token"

    def __init__(self, config):
        self.client_id = config["FORTNOX_CLIENT_ID"]
        self.client_secret = config["FORTNOX_CLIENT_SECRET"]
        self.redirect_uri = config["FORTNOX_REDIRECT_URI"]

    def _get_token(self):
        """Get current access token, refreshing if needed."""
        token = Settings.get("fortnox_access_token")
        if not token:
            raise Exception("Fortnox not connected. Visit /fortnox/connect")
        return token

    def _headers(self):
        return {
            "Authorization": f"Bearer {self._get_token()}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _request(self, method, path, **kwargs):
        url = f"{self.BASE_URL}{path}"
        logger.debug("Fortnox API — %s %s", method, url)
        resp = requests.request(method, url, headers=self._headers(), **kwargs)
        logger.debug("Fortnox API — %s %s → %s", method, url, resp.status_code)
        if resp.status_code == 401:
            logger.info("Fortnox API — 401, attempting token refresh")
            self._refresh_token()
            resp = requests.request(method, url, headers=self._headers(), **kwargs)
            logger.debug("Fortnox API — retry %s %s → %s", method, url, resp.status_code)
        if not resp.ok:
            detail = ""
            try:
                err = resp.json()
                detail = err.get("ErrorInformation", {}).get("Message", "") or str(err)
            except Exception:
                detail = resp.text[:300]
            raise Exception(f"Fortnox {resp.status_code}: {detail}")
        return resp.json() if resp.content else {}

    def _refresh_token(self):
        refresh_token = Settings.get("fortnox_refresh_token")
        if not refresh_token:
            raise Exception("No refresh token available. Please reconnect Fortnox.")
        credentials = base64.b64encode(
            f"{self.client_id}:{self.client_secret}".encode()
        ).decode()
        logger.info("Fortnox refresh_token — POST %s", self.TOKEN_URL)
        resp = requests.post(self.TOKEN_URL, data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        }, headers={
            "Authorization": f"Basic {credentials}",
            "Content-Type": "application/x-www-form-urlencoded",
        })
        logger.debug("Fortnox refresh_token — status=%s", resp.status_code)
        resp.raise_for_status()
        data = resp.json()
        Settings.set("fortnox_access_token", data["access_token"])
        if "refresh_token" in data:
            Settings.set("fortnox_refresh_token", data["refresh_token"])

    def get_auth_url(self):
        params = {
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "scope": "bookkeeping companyinformation archive",
            "state": "onedesk",
            "response_type": "code",
            "access_type": "offline",
        }
        return f"{self.AUTH_URL}?{urlencode(params)}"

    def exchange_code(self, code):
        credentials = base64.b64encode(
            f"{self.client_id}:{self.client_secret}".encode()
        ).decode()
        payload = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self.redirect_uri,
        }
        logger.info("Fortnox exchange_code — POST %s payload=%s", self.TOKEN_URL, payload)
        resp = requests.post(self.TOKEN_URL, data=payload, headers={
            "Authorization": f"Basic {credentials}",
            "Content-Type": "application/x-www-form-urlencoded",
        })
        logger.info("Fortnox exchange_code response — status=%s body=%s",
                     resp.status_code, resp.text)
        resp.raise_for_status()
        return resp.json()

    # ── Payment sync ─────────────────────────────────────────────────────────

    def get_paid_invoice_numbers(self) -> set:
        """
        Return a set of invoice numbers found in Fortnox C-series vouchers.
        C-series = Kundbetalningar (customer payments).
        Looks at TransactionInformation on each voucher row for invoice number patterns.
        """
        import re
        paid = set()
        try:
            data = self._request("GET", "/vouchers",
                                 params={"filter": "lastmonth", "voucherseries": "C"})
            vouchers = data.get("Vouchers", [])
            # Fetch full details for each voucher to read row descriptions
            for v in vouchers:
                nr = v.get("VoucherNumber")
                try:
                    detail = self._request("GET", f"/vouchers/C/{nr}")
                    rows = detail.get("Voucher", {}).get("VoucherRows", [])
                    desc = detail.get("Voucher", {}).get("Description", "")
                    text = desc + " " + " ".join(r.get("TransactionInformation", "") for r in rows)
                    # Match invoice number pattern e.g. 2025-001 or 2025001
                    for match in re.findall(r"\d{4}-\d{3}", text):
                        paid.add(match)
                except Exception:
                    pass
        except Exception as e:
            logger.warning("Fortnox payment sync failed: %s", e)
        return paid

    # ── Financial years ───────────────────────────────────────────────────────

    def get_financial_year_id(self, for_date) -> int | None:
        """Return the Fortnox FinancialYear ID that contains for_date, or None."""
        try:
            data = self._request("GET", "/financialyears")
            for fy in data.get("FinancialYears", []):
                from datetime import date as _date
                from_d = _date.fromisoformat(fy["FromDate"])
                to_d   = _date.fromisoformat(fy["ToDate"])
                if from_d <= for_date <= to_d:
                    return fy["Id"]
        except Exception:
            pass
        return None

    # ── Customers ─────────────────────────────────────────────────────────────

    def get_or_create_customer(self, client):
        """Return Fortnox customer number, creating if needed."""
        if client.fortnox_customer_nr:
            return client.fortnox_customer_nr

        payload = {"Customer": {
            "Name": client.name,
            "OrganisationNumber": client.org_nr or "",
            "Email": client.contact_email or "",
            "Address1": (client.address or "").split("\n")[0],
            "VATNumber": client.vat_nr or "",
            "Currency": client.currency,
            "TermsOfPayment": str(client.payment_days),
        }}
        result = self._request("POST", "/customers", json=payload)
        nr = result["Customer"]["CustomerNumber"]
        from models import db
        client.fortnox_customer_nr = nr
        db.session.commit()
        return nr

    # ── Outgoing invoice voucher ──────────────────────────────────────────────

    def create_outgoing_invoice_voucher(self, invoice):
        """
        Post a sales voucher to Fortnox for an outgoing invoice we generated ourselves.
        Account structure (standard Swedish BAS):
          Debit  1510  Total incl. VAT    (Kundfordringar)
          Credit 3001  Amount excl. VAT   (Försäljning Sverige 25% moms)
          Credit 2610  VAT amount         (Utgående moms 25%)
          Debit/Credit 3740  Rounding     (Öresavrundning, if any)

        Returns the full API response dict.
        """
        from datetime import date as _date

        voucher_date = invoice.issue_date or _date.today()
        fy_id = self.get_financial_year_id(voucher_date)
        if not fy_id:
            raise Exception(f"No financial year found in Fortnox for date {voucher_date}. "
                            "Please create a financial year in Fortnox first.")

        excl_vat = round(invoice.subtotal, 2)
        vat      = round(invoice.vat_amount, 2)
        total    = round(invoice.total, 2)
        rounding = round(total - excl_vat - vat, 2)

        def _row(account, debit, credit, info=""):
            r = {
                "Account": int(account),
                "Debit":   round(debit, 2),
                "Credit":  round(credit, 2),
            }
            if info:
                r["TransactionInformation"] = info
            return r

        description = f"Faktura {invoice.invoice_number} {invoice.client.name}"[:200]

        rows = [
            _row(1510, total,    0,       description),
            _row(3001, 0,        excl_vat, f"Period {invoice.period_start} – {invoice.period_end}"),
            _row(2610, 0,        vat,      "Utgående moms 25%"),
        ]
        if rounding != 0:
            rows.append(_row(3740,
                             max(-rounding, 0),
                             max(rounding, 0),
                             "Öresavrundning"))

        voucher_data = {
            "Description": description,
            "TransactionDate": voucher_date.isoformat(),
            "VoucherSeries": "B",
            "VoucherRows": rows,
        }

        result = self._request("POST", "/vouchers", json={"Voucher": voucher_data},
                               params={"financialyear": fy_id})
        voucher = result.get("Voucher", {})
        voucher_nr = voucher.get("VoucherNumber")
        voucher_series = voucher.get("VoucherSeries", "B")
        voucher_ref = f"{voucher_date.year}-{voucher_series}{voucher_nr}" if voucher_nr else None
        logger.info("Fortnox voucher created — ref=%s", voucher_ref)

        # NOTE: PDF attachment to Fortnox vouchers requires Arkiv + file connection licenses
        # not included in the current Fortnox plan. PDF is stored in onedesk at /uploads/.
        logger.info("Fortnox voucher %s created — PDF stored locally as %s",
                    voucher_ref, invoice.pdf_filename)

        return result

    # ── Invoices (kept for reference — not used for live posting) ─────────────

    def create_invoice(self, invoice):
        """Create invoice in Fortnox and return invoice number."""
        client = invoice.client
        customer_nr = self.get_or_create_customer(client)

        rows = []

        # Time entries as invoice rows
        if invoice.time_entries:
            total_hours = sum(e.hours for e in invoice.time_entries)
            rows.append({
                "ArticleNumber": "TID",
                "Description": f"Konsulttjänster {invoice.period_start} – {invoice.period_end}",
                "DeliveredQuantity": str(total_hours),
                "Price": str(client.hourly_rate),
                "VAT": "25",
                "Unit": "tim",
            })

        # Expense rows — apply project markup and always bill at 25% VAT
        exp_markup_pct = (invoice.project.expense_markup_pct or 10.0) if invoice.project else 10.0
        markup_factor = 1 + exp_markup_pct / 100
        for exp in invoice.expenses:
            marked_up = round(float(exp.amount_excl_vat) * markup_factor, 2)
            detail = exp.description or exp.merchant or "Utlägg"
            rows.append({
                "Description": f"Utlägg (+{int(exp_markup_pct)}%) – {detail}",
                "DeliveredQuantity": "1",
                "Price": str(marked_up),
                "VAT": "25",
            })

        fy_id = self.get_financial_year_id(invoice.issue_date)
        inv_payload = {
            "CustomerNumber": customer_nr,
            "InvoiceDate": invoice.issue_date.isoformat(),
            "DueDate": invoice.due_date.isoformat(),
            "Currency": invoice.currency,
            "Language": "SV" if invoice.language == "sv" else "EN",
            "InvoiceRows": rows,
            "Remarks": invoice.notes or "",
        }
        if fy_id:
            inv_payload["FinancialYear"] = fy_id
        payload = {"Invoice": inv_payload}

        result = self._request("POST", "/invoices", json=payload)
        fortnox_nr = result["Invoice"]["DocumentNumber"]

        return result

    # ── Expenses / Vouchers ───────────────────────────────────────────────────

    def create_expense_voucher(self, expense):
        """Create a supplier invoice / voucher for an expense."""
        fy_id = self.get_financial_year_id(expense.expense_date)
        if not fy_id:
            raise Exception(f"No financial year found in Fortnox for date {expense.expense_date}.")

        # Debit account: from category if set, else fallback to 5410 Diverse inköp
        debit_account = 5410
        if expense.expense_category and expense.expense_category.debit_account:
            debit_account = int(expense.expense_category.debit_account)

        # Credit account: 1930 company card, 2893 personal card
        credit_account = 2893 if expense.paid_by == "personal" else 1930
        vat_account = 2641  # Debiterad ingående moms

        rows = [
            {
                "Account": debit_account,
                "Debit": round(float(expense.amount_excl_vat), 2),
                "Credit": 0,
                "TransactionInformation": (expense.description or "")[:200],
            },
        ]
        if float(expense.vat_amount or 0) > 0:
            rows.append({
                "Account": vat_account,
                "Debit": round(float(expense.vat_amount), 2),
                "Credit": 0,
            })
        rows.append({
            "Account": credit_account,
            "Debit": 0,
            "Credit": round(float(expense.amount_incl_vat), 2),
        })

        voucher_data = {
            "Description": (expense.description or expense.merchant or "Utlägg")[:200],
            "TransactionDate": expense.expense_date.isoformat(),
            "VoucherSeries": "A",
            "VoucherRows": rows,
        }
        result = self._request("POST", "/vouchers", json={"Voucher": voucher_data},
                               params={"financialyear": fy_id})
        voucher = result.get("Voucher", {})
        voucher_nr = voucher.get("VoucherNumber")
        voucher_series = voucher.get("VoucherSeries", "A")
        voucher_ref = f"{expense.expense_date.year}-{voucher_series}{voucher_nr}" if voucher_nr else None
        logger.info("Fortnox expense voucher created — ref=%s", voucher_ref)

        from models import db
        expense.fortnox_voucher_nr = voucher_ref
        db.session.commit()
        return voucher_ref

    def create_supplier_invoice_voucher(self, inv):
        """Book a supplier invoice: debit=category, VAT=2641, credit=1930."""
        voucher_date = inv.invoice_date or inv.due_date
        if not voucher_date:
            from datetime import date as _date
            voucher_date = _date.today()
        fy_id = self.get_financial_year_id(voucher_date)
        if not fy_id:
            raise Exception(f"No financial year found in Fortnox for date {voucher_date}.")

        debit_account = 6540  # fallback
        if inv.supplier_category and inv.supplier_category.debit_account:
            debit_account = int(inv.supplier_category.debit_account)
        elif inv.account_code:
            debit_account = int(inv.account_code)

        rows = [
            {
                "Account": debit_account,
                "Debit": round(float(inv.amount_excl_vat or 0), 2),
                "Credit": 0,
                "TransactionInformation": (inv.supplier_name or "")[:200],
            },
        ]
        if float(inv.vat_amount or 0) > 0:
            rows.append({
                "Account": 2641,
                "Debit": round(float(inv.vat_amount), 2),
                "Credit": 0,
            })
        rows.append({
            "Account": 2440,  # Leverantörsskulder — cleared when payment is made
            "Debit": 0,
            "Credit": round(float(inv.amount_incl_vat or 0), 2),
        })

        description = f"{inv.supplier_name or 'Leverantör'} {inv.payment_ref or ''}".strip()
        voucher_data = {
            "Description": description[:200],
            "TransactionDate": voucher_date.isoformat(),
            "VoucherSeries": "A",
            "VoucherRows": rows,
        }
        result = self._request("POST", "/vouchers", json={"Voucher": voucher_data},
                               params={"financialyear": fy_id})
        voucher = result.get("Voucher", {})
        voucher_nr = voucher.get("VoucherNumber")
        voucher_series = voucher.get("VoucherSeries", "A")
        voucher_ref = f"{voucher_date.year}-{voucher_series}{voucher_nr}" if voucher_nr else None
        logger.info("Fortnox supplier invoice voucher created — ref=%s", voucher_ref)

        from models import db
        inv.fortnox_voucher_nr = voucher_ref
        inv.status = "booked"
        db.session.commit()
        return voucher_ref

    def create_payment_voucher(self, invoice_ids, payment_date):
        """Post payment: debit 2440 (Leverantörsskulder) → credit 1930 (Bank) per invoice.
        Called when a payment file is confirmed uploaded to the bank.
        Accepts a list of invoice IDs (not ORM objects) to avoid stale session data.
        """
        from datetime import date as _date
        from models import db, SupplierInvoice
        if not payment_date:
            raise Exception("Inget betalningsdatum på betalnngsfilen / Payment file has no payment date.")
        fy_id = self.get_financial_year_id(payment_date)
        if not fy_id:
            raise Exception(f"No financial year found in Fortnox for date {payment_date}.")

        # Re-query invoices fresh to avoid stale SQLAlchemy data
        invoices = db.session.query(SupplierInvoice).filter(
            SupplierInvoice.id.in_(invoice_ids)
        ).all()

        rows = []
        total = 0.0
        descriptions = []
        for inv in invoices:
            amount = round(float(inv.amount_incl_vat or 0), 2)
            if amount <= 0:
                continue
            rows.append({
                "Account": 2440,
                "Debit": amount,
                "Credit": 0,
                "TransactionInformation": f"{inv.supplier_name or ''} {inv.payment_ref or ''}".strip()[:200],
            })
            total += amount
            descriptions.append(inv.supplier_name or inv.payment_ref or "")

        if not rows:
            raise Exception("Inga fakturor med belopp att bokföra / No invoices with amount to post.")

        rows.append({
            "Account": 1930,
            "Debit": 0,
            "Credit": round(total, 2),
            "TransactionInformation": "Betalning leverantörsfakturor",
        })

        description = f"Bet. {', '.join(descriptions[:3])}{'...' if len(descriptions) > 3 else ''}"
        voucher_data = {
            "Description": description[:200],
            "TransactionDate": payment_date.isoformat(),
            "VoucherSeries": "A",
            "VoucherRows": rows,
        }
        result = self._request("POST", "/vouchers", json={"Voucher": voucher_data},
                               params={"financialyear": fy_id})
        voucher = result.get("Voucher", {})
        voucher_nr = voucher.get("VoucherNumber")
        voucher_series = voucher.get("VoucherSeries", "A")
        voucher_ref = f"{payment_date.year}-{voucher_series}{voucher_nr}" if voucher_nr else None
        logger.info("Fortnox payment voucher created — ref=%s total=%.2f", voucher_ref, total)
        return voucher_ref

    def create_supplier_voucher(self, voucher_rows, description, voucher_date, series="L", pdf_path=None):
        """Create a supplier voucher (bokföringsorder) in Fortnox."""
        fy_id = self.get_financial_year_id(voucher_date)
        if not fy_id:
            raise Exception(f"No financial year found in Fortnox for date {voucher_date}.")
        rows = []
        for r in voucher_rows:
            row = {
                "Account": int(r["Account"]),
                "Debit": round(float(r.get("Debit", 0)), 2),
                "Credit": round(float(r.get("Credit", 0)), 2),
            }
            rows.append(row)

        voucher_data = {
            "Description": description[:200],
            "TransactionDate": voucher_date.isoformat(),
            "VoucherSeries": series,
            "VoucherRows": rows,
        }

        result = self._request("POST", "/vouchers", json={"Voucher": voucher_data},
                               params={"financialyear": fy_id})
        voucher = result.get("Voucher", {})
        voucher_nr = voucher.get("VoucherNumber")
        voucher_series = voucher.get("VoucherSeries", series)
        voucher_ref = f"{voucher_date.year}-{voucher_series}{voucher_nr}" if voucher_nr else None
        logger.info("Fortnox supplier voucher created — ref=%s", voucher_ref)

        # TODO: attach pdf_path via Fortnox Archive API (same limitation as outgoing voucher)

        return result

    def upload_to_archive(self, file_path, filename):
        """Upload a file to the Fortnox archive. Returns the File Id (used for voucherfileconnections)."""
        ext = filename.rsplit(".", 1)[-1].lower()
        mime = "application/pdf" if ext == "pdf" else f"image/{ext}"
        with open(file_path, "rb") as f:
            file_bytes = f.read()
        logger.info("Fortnox archive — uploading %s (%d bytes)", filename, len(file_bytes))
        resp = requests.post(
            f"{self.BASE_URL}/archive",
            headers={"Authorization": f"Bearer {self._get_token()}"},
            files={"file": (filename, file_bytes, mime)},
        )
        logger.info("Fortnox archive upload — status=%s body=%s", resp.status_code, resp.text)
        if resp.status_code == 201:
            file_obj = resp.json().get("File", {})
            logger.info("Fortnox archive — file URL: %s", file_obj.get("@url", ""))
            return file_obj.get("Id")
        return None

    def connect_file_to_voucher(self, file_id, voucher_series, voucher_nr):
        """Link an archive file to a voucher. Tries /voucherfileconnections then /fileconnections."""
        token = self._get_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

        # Attempt 1: /voucherfileconnections
        payload1 = {
            "VoucherFileConnection": {
                "FileId": file_id,
                "VoucherNumber": str(voucher_nr),
                "VoucherSeries": voucher_series,
            }
        }
        logger.info("Fortnox voucherfileconnections — FileId=%s voucher=%s%s",
                    file_id, voucher_series, voucher_nr)
        resp1 = requests.post(f"{self.BASE_URL}/voucherfileconnections",
                              headers=headers, json=payload1)
        logger.info("Fortnox voucherfileconnections — status=%s body=%s",
                    resp1.status_code, resp1.text)
        if resp1.status_code in (200, 201):
            return True

        # Attempt 2: /fileconnections
        payload2 = {
            "FileConnection": {
                "FileId": file_id,
                "ObjectId": f"{voucher_series}{voucher_nr}",
                "ObjectType": "Voucher",
            }
        }
        logger.info("Fortnox fileconnections — FileId=%s ObjectId=%s%s",
                    file_id, voucher_series, voucher_nr)
        resp2 = requests.post(f"{self.BASE_URL}/fileconnections",
                              headers=headers, json=payload2)
        logger.info("Fortnox fileconnections — status=%s body=%s",
                    resp2.status_code, resp2.text)
        return resp2.status_code in (200, 201)

