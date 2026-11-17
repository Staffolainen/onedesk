import io
import os
import calendar
import re
import ssl
import json
import logging
import smtplib
import secrets
import requests
from logging.handlers import TimedRotatingFileHandler

# ── File logging (weekly rotation, 4-week retention) ─────────────────────────
def _setup_file_logging():
    log_dir = os.path.join(os.path.dirname(__file__), "logs")
    os.makedirs(log_dir, exist_ok=True)
    log_file = os.path.join(log_dir, "onedesk.log")
    handler = TimedRotatingFileHandler(
        log_file, when="W0", interval=1, backupCount=4, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter(
        "%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    handler.setLevel(logging.INFO)
    logging.getLogger().addHandler(handler)

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
                    datefmt="%Y-%m-%d %H:%M:%S")
_setup_file_logging()
from datetime import datetime, date, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
from functools import wraps
from collections import OrderedDict

from flask import (Flask, render_template, request, redirect, url_for,
                   flash, session, jsonify, send_file, abort, g)
from flask_login import LoginManager, login_user, logout_user, login_required, current_user
from flask_wtf.csrf import CSRFProtect
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename

from config import Config
from models import (db, User, Client, Project, PurchaseOrder, POHourType,
                    TimeEntry, Expense, Invoice, MileageEntry, Settings,
                    VoucherTemplate, SupplierInvoice, PaymentFile,
                    InternalCode, DEFAULT_KM_RATE,
                    AgiFieldCode, SalaryType, Employee, Payslip, PayslipLine, AgiReport, TaxTableRow,
                    fiscal_year, fy_start, fy_end)
from bookkeeping import (REVERSE_CHARGE_VAT_RATE, VAT_DOMESTIC, VAT_TREATMENTS,
                         VAT_REVERSE_CHARGE_DOMESTIC, VAT_REVERSE_CHARGE_EU_SERVICES,
                         is_reverse_charge, supplier_invoice_rows, vat_treatment_of)
from sqlalchemy.orm import selectinload, joinedload
from fortnox import FortnoxClient
from receipt_ocr import extract_receipt_data, extract_supplier_invoice_data, _normalize_bankgiro
from pdf_generator import generate_invoice_pdf, render_invoice_html
from payment_file_generator import (generate_pain001, get_execution_date, Transfer,
                                    _swedish_bank_holidays as swedish_bank_holidays)
from payroll import (KINDS, AGI_FIELD_SEED, AGI_AG_BASE_CODES, SALARY_TYPE_SEED, SKV_BANKGIRO,
                     ROUNDING_TOLERANCE, DEFAULT_ACCOUNTS as DEFAULT_PAYROLL_ACCOUNTS,
                     agi_field_selectable, payslip_summary, payslip_agi_values, net_pay,
                     salary_voucher_rows, salary_voucher_description, voucher_balanced,
                     agi_totals, build_agi_xml, tax_payment_rows, tax_settlement_rows, mileage_over_limit,
                     SKV_MILEAGE_RATE_PER_KM, line_amount, add_month,
                     next_period, normalize_personnummer, skv_due_date, digits as payroll_digits,
                     TAX_COLUMNS, fetch_skv_tax_table, tax_base, tax_from_row)
from payroll_pdf import generate_payslip_pdf

app = Flask(__name__)
app.config.from_object(Config)

db.init_app(app)
csrf = CSRFProtect(app)
limiter = Limiter(get_remote_address, app=app, default_limits=[], storage_uri="memory://")

login_manager = LoginManager(app)
login_manager.login_view = "auth_login"
login_manager.login_message = ""

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

import logging as _logging
_audit_logger = _logging.getLogger("onedesk.audit")
_audit_logger.setLevel(_logging.INFO)
_instance_dir = os.path.join(os.path.dirname(__file__), "instance")
os.makedirs(_instance_dir, exist_ok=True)
_audit_handler = _logging.FileHandler(
    os.path.join(_instance_dir, "audit.log"),
    encoding="utf-8"
)
_audit_handler.setFormatter(_logging.Formatter("%(asctime)s\t%(message)s"))
_audit_logger.addHandler(_audit_handler)

def _audit(action: str, detail: str = ""):
    if current_user.is_authenticated:
        user = (getattr(current_user, "display_name", None)
                or getattr(current_user, "email", None)
                or getattr(current_user, "username", None)
                or "—")
    else:
        user = "anon"
    _audit_logger.info(f"{user}\t{action}\t{detail}")

def admin_required(f):
    """Decorator: requires role=='admin'."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not current_user.is_authenticated or current_user.role != 'admin':
            abort(403)
        return f(*args, **kwargs)
    return decorated

# ── Language ──────────────────────────────────────────────────────────────────

TRANSLATIONS = {
    "sv": {
        "dashboard": "Start", "time": "Tidrapportering", "expenses": "Utlägg", "mileage": "Reseersättning",
        "invoices": "Fakturor", "clients": "Kunder", "logout": "Logga ut",
        "save": "Spara", "cancel": "Avbryt", "delete": "Ta bort", "edit": "Redigera",
        "approve": "Godkänn", "send": "Skicka", "hours": "Timmar", "date": "Datum",
        "description": "Beskrivning", "client": "Kund", "project": "Uppdrag",
        "amount": "Belopp", "vat": "Moms", "total": "Totalt", "status": "Status",
        "invoice": "Faktura", "draft": "Utkast", "approved": "Godkänd",
        "sent": "Skickad", "paid": "Betald", "pending": "Väntande",
        "billable": "Debiterbart", "internal": "Internt", "new": "Ny",
        "back": "Tillbaka", "settings": "Inställningar", "currency": "SEK",
        # Supplier invoices
        "sup_title": "Leverantörsfakturor",
        "sup_upload": "Ladda upp faktura",
        "sup_upload_desc": "Ladda upp en PDF eller bild av fakturan. Claude Vision extraherar leverantörsuppgifter, belopp och betalningsinformation automatiskt.",
        "sup_upload_label": "Fakturafil (PDF eller bild)",
        "sup_upload_btn": "Ladda upp och analysera",
        "sup_review_title": "Granska och bekräfta",
        "sup_supplier": "Leverantör",
        "sup_supplier_name": "Leverantörsnamn",
        "sup_invoice_number": "Fakturanummer",
        "sup_currency": "Valuta",
        "sup_dates": "Datum",
        "sup_invoice_date": "Fakturadatum",
        "sup_due_date": "Förfallodatum",
        "sup_amounts": "Fakturadetaljer",
        "sup_excl_vat": "Exkl. moms",
        "sup_vat": "Moms (SEK)",
        "sup_incl_vat": "Att betala (inkl. moms)",
        "sup_payment": "Betalning",
        "sup_ocr": "OCR eller fakturanummer",
        "sup_bookkeeping": "Bokföring",
        "sup_category": "Motkonto bokföring",
        "sup_credit_hint": "Kredit: 2440 Leverantörsskulder",
        "sup_assignment": "Uppdrag (valfritt)",
        "sup_file": "Uppladdad fil",
        "sup_view_file": "Visa fil",
        "sup_save_book": "Spara och bokför",
        "sup_discard": "Kasta faktura",
        "sup_discard_confirm": "Ta bort fakturan?",
        "sup_pending": "Väntar på granskning",
        "sup_backlog": "Kommande, ej utförda betalningar",
        "sup_payment_files": "Betalningsfiler",
        "sup_recently_paid": "Senast bokförda betalningar",
        "sup_review_btn": "Granska",
        "sup_mark_paid": "Markera bokförd manuellt",
        "sup_paid_btn": "Bokförd",
        "sup_overdue": "försenad",
        "sup_payment_run": "Betalkörning",
        "sup_select_invoices": "Välj fakturor att betala",
        "sup_payment_date": "Betalningsdatum",
        "sup_generate_btn": "Generera betalningsfil (pain.001)",
        "sup_generate_hint": "Filen laddas ned direkt. Ladda sedan upp i Handelsbankens internetbank.",
        "sup_no_backlog": "Inga fakturor i betalningsbackloggen.",
        "sup_pending_orders": "Väntande betalordrar",
        "sup_confirm_btn": "Bekräfta betalorder registrerad",
        "sup_confirm_dialog": "Bekräfta att betalfilen är uppladdad till banken? Detta skapar betalningsverifikat i Fortnox (1930 → 2440).",
        "sup_ocr_ok": "Claude Vision extraherade data från fakturan",
        "sup_ocr_fail": "OCR misslyckades – fyll i manuellt",
        "sup_no_invoices": "Inga fakturor att visa.",
    },
    "en": {
        "dashboard": "Start", "time": "Time Tracking", "expenses": "Expenses", "mileage": "Mileage",
        "invoices": "Invoices", "clients": "Clients", "logout": "Log out",
        "save": "Save", "cancel": "Cancel", "delete": "Delete", "edit": "Edit",
        "approve": "Approve", "send": "Send", "hours": "Hours", "date": "Date",
        "description": "Description", "client": "Client", "project": "Project",
        "amount": "Amount", "vat": "VAT", "total": "Total", "status": "Status",
        "invoice": "Invoice", "draft": "Draft", "approved": "Approved",
        "sent": "Sent", "paid": "Paid", "pending": "Pending",
        "billable": "Billable", "internal": "Internal", "new": "New",
        "back": "Back", "settings": "Settings", "currency": "SEK",
        # Supplier invoices
        "sup_title": "Supplier Invoices",
        "sup_upload": "Upload Invoice",
        "sup_upload_desc": "Upload a PDF or image of the invoice. Claude Vision will automatically extract supplier details, amounts, and payment information.",
        "sup_upload_label": "Invoice file (PDF or image)",
        "sup_upload_btn": "Upload & analyse",
        "sup_review_title": "Review & confirm",
        "sup_supplier": "Supplier",
        "sup_supplier_name": "Supplier name",
        "sup_invoice_number": "Invoice number",
        "sup_currency": "Currency",
        "sup_dates": "Dates",
        "sup_invoice_date": "Invoice date",
        "sup_due_date": "Due date",
        "sup_amounts": "Invoice details",
        "sup_excl_vat": "Excl. VAT",
        "sup_vat": "VAT (SEK)",
        "sup_incl_vat": "Amount to pay (incl. VAT)",
        "sup_payment": "Payment",
        "sup_ocr": "OCR or invoice number",
        "sup_bookkeeping": "Bookkeeping",
        "sup_category": "Contra account",
        "sup_credit_hint": "Credit: 2440 Accounts payable",
        "sup_assignment": "Assignment (optional)",
        "sup_file": "Uploaded file",
        "sup_view_file": "View file",
        "sup_save_book": "Save & book",
        "sup_discard": "Discard invoice",
        "sup_discard_confirm": "Delete this invoice?",
        "sup_pending": "Pending review",
        "sup_backlog": "Upcoming, unpaid invoices",
        "sup_payment_files": "Payment files",
        "sup_recently_paid": "Recently booked payments",
        "sup_review_btn": "Review",
        "sup_mark_paid": "Mark as booked manually",
        "sup_paid_btn": "Booked",
        "sup_overdue": "overdue",
        "sup_payment_run": "Payment run",
        "sup_select_invoices": "Select invoices to pay",
        "sup_payment_date": "Payment date",
        "sup_generate_btn": "Generate payment file (pain.001)",
        "sup_generate_hint": "File downloads immediately. Upload to Handelsbanken internet bank.",
        "sup_no_backlog": "No invoices in payment backlog.",
        "sup_pending_orders": "Pending payment orders",
        "sup_confirm_btn": "Confirm payment order registered",
        "sup_confirm_dialog": "Confirm that the payment file has been uploaded to the bank? This will create a payment voucher in Fortnox (1930 → 2440).",
        "sup_ocr_ok": "Claude Vision extracted data from the invoice",
        "sup_ocr_fail": "OCR failed – please fill in manually",
        "sup_no_invoices": "No invoices to show.",
    }
}

_AD_SKIP_ENDPOINTS = frozenset({
    'auth_login', 'auth_logout', 'auth_not_provisioned', 'static', 'set_lang',
    'healthz',
})

@app.before_request
def set_language():
    g.lang = session.get("lang", "sv")
    g.t = TRANSLATIONS.get(g.lang, TRANSLATIONS["sv"])
    g.config = app.config
    g.build_time = os.getenv("BUILD_TIME", "dev")

@app.before_request
def ad_auto_login():
    """Auto-login from Azure AD Easy Auth headers (X-MS-CLIENT-PRINCIPAL)."""
    if current_user.is_authenticated:
        return
    if request.endpoint in _AD_SKIP_ENDPOINTS:
        return

    principal_header = request.headers.get('X-MS-CLIENT-PRINCIPAL')
    if not principal_header:
        return

    try:
        import base64 as _b64
        principal = json.loads(_b64.b64decode(principal_header).decode('utf-8'))
        claims = {c['typ']: c['val'] for c in principal.get('claims', [])}

        ad_oid = (
            claims.get('http://schemas.microsoft.com/identity/claims/objectidentifier')
            or claims.get('oid')
        )
        email = (
            claims.get('http://schemas.xmlsoap.org/ws/2005/05/identity/claims/emailaddress')
            or claims.get('preferred_username')
            or principal.get('userDetails', '')
        )
        if email:
            email = email.lower()
    except Exception as e:
        app.logger.warning("AD auto-login failed to parse principal: %s", e)
        return

    allowed_domains = [d.strip() for d in app.config.get("ALLOWED_EMAIL_DOMAINS", "").split(",") if d.strip()]
    if allowed_domains and email and email.split("@")[-1] not in allowed_domains:
        app.logger.warning("AD auto-login rejected domain: %s", email)
        abort(403)

    try:
        user = None
        if ad_oid:
            user = User.query.filter_by(ad_oid=ad_oid).first()
        if not user and email:
            user = User.query.filter_by(email=email).first()

        if user and user.active:
            if ad_oid and not user.ad_oid:
                user.ad_oid = ad_oid
            if email and not user.email:
                user.email = email
            user.last_login_at = datetime.utcnow()
            db.session.commit()
            login_user(user, remember=True)
        elif user and not user.active:
            flash("Ditt konto är inaktiverat / Your account is disabled", "error")
            return redirect(url_for('auth_login'))
        else:
            # AD user not provisioned in onedesk
            return redirect(url_for('auth_not_provisioned'))
    except Exception as e:
        app.logger.warning("AD auto-login failed: %s", e)

@app.route("/healthz")
def healthz():
    """Unauthenticated liveness/readiness probe for smoke tests and load balancers.

    Reports the build currently running (BUILD_TIME) so a deploy can be verified
    against the expected image, and pings the DB to confirm the app can serve requests.
    """
    build_time = os.getenv("BUILD_TIME", "dev")
    try:
        from sqlalchemy import text
        db.session.execute(text("SELECT 1"))
        return jsonify(status="ok", build_time=build_time), 200
    except Exception as e:
        app.logger.warning("healthz DB check failed: %s", e)
        return jsonify(status="error", build_time=build_time, detail="db"), 503


@app.route("/lang/<lang>")
def set_lang(lang):
    if lang in ("sv", "en"):
        session["lang"] = lang
    return redirect(request.referrer or url_for("dashboard"))

@app.after_request
def set_security_headers(response):
    response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; "
        "font-src 'self'; "
        "frame-ancestors 'none';"
    )
    return response

def fmt_amount(amount):
    """Format number as Swedish currency string"""
    return f"{amount:,.0f}".replace(",", " ")

app.jinja_env.filters["fmt_amount"] = fmt_amount

def _safe_float(value, default=0.0):
    try:
        return float(value) if value and str(value).strip() else default
    except (ValueError, TypeError):
        return default

def _safe_int(value, default=0):
    try:
        return int(value) if value and str(value).strip() else default
    except (ValueError, TypeError):
        return default

def _safe_date(value):
    try:
        return datetime.strptime(value, "%Y-%m-%d").date() if value and value.strip() else None
    except (ValueError, TypeError):
        return None


# ── Assignment / internal-code target ─────────────────────────────────────────
# Time, expenses, mileage and supplier invoices each hang off *either* a client
# assignment or an internal code, never both. The forms post one combined select
# named "target", whose value is "p:<project id>" or "i:<internal code id>".

def _parse_target(form=None):
    """Read the combined target select and return (project_id, internal_code_id).

    At most one is non-None. Falls back to a bare ``project_id`` field so links and
    any form still posting the old field keep working.
    """
    form = request.form if form is None else form
    raw = (form.get("target") or "").strip()
    if raw.startswith("i:"):
        return None, (_safe_int(raw[2:]) or None)
    if raw.startswith("p:"):
        return (_safe_int(raw[2:]) or None), None
    if raw:
        # A plain integer: legacy callers meant a project.
        return (_safe_int(raw) or None), None
    return (_safe_int(form.get("project_id")) or None), None


def _target_options(include_inactive_project=None, include_inactive_code=None, for_cost=False):
    """Assignments and internal codes for the combined select, in display order.

    ``for_cost`` restricts internal codes to those that accept costs; the time grid
    passes False and filters on ``track_time`` instead. An inactive row that an
    existing record already points at is included so editing never silently drops it.
    """
    projects = (Project.query
        .join(Client)
        .filter(Project.active == True)
        .order_by(Client.name, Project.name)
        .all())
    if include_inactive_project and not any(p.id == include_inactive_project for p in projects):
        extra = Project.query.get(include_inactive_project)
        if extra:
            projects.append(extra)

    q = InternalCode.query.filter_by(active=True)
    if for_cost:
        q = q.filter_by(track_cost=True)
    else:
        q = q.filter_by(track_time=True)
    codes = q.order_by(InternalCode.sort_order, InternalCode.code).all()
    if include_inactive_code and not any(c.id == include_inactive_code for c in codes):
        extra = InternalCode.query.get(include_inactive_code)
        if extra:
            codes.append(extra)
    return projects, codes


_PAYMENT_PATTERNS = {
    "bg":   re.compile(r"^\d{3,4}-\d{4}$"),
    "pg":   re.compile(r"^\d{1,7}-\d$"),
    "iban": re.compile(r"^[A-Z]{2}\d{2}[A-Z0-9]{4,30}$"),
}

def _validate_payment_account(account_type, account):
    pat = _PAYMENT_PATTERNS.get(account_type)
    return pat and bool(pat.match(account.upper().replace(" ", "")))

def _ocr_payment_account(ocr):
    """Map OCR bankgiro/plusgiro/iban keys to the unified model fields."""
    for key, typ in [("bankgiro", "bg"), ("plusgiro", "pg"), ("iban", "iban")]:
        val = (ocr.get(key) or "").strip()
        if val:
            return {"payment_account": val, "payment_account_type": typ}
    return {"payment_account": None, "payment_account_type": None}

# ── Auth ──────────────────────────────────────────────────────────────────────

@app.route("/login", methods=["GET", "POST"])
@limiter.limit("10 per minute; 30 per hour")
def auth_login():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))
    if request.method == "POST":
        password = request.form.get("password", "")
        remember = request.form.get("remember") == "1"
        user = User.query.first()
        if user and check_password_hash(user.password_hash, password):
            login_user(user, remember=remember)
            return redirect(url_for("dashboard"))
        flash("Fel lösenord / Wrong password", "error")
    return render_template("auth/login.html")

@app.route("/logout")
@login_required
def auth_logout():
    logout_user()
    return redirect(url_for("auth_login"))

@app.route("/not-provisioned")
def auth_not_provisioned():
    """Shown when an Azure AD user exists but has no onedesk account yet."""
    return render_template("auth/not_provisioned.html"), 403

@app.route("/supplier-uploads/<path:filename>")
@login_required
def supplier_invoice_file(filename):
    """Serve supplier invoice files only to authenticated users."""
    folder = _supplier_upload_folder()
    safe_path = os.path.realpath(os.path.join(folder, filename))
    if not safe_path.startswith(os.path.realpath(folder) + os.sep):
        abort(404)
    return send_file(safe_path)

@app.route("/uploads/<path:filename>")
@login_required
def uploaded_file(filename):
    """Serve upload files only to authenticated users."""
    upload_folder = app.config["UPLOAD_FOLDER"]
    # Prevent path traversal
    safe_path = os.path.realpath(os.path.join(upload_folder, filename))
    if not safe_path.startswith(os.path.realpath(upload_folder) + os.sep):
        abort(404)
    return send_file(safe_path)

# ── Dashboard ─────────────────────────────────────────────────────────────────

@app.route("/")
@login_required
def dashboard():
    today = date.today()
    month_start = today.replace(day=1)

    # This month's hours per client, plus internal hours broken out by code
    monthly_entries = (TimeEntry.query
        .join(Project).join(Client)
        .filter(TimeEntry.entry_date >= month_start)
        .all())

    hours_by_client = {}
    for e in monthly_entries:
        cname = e.project.client.name
        hours_by_client[cname] = hours_by_client.get(cname, 0) + e.hours

    monthly_internal = (TimeEntry.query
        .join(InternalCode)
        .filter(TimeEntry.entry_date >= month_start)
        .options(selectinload(TimeEntry.internal_code))
        .all())
    hours_by_internal = {}
    for e in monthly_internal:
        hours_by_internal[e.internal_code.label] = \
            hours_by_internal.get(e.internal_code.label, 0) + e.hours

    # Pending expenses
    pending_expenses = Expense.query.filter_by(status="pending").count()

    # Open invoices
    open_invoices = Invoice.query.filter(Invoice.status.in_(["draft","approved"])).all()

    # Recent time entries
    recent_entries = (TimeEntry.query
        .order_by(TimeEntry.entry_date.desc())
        .limit(5).all())

    # Assignment budget overview — eager-load all nested relationships to avoid N+1
    active_projects = (Project.query
        .join(Client)
        .filter(Project.active == True, Client.active == True)
        .options(
            selectinload(Project.purchase_orders).selectinload(PurchaseOrder.hour_types),
            selectinload(Project.time_entries).selectinload(TimeEntry.hour_type),
            selectinload(Project.expenses),
            selectinload(Project.mileage_entries),
        )
        .order_by(Client.name, Project.name)
        .all())

    project_budgets = []
    for p in active_projects:
        current_po = p.get_active_po(today)
        current_po_id = current_po.id if current_po else None

        hist_cost        = float(p.accumulated_cost or 0)
        current_invoiced = 0.0
        current_uninvoiced = 0.0

        po_from = current_po.valid_from or date.min if current_po else None
        po_to   = current_po.valid_to   or date.max if current_po else None

        def _in_current_po(d):
            return current_po is not None and po_from <= d <= po_to

        for e in p.time_entries:
            entry_po_id = e.hour_type.po_id if e.hour_type else None
            cost = e.hours * e.effective_rate
            if current_po_id and entry_po_id == current_po_id:
                if e.invoice_id:
                    current_invoiced += cost
                else:
                    current_uninvoiced += cost
            else:
                hist_cost += cost

        for exp in p.expenses:
            if exp.status == "pending":
                continue  # not yet approved, exclude
            cost = exp.amount_excl_vat
            if _in_current_po(exp.expense_date):
                if exp.invoice_id:
                    current_invoiced += cost
                else:
                    current_uninvoiced += cost
            else:
                hist_cost += cost

        for m in p.mileage_entries:
            cost = m.amount
            if _in_current_po(m.entry_date):
                if m.invoice_id:
                    current_invoiced += cost
                else:
                    current_uninvoiced += cost
            else:
                hist_cost += cost

        po_budget = float(current_po.po_amount) if current_po and current_po.po_amount else None
        headroom  = max(0.0, po_budget - current_invoiced - current_uninvoiced) if po_budget is not None else None
        total_bar = hist_cost + (po_budget if po_budget is not None else current_invoiced + current_uninvoiced)

        # Skip projects with no data at all
        if total_bar == 0 and not current_po:
            continue

        project_budgets.append({
            "project":            p,
            "hist_cost":          round(hist_cost, 0),
            "current_invoiced":   round(current_invoiced, 0),
            "current_uninvoiced": round(current_uninvoiced, 0),
            "headroom":           round(headroom, 0) if headroom is not None else None,
            "po_budget":          round(po_budget, 0) if po_budget is not None else None,
            "total_bar":          max(total_bar, 1),
            "current_po":         current_po,
        })

    return render_template("dashboard.html",
        hours_by_client=hours_by_client,
        hours_by_internal=hours_by_internal,
        pending_expenses=pending_expenses,
        open_invoices=open_invoices,
        recent_entries=recent_entries,
        project_budgets=project_budgets,
        today=today,
    )

# ── Clients ───────────────────────────────────────────────────────────────────

@app.route("/clients")
@login_required
def clients_list():
    clients = Client.query.filter_by(active=True).order_by(Client.name).all()
    return render_template("clients/index.html", clients=clients)

@app.route("/clients/new", methods=["GET", "POST"])
@login_required
def clients_new():
    if request.method == "POST":
        c = Client(
            name=request.form["name"],
            org_nr=request.form.get("org_nr"),
            contact_name=request.form.get("contact_name"),
            contact_email=request.form.get("contact_email"),
            address=request.form.get("address"),
            vat_nr=request.form.get("vat_nr"),
            hourly_rate=_safe_float(request.form.get("hourly_rate"), app.config["DEFAULT_HOURLY_RATE"]),
            km_rate=_safe_float(request.form.get("km_rate"), 25.0),
            payment_days=_safe_int(request.form.get("payment_days"), 30),
            invoice_language=request.form.get("invoice_language", "sv"),
        )
        db.session.add(c)
        db.session.commit()
        flash("Kund skapad / Client created", "success")
        return redirect(url_for("clients_list"))
    return render_template("clients/form.html", client=None)

@app.route("/clients/<int:client_id>/edit", methods=["GET", "POST"])
@login_required
def clients_edit(client_id):
    c = Client.query.get_or_404(client_id)
    if request.method == "POST":
        c.name = request.form["name"]
        c.org_nr = request.form.get("org_nr")
        c.contact_name = request.form.get("contact_name")
        c.contact_email = request.form.get("contact_email")
        c.address = request.form.get("address")
        c.vat_nr = request.form.get("vat_nr")
        c.hourly_rate = _safe_float(request.form.get("hourly_rate"), 1500)
        c.km_rate = _safe_float(request.form.get("km_rate"), 25.0)
        c.payment_days = _safe_int(request.form.get("payment_days"), 30)
        c.invoice_language = request.form.get("invoice_language", "sv")
        db.session.commit()
        flash("Kund uppdaterad / Client updated", "success")
        return redirect(url_for("clients_list"))
    return render_template("clients/form.html", client=c)

@app.route("/clients/<int:client_id>/projects/new", methods=["GET","POST"])
@login_required
def projects_new(client_id):
    c = Client.query.get_or_404(client_id)
    if request.method == "POST":
        p = Project(
            client_id=client_id,
            project_number=request.form.get("project_number", "").strip() or Project.generate_number(),
            name=request.form["name"],
            description=request.form.get("description"),
            start_date=_safe_date(request.form.get("start_date")),
            end_date=_safe_date(request.form.get("end_date")),
            accumulated_cost=_safe_float(request.form.get("accumulated_cost"), 0),
            invoice_cc_email=request.form.get("invoice_cc_email", "").strip() or None,
            contact_person_name=request.form.get("contact_person_name", "").strip() or None,
            expense_markup_pct=_safe_float(request.form.get("expense_markup_pct"), 10.0),
        )
        db.session.add(p)
        db.session.commit()
        flash("Uppdrag skapat / Assignment created", "success")
        return redirect(url_for("clients_list"))
    next_number = Project.generate_number()
    return render_template("clients/project_form.html", client=c, project=None, next_number=next_number)

@app.route("/projects/<int:project_id>/edit", methods=["GET", "POST"])
@login_required
def projects_edit(project_id):
    p = Project.query.get_or_404(project_id)
    if request.method == "POST":
        p.project_number   = request.form.get("project_number", "").strip() or p.project_number
        p.name             = request.form["name"]
        p.description      = request.form.get("description")
        p.start_date       = _safe_date(request.form.get("start_date"))
        p.end_date         = _safe_date(request.form.get("end_date"))
        p.active           = request.form.get("active") == "1"
        p.accumulated_cost = _safe_float(request.form.get("accumulated_cost"), 0)
        p.invoice_cc_email = request.form.get("invoice_cc_email", "").strip() or None
        p.contact_person_name = request.form.get("contact_person_name", "").strip() or None
        p.expense_markup_pct = _safe_float(request.form.get("expense_markup_pct"), 10.0)
        db.session.commit()
        flash("Uppdrag uppdaterat / Assignment updated", "success")
        return redirect(url_for("clients_list"))
    next_number = None if p.project_number else Project.generate_number()
    return render_template("clients/project_form.html", client=p.client, project=p, next_number=next_number)

def _po_overlaps(project_id, valid_from, valid_to, exclude_po_id=None):
    """Return the first existing PO on the project whose validity window
    overlaps [valid_from, valid_to]. None dates mean open-ended."""
    existing = PurchaseOrder.query.filter_by(project_id=project_id, active=True).all()
    for po in existing:
        if exclude_po_id and po.id == exclude_po_id:
            continue
        # Overlap: new_start <= existing_end AND new_end >= existing_start
        # Treat None as -inf / +inf
        new_start = valid_from or date.min
        new_end   = valid_to   or date.max
        ex_start  = po.valid_from or date.min
        ex_end    = po.valid_to   or date.max
        if new_start <= ex_end and new_end >= ex_start:
            return po
    return None

@app.route("/projects/<int:project_id>/purchase-orders/new", methods=["GET", "POST"])
@login_required
def po_new(project_id):
    p = Project.query.get_or_404(project_id)
    if request.method == "POST":
        valid_from = _safe_date(request.form.get("valid_from"))
        valid_to   = _safe_date(request.form.get("valid_to"))
        overlap = _po_overlaps(project_id, valid_from, valid_to)
        if overlap:
            flash(f"Datumkonflik med beställning {overlap.po_number or overlap.id} – perioder får ej överlappa / "
                  f"Date conflict with order {overlap.po_number or overlap.id} – periods must not overlap", "error")
            return render_template("clients/po_form.html", project=p, po=None)
        km_rate_str = request.form.get("km_rate", "").strip()
        po = PurchaseOrder(
            project_id=project_id,
            po_number=request.form.get("po_number") or None,
            description=request.form.get("description") or None,
            hourly_rate=_safe_float(request.form.get("hourly_rate"), app.config["DEFAULT_HOURLY_RATE"]),
            km_rate=_safe_float(km_rate_str) if km_rate_str else None,
            valid_from=valid_from,
            valid_to=valid_to,
        )
        db.session.add(po)
        db.session.flush()
        db.session.add(POHourType(po_id=po.id, name="Normal", billable=True, sort_order=0, hourly_rate=po.hourly_rate))
        db.session.add(POHourType(po_id=po.id, name="Restid", billable=True, sort_order=1, hourly_rate=po.hourly_rate))
        db.session.commit()
        flash("Beställning skapad / Order created", "success")
        return redirect(url_for("clients_list"))
    return render_template("clients/po_form.html", project=p, po=None)

@app.route("/purchase-orders/<int:po_id>/edit", methods=["GET", "POST"])
@login_required
def po_edit(po_id):
    po = PurchaseOrder.query.get_or_404(po_id)
    project = po.project
    if request.method == "POST":
        valid_from = _safe_date(request.form.get("valid_from"))
        valid_to   = _safe_date(request.form.get("valid_to"))
        overlap = _po_overlaps(po.project_id, valid_from, valid_to, exclude_po_id=po.id)
        if overlap:
            flash(f"Datumkonflik med beställning {overlap.po_number or overlap.id} – perioder får ej överlappa / "
                  f"Date conflict with order {overlap.po_number or overlap.id} – periods must not overlap", "error")
            return redirect(url_for("po_edit", po_id=po.id))
        po.po_number = request.form.get("po_number") or None
        po.description = request.form.get("description") or None
        po.hourly_rate = _safe_float(request.form.get("hourly_rate"), po.hourly_rate)
        po_amount_str = request.form.get("po_amount", "").strip()
        po.po_amount = _safe_float(po_amount_str) if po_amount_str else None
        km_rate_str = request.form.get("km_rate", "").strip()
        po.km_rate = _safe_float(km_rate_str) if km_rate_str else None
        po.valid_from = valid_from
        po.valid_to   = valid_to
        db.session.commit()
        flash("Beställning uppdaterad / Order updated", "success")
        return redirect(url_for("clients_list"))
    # Consumption stats per hour type
    consumption = []
    total_hours = 0.0
    total_amount = 0.0
    for ht in po.hour_types:
        if not ht.active:
            continue
        h = sum(e.hours for e in ht.time_entries)
        rate = ht.hourly_rate if ht.hourly_rate is not None else po.hourly_rate
        amt = h * rate
        consumption.append({"hour_type": ht, "hours": h, "rate": rate, "amount": amt})
        total_hours += h
        total_amount += amt
    return render_template("clients/po_form.html", project=project, po=po,
                           consumption=consumption,
                           total_hours=total_hours,
                           total_amount=total_amount)

@app.route("/purchase-orders/<int:po_id>/delete", methods=["POST"])
@login_required
def po_delete(po_id):
    po = PurchaseOrder.query.get_or_404(po_id)
    db.session.delete(po)
    db.session.commit()
    flash("Beställning borttagen / Order deleted", "success")
    return redirect(url_for("clients_list"))

@app.route("/purchase-orders/<int:po_id>/hour-types/add", methods=["POST"])
@login_required
def hour_type_add(po_id):
    po = PurchaseOrder.query.get_or_404(po_id)
    name = request.form.get("name", "").strip()
    if name:
        max_order = max((ht.sort_order for ht in po.hour_types), default=-1)
        billable = "billable" in request.form
        rate_str = request.form.get("hourly_rate", "").strip()
        hourly_rate = float(rate_str) if rate_str else None
        db.session.add(POHourType(
            po_id=po_id, name=name, billable=billable,
            sort_order=max_order + 1, hourly_rate=hourly_rate,
        ))
        db.session.commit()
        flash(f"Tidtyp '{name}' tillagd / Hour type '{name}' added", "success")
    return redirect(url_for("po_edit", po_id=po_id))

@app.route("/hour-types/<int:ht_id>/update", methods=["POST"])
@login_required
def hour_type_update(ht_id):
    ht = POHourType.query.get_or_404(ht_id)
    ht.name = request.form.get("name", "").strip() or ht.name
    rate_str = request.form.get("hourly_rate", "").strip()
    ht.hourly_rate = float(rate_str) if rate_str else None
    ht.billable = "billable" in request.form
    db.session.commit()
    flash("Tidtyp uppdaterad / Hour type updated", "success")
    return redirect(url_for("po_edit", po_id=ht.po_id))

@app.route("/hour-types/<int:ht_id>/delete", methods=["POST"])
@login_required
def hour_type_delete(ht_id):
    ht = POHourType.query.get_or_404(ht_id)
    po_id = ht.po_id
    if ht.time_entries:
        flash("Kan inte ta bort tidtyp med loggad tid / Cannot delete hour type with logged time", "error")
        return redirect(url_for("po_edit", po_id=po_id))
    db.session.delete(ht)
    db.session.commit()
    flash("Tidtyp borttagen / Hour type deleted", "success")
    return redirect(url_for("po_edit", po_id=po_id))

# ── Time Reporting ────────────────────────────────────────────────────────────

@app.route("/time")
@login_required
def time_index():
    today = date.today()
    current_week_start = today - timedelta(days=today.weekday())
    week_start_str = request.args.get("week")
    if week_start_str:
        week_start = datetime.strptime(week_start_str, "%Y-%m-%d").date()
    else:
        week_start = current_week_start
    week_end = week_start + timedelta(days=6)

    work_dates = [week_start + timedelta(days=i) for i in range(7)]

    entries = (TimeEntry.query
        .filter(TimeEntry.entry_date >= week_start, TimeEntry.entry_date <= week_end)
        .options(selectinload(TimeEntry.hour_type).selectinload(POHourType.po))
        .order_by(TimeEntry.entry_date)
        .all())

    # Build matrix rows ordered by first appearance. A row is one target — an
    # assignment or an internal code — plus its hour/time type.
    seen_keys = []
    matrix = {}
    for e in entries:
        key = (e.project_id, e.internal_code_id, e.hour_type_id, e.time_type)
        if key not in matrix:
            seen_keys.append(key)
            matrix[key] = {
                "target": e,                     # any entry of the row; carries the labels
                "target_key": e.target_key,
                "is_internal": e.is_internal,
                "hour_type_id": e.hour_type_id,
                "time_type": e.time_type or "Normal",
                "display_type": e.display_type,
                "days": {},
                "comment": "",
            }
        matrix[key]["days"][e.entry_date] = e
        if e.description and not matrix[key]["comment"]:
            matrix[key]["comment"] = e.description

    matrix_rows = [matrix[k] for k in seen_keys]
    total_hours = sum(e.hours for e in entries)
    internal_hours = sum(e.hours for e in entries if e.is_internal)
    projects, internal_codes = _target_options()

    # Build target → PO hour types map for the JS-driven add-row dropdown, keyed by
    # the same "p:<id>" / "i:<id>" values the select uses. Internal codes never have
    # PO hour types, so they map to an empty list.
    hour_types_map = {f"i:{c.id}": [] for c in internal_codes}
    for p in projects:
        active_po = None
        for po in p.purchase_orders:
            if po.active and \
               (po.valid_from is None or po.valid_from <= today) and \
               (po.valid_to is None or po.valid_to >= today):
                if active_po is None or (po.valid_from or date.min) > (active_po.valid_from or date.min):
                    active_po = po
        if active_po:
            hour_types_map[f"p:{p.id}"] = [
                {"key": f"ht_{ht.id}", "name": ht.name}
                for ht in active_po.hour_types if ht.active
            ]
        else:
            hour_types_map[f"p:{p.id}"] = []

    # Swedish public holidays for the year(s) covering the displayed week
    import holidays as _holidays
    import json
    se_holidays = _holidays.Sweden(years={week_start.year, week_end.year})
    bank_holidays = {d.isoformat(): name for d, name in se_holidays.items()}

    return render_template("time/index.html",
        week_start=week_start, week_end=week_end,
        work_dates=work_dates,
        matrix_rows=matrix_rows,
        total_hours=total_hours,
        internal_hours=internal_hours,
        projects=projects,
        internal_codes=internal_codes,
        today=today,
        current_week_start=current_week_start,
        prev_week=(week_start - timedelta(days=7)).isoformat(),
        next_week=(week_start + timedelta(days=7)).isoformat(),
        hour_types_map=hour_types_map,
        bank_holidays=bank_holidays,
    )

@app.route("/time/save-row", methods=["POST"])
@login_required
def time_save_row():
    week = request.form.get("week")
    project_id, internal_code_id = _parse_target()

    if not project_id and not internal_code_id:
        flash("Välj ett uppdrag eller en intern kod / Select an assignment or internal code", "error")
        return redirect(url_for("time_index", week=week))

    if internal_code_id:
        code = InternalCode.query.get(internal_code_id)
        if not code or not code.active or not code.track_time:
            flash("Ogiltig intern kod / Invalid internal code", "error")
            return redirect(url_for("time_index", week=week))

    # Resolve hour type — either a PO hour type or a free-text type string.
    # Existing rows pass hour_type_id (int) or time_type (str) directly.
    # New rows from the add-row form pass type_key like "ht_123".
    # Internal codes carry no PO, so they never get an hour_type_id.
    hour_type_id = None
    time_type = None

    hour_type_id_raw = request.form.get("hour_type_id", "").strip()
    time_type_raw = request.form.get("time_type", "").strip()
    type_key = request.form.get("type_key", "").strip()

    if hour_type_id_raw:
        hour_type_id = int(hour_type_id_raw)
    elif type_key.startswith("ht_"):
        hour_type_id = int(type_key[3:])
    elif time_type_raw:
        time_type = time_type_raw
    else:
        time_type = "Normal"

    if internal_code_id:
        hour_type_id = None   # PO hour types belong to client assignments only

    description = request.form.get("description", "").strip()

    for key, value in request.form.items():
        if not key.startswith("hours_"):
            continue
        date_str = key[6:]
        try:
            entry_date = datetime.strptime(date_str, "%Y-%m-%d").date()
            hours = float(value.strip()) if value.strip() else 0.0
        except (ValueError, AttributeError):
            continue

        q = TimeEntry.query.filter(
            TimeEntry.project_id == project_id,
            TimeEntry.internal_code_id == internal_code_id,
            TimeEntry.entry_date == entry_date,
        )
        if hour_type_id:
            existing = q.filter(TimeEntry.hour_type_id == hour_type_id).first()
        else:
            existing = q.filter(
                TimeEntry.hour_type_id == None,
                TimeEntry.time_type == time_type,
            ).first()

        if hours > 0:
            if existing and not existing.invoiced:
                existing.hours = hours
                existing.description = description
            elif not existing:
                db.session.add(TimeEntry(
                    project_id=project_id,
                    internal_code_id=internal_code_id,
                    entry_date=entry_date,
                    hours=hours,
                    hour_type_id=hour_type_id,
                    time_type=time_type,
                    description=description,
                ))
        elif existing and not existing.invoiced:
            db.session.delete(existing)

    # Also propagate description change to any existing entries for this row/week
    # that weren't touched by the hours loop (e.g. invoiced rows or unchanged cells)
    week_str = request.form.get("week")
    if week_str and description is not None:
        from datetime import timedelta
        ws = datetime.strptime(week_str, "%Y-%m-%d").date()
        we = ws + timedelta(days=6)
        wq = TimeEntry.query.filter(
            TimeEntry.project_id == project_id,
            TimeEntry.internal_code_id == internal_code_id,
            TimeEntry.entry_date >= ws,
            TimeEntry.entry_date <= we,
        )
        if hour_type_id:
            existing_entries = wq.filter(TimeEntry.hour_type_id == hour_type_id).all()
        else:
            existing_entries = wq.filter(
                TimeEntry.hour_type_id == None,
                TimeEntry.time_type == time_type,
            ).all()
        for e in existing_entries:
            e.description = description

    db.session.commit()
    return redirect(url_for("time_index", week=week))

@app.route("/time/<int:entry_id>/delete", methods=["POST"])
@login_required
def time_delete(entry_id):
    e = TimeEntry.query.get_or_404(entry_id)
    if e.invoiced:
        flash("Kan ej ta bort fakturerade poster / Cannot delete invoiced entries", "error")
        return redirect(url_for("time_index"))
    db.session.delete(e)
    db.session.commit()
    flash("Borttagen / Deleted", "success")
    return redirect(request.referrer or url_for("time_index"))

@app.route("/time/monthly")
@login_required
def time_monthly():
    today = date.today()
    year = int(request.args.get("year", today.year))
    month = int(request.args.get("month", today.month))
    month_start = date(year, month, 1)
    if month == 12:
        month_end = date(year + 1, 1, 1) - timedelta(days=1)
    else:
        month_end = date(year, month + 1, 1) - timedelta(days=1)

    entries = (TimeEntry.query
        .filter(TimeEntry.entry_date >= month_start, TimeEntry.entry_date <= month_end)
        .order_by(TimeEntry.entry_date)
        .all())

    by_project = {}
    for e in entries:
        key = e.target_key
        if key not in by_project:
            by_project[key] = {
                "target": e,
                "is_internal": e.is_internal,
                "rate": e.effective_rate,
                "entries": [],
                "total": 0,
            }
        by_project[key]["entries"].append(e)
        by_project[key]["total"] += e.hours

    return render_template("time/monthly.html",
        year=year, month=month, month_start=month_start, month_end=month_end,
        by_project=by_project,
        prev_month=((month_start - timedelta(days=1)).replace(day=1)),
        next_month=month_end + timedelta(days=1),
    )

# ── Expenses ──────────────────────────────────────────────────────────────────

ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp", "pdf"}

def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS

@app.route("/expenses")
@login_required
def expenses_index():
    status_filter = request.args.get("status", "")
    q = Expense.query.order_by(Expense.expense_date.desc())
    if status_filter:
        q = q.filter_by(status=status_filter)
    if request.args.get("target") == "internal":
        q = q.filter(Expense.internal_code_id != None)
    expenses = q.all()
    projects, internal_codes = _target_options(for_cost=True)
    return render_template("expenses/index.html", expenses=expenses, projects=projects,
                           internal_codes=internal_codes, status_filter=status_filter,
                           target_filter=request.args.get("target", ""))

@app.route("/expenses/capture", methods=["GET", "POST"])
@limiter.limit("10 per minute")
@login_required
def expenses_capture():
    """Mobile receipt capture page"""
    if request.method == "POST":
        file = request.files.get("receipt")
        if not file or not allowed_file(file.filename):
            flash("Ingen giltig fil / No valid file", "error")
            return redirect(url_for("expenses_capture"))

        filename = f"{secrets.token_hex(8)}_{secure_filename(file.filename)}"
        upload_path = os.path.join(app.config["UPLOAD_FOLDER"], filename)
        file.save(upload_path)

        # OCR via Claude Vision
        ocr_data = extract_receipt_data(upload_path, app.config["ANTHROPIC_API_KEY"])

        # Store in session for review step
        session["pending_expense"] = {
            "receipt_filename": filename,
            "ocr_data": ocr_data,
        }
        return redirect(url_for("expenses_review"))

    return render_template("expenses/capture.html")

@app.route("/expenses/review", methods=["GET", "POST"])
@login_required
def expenses_review():
    pending = session.get("pending_expense")
    if not pending:
        return redirect(url_for("expenses_capture"))

    from models import ExpenseCategory
    projects, internal_codes = _target_options(for_cost=True)
    categories = ExpenseCategory.query.filter_by(active=True).order_by(ExpenseCategory.sort_order, ExpenseCategory.name).all()

    if request.method == "POST":
        action = request.form.get("action")
        if action == "discard":
            # Remove uploaded file
            fp = os.path.join(app.config["UPLOAD_FOLDER"], pending["receipt_filename"])
            if os.path.exists(fp):
                os.remove(fp)
            session.pop("pending_expense", None)
            flash("Utlägg kasserat / Expense discarded", "info")
            return redirect(url_for("expenses_index"))

        # Save expense
        amount_incl = _safe_float(request.form.get("amount_incl_vat"), 0.0)
        reverse_charge = request.form.get("reverse_charge") == "1"
        if reverse_charge:
            # Omvänd skattskyldighet: the receipt carries no VAT, so the amount entered
            # is both the net cost and what is actually paid. The 25% is virtual and
            # booked to 2614/2647 by the voucher builder — it never touches the payment.
            vat_amount = 0.0
            amount_excl = amount_incl
            vat_rate = REVERSE_CHARGE_VAT_RATE
        else:
            vat_amount = round(_safe_float(request.form.get("vat_amount"), 0.0), 2)
            amount_excl = round(amount_incl - vat_amount, 2)
            vat_rate = round(vat_amount / amount_excl * 100, 1) if amount_excl > 0 else 0.0
        expense_date = _safe_date(request.form.get("expense_date"))
        if not expense_date:
            flash("Ogiltigt datum / Invalid date", "error")
            return redirect(url_for("expenses_review"))

        project_id, internal_code_id = _parse_target()
        exp = Expense(
            project_id=project_id,
            internal_code_id=internal_code_id,
            expense_category_id=_safe_int(request.form.get("expense_category_id")) or None,
            expense_date=expense_date,
            merchant=request.form.get("merchant", ""),
            description=request.form.get("description", ""),
            amount_incl_vat=amount_incl,
            amount_excl_vat=amount_excl,
            vat_amount=vat_amount,
            vat_rate=vat_rate,
            reverse_charge=reverse_charge,
            # An internal code is never invoiced, so the cost can never be billable.
            billable=(request.form.get("billable") == "1") and not internal_code_id,
            paid_by=request.form.get("paid_by", "personal"),
            receipt_filename=pending["receipt_filename"],
            status="approved",
            ocr_raw=json.dumps(pending.get("ocr_data", {})),
        )
        db.session.add(exp)
        db.session.commit()
        session.pop("pending_expense", None)

        # Post to Fortnox if connected
        try:
            fortnox = FortnoxClient(app.config)
            voucher_ref = fortnox.create_expense_voucher(exp)
            if voucher_ref and exp.receipt_filename:
                receipt_path = os.path.join(app.config["UPLOAD_FOLDER"], exp.receipt_filename)
                base = exp.receipt_filename
                if "_" in base and len(base.split("_")[0]) == 16:
                    base = base.split("_", 1)[1]
                base = base.replace("—", "-").replace("–", "-")
                attach_name = f"{voucher_ref} - {base}"
                _email_pdf_to_fortnox_inbox(receipt_path, attach_name, voucher_ref, app.config)
        except Exception as e:
            flash(f"Fortnox-fel / Fortnox error: {e}", "warning")

        flash("Utlägg sparat / Expense saved", "success")
        return redirect(url_for("expenses_index"))

    return render_template("expenses/review.html",
        pending=pending, projects=projects, internal_codes=internal_codes,
        categories=categories,
        ocr=pending.get("ocr_data", {}))

@app.route("/expenses/<int:exp_id>/delete", methods=["POST"])
@login_required
def expenses_delete(exp_id):
    exp = Expense.query.get_or_404(exp_id)
    if exp.invoice_id:
        flash("Kan ej ta bort fakturerade utlägg / Cannot delete invoiced expense", "error")
        return redirect(url_for("expenses_index"))
    if exp.receipt_filename:
        fp = os.path.join(app.config["UPLOAD_FOLDER"], exp.receipt_filename)
        if os.path.exists(fp):
            os.remove(fp)
    db.session.delete(exp)
    db.session.commit()
    flash("Borttaget / Deleted", "success")
    return redirect(url_for("expenses_index"))

# ── Mileage ───────────────────────────────────────────────────────────────────

@app.route("/mileage", methods=["GET", "POST"])
@login_required
def mileage_index():
    projects, internal_codes = _target_options(for_cost=True)
    if request.method == "POST":
        project_id, internal_code_id = _parse_target()
        entry_date = _safe_date(request.form.get("entry_date"))
        if not entry_date:
            flash("Ogiltigt datum / Invalid date", "error")
            return redirect(url_for("mileage_index"))
        if not project_id and not internal_code_id:
            flash("Välj ett uppdrag eller en intern kod / Select an assignment or internal code", "error")
            return redirect(url_for("mileage_index"))
        km = _safe_float(request.form.get("km"), 0.0)
        # Resolve km rate: PO for that date → client default, or the internal code's
        # own rate. A value typed into the form always wins.
        km_rate = None
        if project_id:
            project = Project.query.get(project_id)
            km_rate = project.get_km_rate(entry_date) if project else None
        elif internal_code_id:
            code = InternalCode.query.get(internal_code_id)
            km_rate = code.get_km_rate() if code else None
        km_rate_override = request.form.get("km_rate", "").strip()
        if km_rate_override:
            km_rate = _safe_float(km_rate_override, km_rate)
        if not km_rate:
            km_rate = DEFAULT_KM_RATE
        m = MileageEntry(
            project_id=project_id,
            internal_code_id=internal_code_id,
            entry_date=entry_date,
            km=km,
            km_rate=km_rate,
            description=request.form.get("description", ""),
            # Internal mileage is a company cost, never resold.
            billable=(request.form.get("billable") == "1") and not internal_code_id,
            status="approved",
        )
        db.session.add(m)
        db.session.commit()
        flash("Reseersättning sparad / Mileage saved", "success")
        return redirect(url_for("mileage_index"))
    entries = MileageEntry.query.order_by(MileageEntry.entry_date.desc()).all()
    return render_template("mileage/index.html", entries=entries, projects=projects,
                           internal_codes=internal_codes, today=date.today())

@app.route("/mileage/<int:entry_id>/delete", methods=["POST"])
@login_required
def mileage_delete(entry_id):
    m = MileageEntry.query.get_or_404(entry_id)
    if m.invoice_id:
        flash("Kan ej ta bort fakturerad reseersättning / Cannot delete invoiced mileage", "error")
        return redirect(url_for("mileage_index"))
    db.session.delete(m)
    db.session.commit()
    flash("Borttaget / Deleted", "success")
    return redirect(url_for("mileage_index"))

# ── Invoices ──────────────────────────────────────────────────────────────────

@app.route("/invoices")
@login_required
def invoices_list():
    invoices = Invoice.query.order_by(Invoice.issue_date.desc()).all()
    return render_template("invoices/index.html", invoices=invoices, today=date.today())

@app.route("/invoices/new", methods=["GET", "POST"])
@limiter.limit("20 per hour")
@login_required
def invoices_new():
    projects = (Project.query
        .join(Client)
        .filter(Client.active == True, Project.active == True)
        .order_by(Client.name, Project.name)
        .all())
    today = date.today()
    if request.method == "POST":
        project_id = _safe_int(request.form.get("project_id"))
        project = Project.query.get_or_404(project_id)
        client = project.client
        client_id = client.id
        period_start = _safe_date(request.form.get("period_start"))
        period_end = _safe_date(request.form.get("period_end"))
        if not period_start or not period_end:
            flash("Ogiltiga datum / Invalid dates", "error")
            return redirect(url_for("invoices_new"))

        # Check for existing draft/approved invoices overlapping this period for this assignment
        confirm_replace = request.form.get("confirm_replace") == "1"
        conflicting = Invoice.query.filter(
            Invoice.project_id == project_id,
            Invoice.status.in_(["draft", "approved"]),
            Invoice.period_start <= period_end,
            Invoice.period_end >= period_start,
        ).all()
        if conflicting and not confirm_replace:
            return render_template("invoices/new.html",
                projects=projects,
                default_start=request.form["period_start"],
                default_end=request.form["period_end"],
                selected_project_id=project_id,
                conflicting=conflicting,
            )
        # Delete confirmed conflicts before creating
        for old_inv in conflicting:
            for e in old_inv.time_entries:
                e.invoice_id = None
                e.invoiced = False
            for exp in old_inv.expenses:
                exp.invoice_id = None
                exp.status = "approved"
            db.session.delete(old_inv)
        db.session.flush()

        # Gather uninvoiced time entries for this assignment
        entries = (TimeEntry.query
            .filter(TimeEntry.project_id == project_id,
                    TimeEntry.internal_code_id == None,
                    TimeEntry.entry_date >= period_start,
                    TimeEntry.entry_date <= period_end,
                    TimeEntry.invoiced == False)
            .order_by(TimeEntry.entry_date)
            .all())

        # Gather approved billable expenses for this assignment
        expenses = (Expense.query
            .filter(
                Expense.project_id == project_id,
                Expense.internal_code_id == None,
                Expense.billable == True,
                Expense.status == "approved",
                Expense.invoice_id == None,
                Expense.expense_date >= period_start,
                Expense.expense_date <= period_end,
            ).all())

        # Gather approved billable mileage entries for this assignment
        mileage = (MileageEntry.query
            .filter(
                MileageEntry.project_id == project_id,
                MileageEntry.internal_code_id == None,
                MileageEntry.billable == True,
                MileageEntry.status == "approved",
                MileageEntry.invoice_id == None,
                MileageEntry.entry_date >= period_start,
                MileageEntry.entry_date <= period_end,
            ).all())

        markup_factor = 1 + (project.expense_markup_pct or 10.0) / 100
        time_subtotal = round(sum(e.hours * e.effective_rate for e in entries), 2)
        expense_subtotal = round(sum(e.amount_excl_vat * markup_factor for e in expenses), 2)
        mileage_subtotal = round(sum(m.amount for m in mileage), 2)
        subtotal = time_subtotal + expense_subtotal + mileage_subtotal
        vat = round(subtotal * 0.25, 2)
        total = round(subtotal + vat)  # whole kronor

        inv = Invoice(
            client_id=client_id,
            project_id=project_id,
            period_start=period_start,
            period_end=period_end,
            issue_date=today,
            due_date=today + timedelta(days=client.payment_days),
            language=client.invoice_language,
            subtotal=subtotal,
            vat_amount=vat,
            total=total,
            currency=client.currency,
            status="draft",
        )
        from sqlalchemy.exc import IntegrityError as _IntegrityError
        db.session.add(inv)
        for attempt in range(5):
            inv.generate_number(app.config["FY_START_MONTH"])
            try:
                db.session.flush()
                break
            except _IntegrityError:
                db.session.rollback()
                if attempt == 4:
                    flash("Fakturanummer kunde inte tilldelas – försök igen / Could not assign invoice number – please retry", "error")
                    return redirect(url_for("invoices_create"))

        for e in entries:
            e.invoice_id = inv.id
        for exp in expenses:
            exp.invoice_id = inv.id
        for m in mileage:
            m.invoice_id = inv.id

        db.session.commit()
        return redirect(url_for("invoices_proforma", invoice_id=inv.id))

    # Default period: last month
    first_of_month = today.replace(day=1)
    last_month_end = first_of_month - timedelta(days=1)
    last_month_start = last_month_end.replace(day=1)

    return render_template("invoices/new.html",
        projects=projects,
        default_start=last_month_start.isoformat(),
        default_end=last_month_end.isoformat(),
    )

@app.route("/invoices/<int:invoice_id>")
@login_required
def invoices_proforma(invoice_id):
    inv = (Invoice.query
        .options(
            selectinload(Invoice.time_entries).selectinload(TimeEntry.hour_type).selectinload(POHourType.po),
            selectinload(Invoice.expenses),
            selectinload(Invoice.mileage_entries),
        )
        .get_or_404(invoice_id))
    return render_template("invoices/proforma.html", inv=inv)

@app.route("/invoices/<int:invoice_id>/approve", methods=["POST"])
@login_required
def invoices_approve(invoice_id):
    inv = Invoice.query.get_or_404(invoice_id)
    inv.status = "approved"
    for e in inv.time_entries:
        e.invoiced = True
    for exp in inv.expenses:
        exp.status = "invoiced"
    for m in inv.mileage_entries:
        m.status = "invoiced"
    db.session.commit()

    # Generate PDF
    try:
        pdf_path = generate_invoice_pdf(inv, app.config)
        inv.pdf_filename = os.path.basename(pdf_path)
        db.session.commit()
    except Exception as e:
        flash("PDF kunde inte genereras – använd knappen Skriv ut för att spara som PDF via webbläsaren. "
              "Kör 'brew install pango' för att aktivera automatisk PDF-generering. / "
              "PDF could not be generated – use the Print button to save as PDF via the browser. "
              "Run 'brew install pango' to enable automatic PDF generation.", "warning")

    _audit("invoice_approved", f"invoice_id={invoice_id} number={inv.invoice_number} total={inv.total}")
    flash("Faktura godkänd / Invoice approved", "success")
    return redirect(url_for("invoices_proforma", invoice_id=invoice_id))

@app.route("/invoices/<int:invoice_id>/send", methods=["POST"])
@login_required
def invoices_send(invoice_id):
    inv = Invoice.query.get_or_404(invoice_id)
    if inv.status not in ("approved",):
        flash("Faktura måste godkännas först / Invoice must be approved first", "error")
        return redirect(url_for("invoices_proforma", invoice_id=invoice_id))

    # Send email
    try:
        _send_invoice_email(inv, app.config)
        inv.status = "sent"
        inv.sent_at = datetime.utcnow()
        db.session.commit()
        flash("Faktura skickad / Invoice sent", "success")
    except Exception as e:
        flash(f"E-postfel / Email error: {e}", "error")

    # ── Fortnox live sync ──
    if Settings.get("fortnox_access_token"):
        try:
            # Ensure PDF exists before posting (it's attached to the voucher)
            if not inv.pdf_filename:
                pdf_path = generate_invoice_pdf(inv, app.config)
                inv.pdf_filename = os.path.basename(pdf_path)
                db.session.commit()
            fortnox = FortnoxClient(app.config)
            result = fortnox.create_outgoing_invoice_voucher(inv)
            voucher = result.get("Voucher", {})
            voucher_nr = voucher.get("VoucherNumber")
            voucher_series = voucher.get("VoucherSeries", "B")
            fy_year = (inv.issue_date or date.today()).year
            voucher_ref = f"{fy_year}-{voucher_series}{voucher_nr}" if voucher_nr else None
            if voucher_ref:
                inv.fortnox_invoice_nr = voucher_ref
                db.session.commit()
                if inv.pdf_filename:
                    pdf_path = os.path.join(app.root_path, "static", "uploads", inv.pdf_filename)
                    attach_name = f"{voucher_ref} - {inv.invoice_number}.pdf"
                    _email_pdf_to_fortnox_inbox(pdf_path, attach_name, voucher_ref, app.config)
                flash(f"Fortnox verifikat skapat: {voucher_ref} / Voucher created: {voucher_ref}", "success")
        except Exception as e:
            flash(f"Fortnox-fel / Fortnox error: {e}", "warning")
        return redirect(url_for("invoices_proforma", invoice_id=invoice_id))

    # Fortnox not connected — show dry-run payload preview instead
    session["fortnox_preview"] = _build_outgoing_invoice_voucher_preview(inv)
    session["fortnox_preview"]["back_url"] = url_for("invoices_proforma", invoice_id=invoice_id)
    session["fortnox_preview"]["back_label"] = f"← Faktura {inv.invoice_number}"
    return redirect(url_for("fortnox_preview"))

@app.route("/invoices/<int:invoice_id>/download")
@login_required
def invoices_download(invoice_id):
    inv = Invoice.query.get_or_404(invoice_id)
    if not inv.pdf_filename:
        try:
            pdf_path = generate_invoice_pdf(inv, app.config)
            inv.pdf_filename = os.path.basename(pdf_path)
            db.session.commit()
        except Exception as e:
            flash(f"PDF-generering misslyckades – använd Skriv ut istället / PDF generation failed – use Print instead: {e}", "warning")
            return redirect(url_for("invoices_print", invoice_id=invoice_id))

    pdf_path = os.path.join(app.root_path, "static", "uploads", inv.pdf_filename)
    return send_file(pdf_path, as_attachment=True,
                     download_name=f"faktura-{inv.invoice_number}.pdf")


@app.route("/invoices/<int:invoice_id>/print")
@login_required
def invoices_print(invoice_id):
    """Render invoice as printable HTML (fallback when WeasyPrint is unavailable)."""
    inv = Invoice.query.get_or_404(invoice_id)
    html = render_invoice_html(inv, app.config)
    return html, 200, {"Content-Type": "text/html; charset=utf-8"}

@app.route("/invoices/<int:invoice_id>/mark-paid", methods=["POST"])
@login_required
def invoices_mark_paid(invoice_id):
    inv = Invoice.query.get_or_404(invoice_id)
    inv.status = "paid"
    db.session.commit()
    _audit("invoice_marked_paid", f"invoice_id={invoice_id} number={inv.invoice_number}")
    flash("Markerad som betald / Marked as paid", "success")
    return redirect(url_for("invoices_list"))

@app.route("/invoices/<int:invoice_id>/set-number", methods=["POST"])
@login_required
def invoices_set_number(invoice_id):
    import re
    inv = Invoice.query.get_or_404(invoice_id)
    if inv.status != "draft":
        flash("Kan bara ändra nummer på utkast / Can only change number on drafts", "error")
        return redirect(url_for("invoices_proforma", invoice_id=invoice_id))
    new_number = request.form.get("invoice_number", "").strip()
    if not re.match(r"^\d{4}-\d{3}$", new_number):
        flash("Ogiltigt format — använd ÅÅÅÅ-NNN t.ex. 2025-007 / Invalid format — use YYYY-NNN e.g. 2025-007", "error")
        return redirect(url_for("invoices_proforma", invoice_id=invoice_id))
    conflict = Invoice.query.filter(Invoice.invoice_number == new_number, Invoice.id != invoice_id).first()
    if conflict:
        flash(f"Fakturanummer {new_number} används redan / Invoice number {new_number} already in use", "error")
        return redirect(url_for("invoices_proforma", invoice_id=invoice_id))
    inv.invoice_number = new_number
    db.session.commit()
    flash(f"Fakturanummer uppdaterat till {new_number} / Invoice number updated to {new_number}", "success")
    return redirect(url_for("invoices_proforma", invoice_id=invoice_id))

@app.route("/invoices/<int:invoice_id>/delete", methods=["POST"])
@login_required
def invoices_delete(invoice_id):
    inv = Invoice.query.get_or_404(invoice_id)
    if inv.status not in ("draft",):
        flash("Kan bara ta bort utkast / Can only delete drafts", "error")
        return redirect(url_for("invoices_list"))
    # Unlink entries
    for e in inv.time_entries:
        e.invoice_id = None
        e.invoiced = False
    for exp in inv.expenses:
        exp.invoice_id = None
        exp.status = "approved"
    for m in inv.mileage_entries:
        m.invoice_id = None
        m.status = "approved"
    inv_number = inv.invoice_number
    db.session.delete(inv)
    db.session.commit()
    _audit("invoice_deleted", f"invoice_id={invoice_id} number={inv_number}")
    flash("Faktura borttagen / Invoice deleted", "success")
    return redirect(url_for("invoices_list"))

# ── Fortnox OAuth ─────────────────────────────────────────────────────────────

@app.route("/fortnox/sync-payments", methods=["POST"])
@limiter.limit("5 per hour")
@login_required
@admin_required
def fortnox_sync_payments():
    """Check Fortnox C-series vouchers and mark matching invoices as paid."""
    if not Settings.get("fortnox_access_token"):
        flash("Fortnox ej anslutet / Fortnox not connected", "error")
        return redirect(url_for("invoices_list"))
    try:
        fortnox = FortnoxClient(app.config)
        paid_nrs = fortnox.get_paid_invoice_numbers()
        app.logger.info("Fortnox payment sync — found C-series invoice numbers: %s", paid_nrs)
        count = 0
        if paid_nrs:
            from models import Invoice as Inv
            invoices = Inv.query.filter(
                Inv.status == "sent",
                Inv.invoice_number.in_(paid_nrs)
            ).all()
            for inv in invoices:
                inv.status = "paid"
                count += 1
            db.session.commit()
        flash(f"{count} faktura(or) markerade som betalda / {count} invoice(s) marked as paid", "success")
    except Exception as e:
        flash(f"Fortnox sync-fel: {e}", "error")
    return redirect(url_for("invoices_list"))


@app.route("/fortnox/connect")
@login_required
def fortnox_connect():
    fortnox = FortnoxClient(app.config)
    auth_url = fortnox.get_auth_url()
    app.logger.info("Fortnox OAuth — redirecting to auth URL: %s", auth_url)
    return redirect(auth_url)

@app.route("/fortnox/callback")
@login_required
def fortnox_callback():
    app.logger.info("Fortnox callback received — args: %s", dict(request.args))
    code = request.args.get("code")
    if not code:
        error = request.args.get("error", "no_code")
        error_desc = request.args.get("error_description", "")
        app.logger.error("Fortnox callback missing code. error=%s desc=%s args=%s",
                         error, error_desc, dict(request.args))
        flash(f"Fortnox-anslutning misslyckades: {error} — {error_desc} / "
              f"Connection failed: {error} — {error_desc}", "error")
        return redirect(url_for("settings"))
    try:
        fortnox = FortnoxClient(app.config)
        tokens = fortnox.exchange_code(code)
        app.logger.info("Fortnox token exchange success — keys: %s", list(tokens.keys()))
        Settings.set("fortnox_access_token", tokens["access_token"])
        Settings.set("fortnox_refresh_token", tokens.get("refresh_token", ""))
        flash("Fortnox anslutet / Fortnox connected!", "success")
    except Exception as e:
        app.logger.error("Fortnox token exchange failed: %s", e, exc_info=True)
        flash(f"Fortnox-fel: {e}", "error")
    return redirect(url_for("settings"))

@app.route("/settings")
@login_required
def settings():
    from models import ExpenseCategory, SupplierCategory
    fortnox_connected = bool(Settings.get("fortnox_access_token"))
    users = User.query.order_by(User.display_name).all() if current_user.is_admin else []
    expense_cats = ExpenseCategory.query.filter_by(active=True).order_by(ExpenseCategory.sort_order, ExpenseCategory.name).all()
    supplier_cats = SupplierCategory.query.filter_by(active=True).order_by(SupplierCategory.sort_order, SupplierCategory.name).all()
    internal_codes = InternalCode.query.filter_by(active=True).order_by(InternalCode.sort_order, InternalCode.code).all()
    return render_template("settings.html",
        fortnox_connected=fortnox_connected,
        users=users,
        expense_categories=expense_cats,
        supplier_categories=supplier_cats,
        internal_codes=internal_codes,
        currencies=_get_currencies())

# ── User management ───────────────────────────────────────────────────────────

@app.route("/settings/users/new", methods=["GET", "POST"])
@login_required
@admin_required
def user_new():
    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        display_name = request.form.get("display_name", "").strip()
        role = request.form.get("role", "employee")
        if not email or not display_name:
            flash("Namn och e-post krävs / Name and email are required", "error")
            return redirect(url_for("user_new"))
        if User.query.filter_by(email=email).first():
            flash("E-postadressen används redan / Email already in use", "error")
            return redirect(url_for("user_new"))
        user = User(
            display_name=display_name,
            email=email,
            role=role,
            active=True,
        )
        db.session.add(user)
        db.session.commit()
        flash(f"Användare {display_name} skapad / User {display_name} created", "success")
        return redirect(url_for("settings") + "#users")
    return render_template("users/form.html", user=None)

@app.route("/settings/users/<int:user_id>/edit", methods=["GET", "POST"])
@login_required
@admin_required
def user_edit(user_id):
    user = User.query.get_or_404(user_id)
    if request.method == "POST":
        user.display_name = request.form.get("display_name", user.display_name).strip()
        user.email = (request.form.get("email") or user.email).strip().lower()
        user.role = request.form.get("role", user.role)
        active_val = request.form.get("active", "1")
        # Prevent admin from deactivating themselves
        if user.id == current_user.id and active_val != "1":
            flash("Du kan inte inaktivera ditt eget konto / Cannot deactivate your own account", "error")
        else:
            user.active = (active_val == "1")
        db.session.commit()
        flash("Användare uppdaterad / User updated", "success")
        return redirect(url_for("settings") + "#users")
    return render_template("users/form.html", user=user)

# ── Internal code reporting ───────────────────────────────────────────────────

@app.route("/internal")
@login_required
def internal_report():
    """Time and cost per internal code over a period, defaulting to the fiscal year.

    Costs are summed excluding VAT, matching how assignment budgets are reported.
    Mileage has no VAT, so its amount is used as-is.
    """
    today = date.today()
    fy = _safe_int(request.args.get("fy")) or fiscal_year(today, app.config["FY_START_MONTH"])
    period_start = _safe_date(request.args.get("from")) or fy_start(fy, app.config["FY_START_MONTH"])
    period_end   = _safe_date(request.args.get("to"))   or fy_end(fy, app.config["FY_START_MONTH"])

    codes = InternalCode.query.order_by(InternalCode.sort_order, InternalCode.code).all()
    rows = []
    for c in codes:
        hours = sum(e.hours for e in TimeEntry.query.filter(
            TimeEntry.internal_code_id == c.id,
            TimeEntry.entry_date >= period_start,
            TimeEntry.entry_date <= period_end).all())

        expenses = Expense.query.filter(
            Expense.internal_code_id == c.id,
            Expense.status != "pending",
            Expense.expense_date >= period_start,
            Expense.expense_date <= period_end).all()
        expense_cost = sum(e.amount_excl_vat or 0.0 for e in expenses)

        mileage = MileageEntry.query.filter(
            MileageEntry.internal_code_id == c.id,
            MileageEntry.entry_date >= period_start,
            MileageEntry.entry_date <= period_end).all()
        mileage_cost = sum(m.amount for m in mileage)
        mileage_km   = sum(m.km for m in mileage)

        suppliers = SupplierInvoice.query.filter(
            SupplierInvoice.internal_code_id == c.id,
            SupplierInvoice.invoice_date >= period_start,
            SupplierInvoice.invoice_date <= period_end).all()
        supplier_cost = sum(si.amount_excl_vat or 0.0 for si in suppliers)

        total_cost = expense_cost + mileage_cost + supplier_cost
        if not hours and not total_cost:
            continue
        rows.append({
            "code": c,
            "hours": round(hours, 2),
            "expense_cost": round(expense_cost, 2),
            "expense_count": len(expenses),
            "mileage_cost": round(mileage_cost, 2),
            "mileage_km": round(mileage_km, 1),
            "supplier_cost": round(supplier_cost, 2),
            "supplier_count": len(suppliers),
            "total_cost": round(total_cost, 2),
        })

    totals = {
        "hours":      round(sum(r["hours"] for r in rows), 2),
        "total_cost": round(sum(r["total_cost"] for r in rows), 2),
    }
    return render_template("internal/report.html",
        rows=rows, totals=totals, fy=fy,
        period_start=period_start, period_end=period_end,
        prev_fy=fy - 1, next_fy=fy + 1)


# ── Internal time/cost codes ──────────────────────────────────────────────────

INTERNAL_CODE_KINDS = [
    ("sales",   "Försäljning",    "Sales"),
    ("absence", "Frånvaro",       "Absence"),
    ("admin",   "Administration", "Administration"),
    ("other",   "Övrigt",         "Other"),
]


@app.route("/settings/internal-codes", methods=["GET", "POST"])
@login_required
@admin_required
def internal_codes():
    if request.method == "POST":
        action = request.form.get("action")

        if action == "new":
            code = (request.form.get("code") or "").strip().upper()
            name = (request.form.get("name") or "").strip()
            if not code or not name:
                flash("Kod och namn krävs / Code and name are required", "error")
                return redirect(url_for("internal_codes"))
            if InternalCode.query.filter(db.func.upper(InternalCode.code) == code).first():
                flash(f"Koden {code} finns redan / Code {code} already exists", "error")
                return redirect(url_for("internal_codes"))
            db.session.add(InternalCode(
                code=code,
                name=name,
                description=(request.form.get("description") or "").strip(),
                kind=request.form.get("kind", "other"),
                track_time=request.form.get("track_time") == "1",
                track_cost=request.form.get("track_cost") == "1",
                km_rate=_safe_float(request.form.get("km_rate"), 0.0) or None,
                sort_order=_safe_int(request.form.get("sort_order")) or (InternalCode.query.count() + 1),
                active=True,
            ))
            db.session.commit()
            _audit("internal_code_create", code)
            flash("Intern kod skapad / Internal code created", "success")

        elif action == "save":
            c = InternalCode.query.get(_safe_int(request.form.get("code_id")))
            if c:
                new_code = (request.form.get("code") or c.code).strip().upper()
                clash = InternalCode.query.filter(
                    db.func.upper(InternalCode.code) == new_code,
                    InternalCode.id != c.id,
                ).first()
                if clash:
                    flash(f"Koden {new_code} finns redan / Code {new_code} already exists", "error")
                    return redirect(url_for("internal_codes"))
                c.code        = new_code
                c.name        = (request.form.get("name") or c.name).strip()
                c.description = (request.form.get("description") or "").strip()
                c.kind        = request.form.get("kind", c.kind)
                c.track_time  = request.form.get("track_time") == "1"
                c.track_cost  = request.form.get("track_cost") == "1"
                c.km_rate     = _safe_float(request.form.get("km_rate"), 0.0) or None
                c.sort_order  = _safe_int(request.form.get("sort_order"), c.sort_order)
                db.session.commit()
                _audit("internal_code_update", c.code)
                flash("Intern kod sparad / Internal code saved", "success")

        elif action in ("deactivate", "activate"):
            c = InternalCode.query.get(_safe_int(request.form.get("code_id")))
            if c:
                c.active = (action == "activate")
                db.session.commit()
                _audit(f"internal_code_{action}", c.code)
                flash("Intern kod uppdaterad / Internal code updated", "success")

        elif action == "delete":
            # Only ever deletable while nothing points at it; otherwise deactivate,
            # so historical time and costs keep their label.
            c = InternalCode.query.get(_safe_int(request.form.get("code_id")))
            if c:
                if _internal_code_usage(c)["total"]:
                    flash("Koden används – inaktivera i stället / Code is in use – deactivate instead", "error")
                else:
                    db.session.delete(c)
                    db.session.commit()
                    _audit("internal_code_delete", c.code)
                    flash("Intern kod borttagen / Internal code deleted", "success")

        return redirect(url_for("internal_codes"))

    codes = InternalCode.query.order_by(InternalCode.sort_order, InternalCode.code).all()
    usage = {c.id: _internal_code_usage(c) for c in codes}
    return render_template("settings/internal_codes.html",
        codes=codes, usage=usage, kinds=INTERNAL_CODE_KINDS,
        default_km_rate=DEFAULT_KM_RATE)


def _internal_code_usage(code):
    """How many rows of each kind point at this code — drives the delete guard."""
    counts = {
        "time":      TimeEntry.query.filter_by(internal_code_id=code.id).count(),
        "expenses":  Expense.query.filter_by(internal_code_id=code.id).count(),
        "mileage":   MileageEntry.query.filter_by(internal_code_id=code.id).count(),
        "suppliers": SupplierInvoice.query.filter_by(internal_code_id=code.id).count(),
    }
    counts["total"] = sum(counts.values())
    return counts


# ── Expense categories ────────────────────────────────────────────────────────

@app.route("/settings/expense-categories", methods=["GET", "POST"])
@login_required
@admin_required
def expense_categories():
    from models import ExpenseCategory
    if request.method == "POST":
        action = request.form.get("action")
        if action == "new":
            name = request.form.get("name", "").strip()
            debit_account = request.form.get("debit_account", "").strip()
            if name and debit_account:
                sort_order = ExpenseCategory.query.count() + 1
                db.session.add(ExpenseCategory(name=name, debit_account=debit_account, sort_order=sort_order))
                db.session.commit()
                flash("Kategori skapad / Category created", "success")
        elif action == "delete":
            cat_id = _safe_int(request.form.get("category_id"))
            cat = ExpenseCategory.query.get(cat_id)
            if cat:
                cat.active = False
                db.session.commit()
                flash("Kategori inaktiverad / Category deactivated", "success")
        elif action == "save":
            cat_id = _safe_int(request.form.get("category_id"))
            cat = ExpenseCategory.query.get(cat_id)
            if cat:
                cat.name = request.form.get("name", cat.name).strip()
                cat.debit_account = request.form.get("debit_account", cat.debit_account).strip()
                db.session.commit()
                flash("Kategori sparad / Category saved", "success")
        return redirect(url_for("expense_categories"))
    categories = ExpenseCategory.query.order_by(ExpenseCategory.sort_order, ExpenseCategory.name).all()
    return render_template("settings/expense_categories.html", categories=categories)


def _get_currencies():
    raw = Settings.get("invoice_currencies", "SEK,EUR")
    return [c.strip().upper() for c in raw.split(",") if c.strip()]


@app.route("/settings/currencies", methods=["GET", "POST"])
@login_required
@admin_required
def settings_currencies():
    if request.method == "POST":
        action = request.form.get("action")
        if action == "add":
            code = request.form.get("code", "").strip().upper()[:3]
            if code and code.isalpha():
                codes = _get_currencies()
                if code not in codes:
                    codes.append(code)
                Settings.set("invoice_currencies", ",".join(codes))
        elif action == "delete":
            code = request.form.get("code", "").strip().upper()
            codes = [c for c in _get_currencies() if c != code]
            Settings.set("invoice_currencies", ",".join(codes) or "SEK")
        elif action == "set_default":
            code = request.form.get("code", "").strip().upper()
            codes = _get_currencies()
            if code in codes:
                codes.remove(code)
                codes.insert(0, code)
                Settings.set("invoice_currencies", ",".join(codes))
        return redirect(url_for("settings_currencies"))
    return render_template("settings/currencies.html", currencies=_get_currencies())


@app.route("/settings/supplier-categories", methods=["GET", "POST"])
@login_required
@admin_required
def supplier_categories():
    from models import SupplierCategory
    if request.method == "POST":
        action = request.form.get("action")
        if action == "new":
            name = request.form.get("name", "").strip()
            debit_account = request.form.get("debit_account", "").strip()
            if name and debit_account:
                sort_order = SupplierCategory.query.count() + 1
                db.session.add(SupplierCategory(name=name, debit_account=debit_account, sort_order=sort_order))
                db.session.commit()
                flash("Kategori skapad / Category created", "success")
        elif action == "delete":
            cat_id = _safe_int(request.form.get("category_id"))
            cat = SupplierCategory.query.get(cat_id)
            if cat:
                cat.active = False
                db.session.commit()
                flash("Kategori inaktiverad / Category deactivated", "success")
        elif action == "save":
            cat_id = _safe_int(request.form.get("category_id"))
            cat = SupplierCategory.query.get(cat_id)
            if cat:
                cat.name = request.form.get("name", cat.name).strip()
                cat.debit_account = request.form.get("debit_account", cat.debit_account).strip()
                db.session.commit()
                flash("Kategori sparad / Category saved", "success")
        return redirect(url_for("supplier_categories"))
    categories = SupplierCategory.query.order_by(SupplierCategory.sort_order, SupplierCategory.name).all()
    return render_template("settings/supplier_categories.html", categories=categories)


@app.route("/settings/password", methods=["POST"])
@login_required
def change_password():
    new_pw = request.form.get("new_password", "")
    confirm = request.form.get("confirm_password", "")
    if new_pw != confirm:
        flash("Lösenorden matchar inte / Passwords don't match", "error")
    elif len(new_pw) < 8:
        flash("Lösenordet måste vara minst 8 tecken / Password must be at least 8 characters", "error")
    else:
        current_user.password_hash = generate_password_hash(new_pw)
        db.session.commit()
        flash("Lösenord uppdaterat / Password updated", "success")
    return redirect(url_for("settings"))

# ── Backup / Restore ──────────────────────────────────────────────────────────

def _model_rows(model_class):
    """Serialize all rows of a SQLAlchemy model to a list of plain dicts."""
    rows = []
    for obj in model_class.query.all():
        d = {}
        for col in obj.__table__.columns:
            val = getattr(obj, col.name)
            if isinstance(val, (datetime, date)):
                val = val.isoformat()
            d[col.name] = val
        rows.append(d)
    return rows


def _restore_rows(model_class, records, date_cols=(), datetime_cols=()):
    """Re-insert records (with original IDs) into the database."""
    for r in records:
        kwargs = dict(r)
        for col in date_cols:
            if kwargs.get(col):
                kwargs[col] = date.fromisoformat(kwargs[col])
        for col in datetime_cols:
            if kwargs.get(col):
                kwargs[col] = datetime.fromisoformat(kwargs[col])
        db.session.add(model_class(**kwargs))


@app.route("/settings/backup")
@login_required
def settings_backup():
    payload = {
        "version": 1,
        "exported_at": datetime.utcnow().isoformat(),
        "data": {
            "clients":         _model_rows(Client),
            "internal_codes":  _model_rows(InternalCode),
            "projects":        _model_rows(Project),
            "purchase_orders": _model_rows(PurchaseOrder),
            "po_hour_types":   _model_rows(POHourType),
            "invoices":        _model_rows(Invoice),
            "time_entries":    _model_rows(TimeEntry),
            "expenses":        _model_rows(Expense),
            "mileage_entries": _model_rows(MileageEntry),
            "settings":        _model_rows(Settings),
            "users":           _model_rows(User),
        },
    }
    buf = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    ts  = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    return send_file(
        io.BytesIO(buf),
        mimetype="application/json",
        as_attachment=True,
        download_name=f"onedesk_backup_{ts}.json",
    )


@app.route("/settings/restore", methods=["POST"])
@login_required
def settings_restore():
    f = request.files.get("backup_file")
    if not f or not f.filename:
        flash("Ingen fil vald / No file selected", "error")
        return redirect(url_for("settings"))

    try:
        payload = json.loads(f.read().decode("utf-8"))
    except Exception as e:
        flash(f"Ogiltig backup-fil / Invalid backup file: {e}", "error")
        return redirect(url_for("settings"))

    if payload.get("version") != 1:
        flash("Okänd backup-version / Unknown backup version", "error")
        return redirect(url_for("settings"))

    d = payload.get("data", {})

    try:
        # Delete in FK dependency order (children first)
        TimeEntry.query.delete()
        MileageEntry.query.delete()
        Expense.query.delete()
        POHourType.query.delete()
        PurchaseOrder.query.delete()
        Invoice.query.delete()
        Project.query.delete()
        Client.query.delete()
        InternalCode.query.delete()
        Settings.query.delete()
        User.query.delete()
        db.session.flush()

        # Restore in parent-first order
        _restore_rows(Client, d.get("clients", []),
                      datetime_cols=("created_at",))
        _restore_rows(InternalCode, d.get("internal_codes", []),
                      datetime_cols=("created_at",))
        _restore_rows(Project, d.get("projects", []),
                      date_cols=("start_date", "end_date"),
                      datetime_cols=("created_at",))
        _restore_rows(PurchaseOrder, d.get("purchase_orders", []),
                      date_cols=("valid_from", "valid_to"),
                      datetime_cols=("created_at",))
        _restore_rows(POHourType, d.get("po_hour_types", []),
                      datetime_cols=("created_at",))
        _restore_rows(Invoice, d.get("invoices", []),
                      date_cols=("period_start", "period_end", "issue_date", "due_date"),
                      datetime_cols=("created_at", "sent_at"))
        _restore_rows(TimeEntry, d.get("time_entries", []),
                      date_cols=("entry_date",),
                      datetime_cols=("created_at",))
        _restore_rows(Expense, d.get("expenses", []),
                      date_cols=("expense_date",),
                      datetime_cols=("created_at",))
        _restore_rows(MileageEntry, d.get("mileage_entries", []),
                      date_cols=("entry_date",),
                      datetime_cols=("created_at",))
        _restore_rows(Settings, d.get("settings", []))
        _restore_rows(User, d.get("users", []))

        db.session.commit()
        flash("Databasen återställd / Database restored successfully", "success")
    except Exception as e:
        db.session.rollback()
        flash(f"Fel vid återställning / Restore failed: {e}", "error")

    return redirect(url_for("settings"))


@app.route("/settings/log")
@login_required
@admin_required
def settings_log():
    """Return the current week's log file as plain text."""
    log_path = os.path.join(app.root_path, "logs", "onedesk.log")
    if not os.path.exists(log_path):
        return "No log file yet.", 200, {"Content-Type": "text/plain; charset=utf-8"}
    # Read last 2000 lines so the browser doesn't choke on huge files
    with open(log_path, encoding="utf-8", errors="replace") as f:
        lines = f.readlines()
    tail = "".join(lines[-2000:])
    return tail, 200, {"Content-Type": "text/plain; charset=utf-8"}


# ── Supplier invoices ─────────────────────────────────────────────────────────

SUPPLIER_UPLOAD_FOLDER_NAME = "supplier"

def _supplier_upload_folder():
    base = os.path.join(app.root_path, "instance", "uploads", "supplier")
    os.makedirs(base, exist_ok=True)
    return base

@app.route("/supplier-invoices")
@login_required
def supplier_invoices_index():
    _sup_opts = selectinload(SupplierInvoice.supplier_category)
    pending = SupplierInvoice.query.filter(
        SupplierInvoice.status == "pending"
    ).options(_sup_opts).order_by(SupplierInvoice.due_date).all()
    # "approved" = saved but Fortnox booking failed; "booked" = fully booked, ready to pay
    backlog = SupplierInvoice.query.filter(
        SupplierInvoice.status.in_(["approved", "booked"])
    ).options(_sup_opts).order_by(SupplierInvoice.due_date).all()
    paid = SupplierInvoice.query.filter_by(status="paid").options(_sup_opts).order_by(SupplierInvoice.created_at.desc()).limit(20).all()
    payment_files = PaymentFile.query.order_by(PaymentFile.created_at.desc()).limit(10).all()
    return render_template("supplier_invoices/index.html",
        pending=pending, backlog=backlog, paid=paid, payment_files=payment_files)

@app.route("/supplier-invoices/upload", methods=["GET", "POST"])
@limiter.limit("10 per minute")
@login_required
def supplier_invoices_upload():
    if request.method == "POST":
        file = request.files.get("invoice_file")
        if not file or not file.filename:
            flash("Ingen fil vald / No file selected", "error")
            return redirect(url_for("supplier_invoices_upload"))
        ext = file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else ""
        if ext not in {"pdf", "jpg", "jpeg", "png", "webp"}:
            flash("Filtypen stöds ej / Unsupported file type", "error")
            return redirect(url_for("supplier_invoices_upload"))

        folder = _supplier_upload_folder()
        filename = f"{secrets.token_hex(8)}_{secure_filename(file.filename)}"
        save_path = os.path.join(folder, filename)
        file.save(save_path)

        # OCR
        ocr = extract_supplier_invoice_data(save_path, app.config.get("ANTHROPIC_API_KEY", ""))
        _log = logging.getLogger("supplier_invoice")
        if ocr.get("error"):
            _log.warning("OCR partial or failed for %s: %s", filename, ocr.get("error"))
        else:
            _log.info("OCR complete for %s — supplier=%s amount=%s", filename, ocr.get("supplier_name"), ocr.get("amount_incl_vat"))

        inv = SupplierInvoice(
            pdf_filename=filename,
            supplier_name=ocr.get("supplier_name"),
            supplier_org_nr=ocr.get("supplier_org_nr"),
            invoice_date=_safe_date(ocr.get("invoice_date")),
            due_date=_safe_date(ocr.get("due_date")),
            amount_excl_vat=_safe_float(ocr.get("amount_excl_vat")),
            vat_amount=_safe_float(ocr.get("vat_amount")),
            amount_incl_vat=_safe_float(ocr.get("amount_incl_vat")),
            currency=ocr.get("currency") or "SEK",
            payment_ref=ocr.get("payment_ref"),
            **_ocr_payment_account(ocr),
            ocr_raw=json.dumps(ocr),
            status="pending",
        )
        db.session.add(inv)
        db.session.commit()
        return redirect(url_for("supplier_invoices_review", invoice_id=inv.id))

    return render_template("supplier_invoices/upload.html")

@app.route("/supplier-invoices/<int:invoice_id>/review", methods=["GET", "POST"])
@login_required
def supplier_invoices_review(invoice_id):
    from models import SupplierCategory
    inv = SupplierInvoice.query.get_or_404(invoice_id)
    projects, internal_codes = _target_options(
        include_inactive_project=inv.project_id,
        include_inactive_code=inv.internal_code_id,
        for_cost=True)
    categories = SupplierCategory.query.filter_by(active=True).order_by(SupplierCategory.sort_order, SupplierCategory.name).all()
    currencies = _get_currencies()

    if request.method == "POST":
        action = request.form.get("action", "save")
        if action == "discard":
            fp = os.path.join(_supplier_upload_folder(), inv.pdf_filename)
            if os.path.exists(fp):
                os.remove(fp)
            db.session.delete(inv)
            db.session.commit()
            flash("Faktura kasserad / Invoice discarded", "info")
            return redirect(url_for("supplier_invoices_index"))

        inv.supplier_name = request.form.get("supplier_name", inv.supplier_name or "").strip()
        inv.supplier_org_nr = request.form.get("supplier_org_nr", "").strip()
        inv.invoice_date = _safe_date(request.form.get("invoice_date"))
        inv.due_date = _safe_date(request.form.get("due_date"))
        amount_excl = _safe_float(request.form.get("amount_excl_vat"))
        vat_amt     = _safe_float(request.form.get("vat_amount"))
        amount_incl = _safe_float(request.form.get("amount_incl_vat"))

        # Validate dates
        if not inv.invoice_date:
            flash("Fakturadatum krävs (YYYY-MM-DD) / Invoice date is required (YYYY-MM-DD)", "error")
            ocr = json.loads(inv.ocr_raw) if inv.ocr_raw else {}
            return render_template("supplier_invoices/review.html",
                inv=inv, projects=projects, internal_codes=internal_codes,
                categories=categories, ocr=ocr, currencies=currencies)
        if not inv.due_date:
            flash("Förfallodatum krävs (YYYY-MM-DD) / Due date is required (YYYY-MM-DD)", "error")
            ocr = json.loads(inv.ocr_raw) if inv.ocr_raw else {}
            return render_template("supplier_invoices/review.html",
                inv=inv, projects=projects, internal_codes=internal_codes,
                categories=categories, ocr=ocr, currencies=currencies)

        # Validate amounts
        if amount_incl <= 0:
            flash("Belopp inkl. moms måste vara större än noll / Amount incl. VAT must be greater than zero", "error")
            ocr = json.loads(inv.ocr_raw) if inv.ocr_raw else {}
            return render_template("supplier_invoices/review.html",
                inv=inv, projects=projects, internal_codes=internal_codes,
                categories=categories, ocr=ocr, currencies=currencies)
        if vat_amt < 0:
            flash("Momsbelopp kan inte vara negativt / VAT amount cannot be negative", "error")
            ocr = json.loads(inv.ocr_raw) if inv.ocr_raw else {}
            return render_template("supplier_invoices/review.html",
                inv=inv, projects=projects, internal_codes=internal_codes,
                categories=categories, ocr=ocr, currencies=currencies)
        vat_treatment = request.form.get("vat_treatment", "").strip()
        if vat_treatment not in VAT_TREATMENTS:
            # Older form posts (and the mobile flow) still send the plain checkbox.
            vat_treatment = (VAT_REVERSE_CHARGE_DOMESTIC
                             if request.form.get("reverse_charge") == "1" else VAT_DOMESTIC)
        reverse_charge = is_reverse_charge(vat_treatment)
        service_amount = _safe_float(request.form.get("service_amount_excl_vat"))
        if reverse_charge:
            # Omvänd skattskyldighet: the invoice carries no VAT, so net == total to pay.
            # The self-assessed VAT is virtual and must not inflate the payment.
            vat_amt = 0.0
            amount_excl = amount_incl
        if vat_treatment != VAT_REVERSE_CHARGE_EU_SERVICES:
            service_amount = 0.0
        elif service_amount < 0 or service_amount > amount_excl:
            ocr = json.loads(inv.ocr_raw) if inv.ocr_raw else {}
            flash(
                f"Service/reparation (12%) måste ligga mellan 0 och beloppet {amount_excl:.2f} / "
                f"Service/repair (12%) must be between 0 and the amount {amount_excl:.2f}",
                "error"
            )
            return render_template("supplier_invoices/review.html",
                inv=inv, projects=projects, internal_codes=internal_codes,
                categories=categories, ocr=ocr, currencies=currencies)
        expected_incl = round(amount_excl + vat_amt, 2)
        if abs(expected_incl - amount_incl) > 0.05:
            diff = round(abs(expected_incl - amount_incl), 2)
            flash(
                f"Beloppen stämmer inte: {amount_excl:.2f} (exkl.) + {vat_amt:.2f} (moms) = {expected_incl:.2f}, "
                f"men 'Att betala' är {amount_incl:.2f} (differens {diff:.2f}). Kontrollera beloppen mot fakturan. "
                f"/ Amount mismatch: {amount_excl:.2f} (excl.) + {vat_amt:.2f} (VAT) = {expected_incl:.2f}, "
                f"but 'Total to pay' is {amount_incl:.2f} (diff {diff:.2f}). Check the amounts against the invoice.",
                "error"
            )
            ocr = json.loads(inv.ocr_raw) if inv.ocr_raw else {}
            return render_template("supplier_invoices/review.html",
                inv=inv, projects=projects, internal_codes=internal_codes,
                categories=categories, ocr=ocr, currencies=currencies)

        payment_ref     = request.form.get("payment_ref", "").strip()
        payment_type    = request.form.get("payment_type", "bg")
        payment_account = request.form.get("payment_account", "").strip()
        if payment_type == "bg":
            # Accept '1218114' / '121 8114' as well as '121-8114'
            payment_account = _normalize_bankgiro(payment_account)

        def _re_render(msg):
            flash(msg, "error")
            ocr = json.loads(inv.ocr_raw) if inv.ocr_raw else {}
            return render_template("supplier_invoices/review.html",
                inv=inv, projects=projects, internal_codes=internal_codes,
                categories=categories, ocr=ocr, currencies=currencies)

        if not payment_ref:
            return _re_render(
                "OCR / betalningsreferens krävs / Payment reference is required"
            )
        if not payment_account:
            return _re_render(
                "Ange kontonummer (BG, PG eller IBAN) / "
                "Payment account (BG, PG or IBAN) is required"
            )
        if not _validate_payment_account(payment_type, payment_account):
            fmt = {"bg": "NNN-NNNN eller NNNN-NNNN", "pg": "NNNNNNN-N", "iban": "SE00NNNN…"}.get(payment_type, "")
            return _re_render(
                f"Ogiltigt kontoformat för {payment_type.upper()} (förväntat: {fmt}) / "
                f"Invalid {payment_type.upper()} format (expected: {fmt})"
            )

        inv.amount_excl_vat      = amount_excl
        inv.vat_amount           = vat_amt
        inv.amount_incl_vat      = amount_incl
        inv.currency             = request.form.get("currency", "SEK")
        inv.payment_ref          = payment_ref
        inv.payment_account      = payment_account
        inv.payment_account_type = payment_type
        inv.supplier_category_id = _safe_int(request.form.get("supplier_category_id")) or None
        inv.reverse_charge = reverse_charge
        inv.vat_treatment = vat_treatment
        inv.service_amount_excl_vat = service_amount
        inv.vat_rate = (REVERSE_CHARGE_VAT_RATE if reverse_charge
                        else _safe_float(request.form.get("vat_rate"), 25.0))
        inv.project_id, inv.internal_code_id = _parse_target()
        inv.status = "approved"
        db.session.commit()

        _audit("supplier_invoice_saved", f"invoice_id={invoice_id} supplier={inv.supplier_name} amount={inv.amount_incl_vat} {inv.currency}")
        _slog = logging.getLogger("supplier_invoice")
        _slog.info("Invoice saved — id=%s file=%s supplier=%s amount=%s %s ref=%s",
                   invoice_id, inv.pdf_filename, inv.supplier_name,
                   inv.amount_incl_vat, inv.currency, inv.payment_ref)
        flash("Faktura sparad / Invoice saved", "success")
        try:
            fortnox = FortnoxClient(app.config)
            voucher_ref = fortnox.create_supplier_invoice_voucher(inv)
            _slog.info("Fortnox voucher created — ref=%s file=%s", voucher_ref, inv.pdf_filename)
            flash(f"Fortnox verifikat skapat: {voucher_ref} / Voucher created: {voucher_ref}", "success")
            if inv.pdf_filename and voucher_ref:
                folder = _supplier_upload_folder()
                old_path = os.path.join(folder, inv.pdf_filename)
                # Strip random hex prefix, prepend voucher ref
                base = inv.pdf_filename
                if "_" in base and len(base.split("_")[0]) == 16:
                    base = base.split("_", 1)[1]
                base = base.replace("—", "-").replace("–", "-")
                new_filename = f"{voucher_ref} - {base}"
                new_path = os.path.join(folder, new_filename)
                if os.path.exists(old_path):
                    os.rename(old_path, new_path)
                    inv.pdf_filename = new_filename
                    db.session.commit()
                _email_pdf_to_fortnox_inbox(new_path, new_filename, voucher_ref, app.config)
        except Exception as e:
            logging.getLogger("supplier_invoice").error(
                "Fortnox voucher failed — id=%s file=%s error=%s", invoice_id, inv.pdf_filename, e,
                exc_info=True)
            flash(f"Fortnox-fel / Fortnox error: {e}", "warning")
            session["fortnox_preview"] = _build_supplier_voucher_preview(inv)
            session["fortnox_preview"]["back_url"] = url_for("supplier_invoices_index")
            session["fortnox_preview"]["back_label"] = "← Leverantörsfakturor / Supplier invoices"
            return redirect(url_for("fortnox_preview"))

        return redirect(url_for("supplier_invoices_index"))

    ocr = json.loads(inv.ocr_raw) if inv.ocr_raw else {}
    return render_template("supplier_invoices/review.html",
        inv=inv, projects=projects, internal_codes=internal_codes,
                categories=categories, ocr=ocr, currencies=currencies)

@app.route("/supplier-invoices/<int:invoice_id>/book", methods=["POST"])
@login_required
def supplier_invoices_book(invoice_id):
    """Manually trigger Fortnox booking for an already-approved invoice."""
    inv = SupplierInvoice.query.get_or_404(invoice_id)
    if inv.status not in ("approved", "pending"):
        flash("Fakturan är redan bokförd / Invoice already booked", "error")
        return redirect(url_for("supplier_invoices_index"))
    # Live booking commented out — show dry-run preview instead
    # try:
    #     _book_supplier_invoice_fortnox(inv)
    #     flash("Bokförd i Fortnox / Booked in Fortnox", "success")
    # except Exception as e:
    #     flash(f"Fortnox-fel / Fortnox error: {e}", "error")
    session["fortnox_preview"] = _build_supplier_voucher_preview(inv)
    session["fortnox_preview"]["back_url"] = url_for("supplier_invoices_index")
    session["fortnox_preview"]["back_label"] = "← Leverantörsfakturor / Supplier invoices"
    return redirect(url_for("fortnox_preview"))

@app.route("/supplier-invoices/<int:invoice_id>/mark-paid", methods=["POST"])
@login_required
def supplier_invoices_mark_paid(invoice_id):
    inv = SupplierInvoice.query.get_or_404(invoice_id)
    inv.status = "paid"
    db.session.commit()
    _audit("supplier_invoice_marked_paid", f"invoice_id={invoice_id} supplier={inv.supplier_name} amount={inv.amount_incl_vat}")
    flash("Markerad som betald / Marked as paid", "success")
    return redirect(url_for("supplier_invoices_index"))

@app.route("/supplier-invoices/<int:invoice_id>/delete", methods=["POST"])
@login_required
def supplier_invoices_delete(invoice_id):
    inv = SupplierInvoice.query.get_or_404(invoice_id)
    if inv.status in ("booked", "paid"):
        flash("Kan ej ta bort bokförd faktura / Cannot delete a booked invoice", "error")
        return redirect(url_for("supplier_invoices_index"))
    if inv.pdf_filename:
        fp = os.path.join(_supplier_upload_folder(), inv.pdf_filename)
        if os.path.exists(fp):
            os.remove(fp)
    db.session.delete(inv)
    db.session.commit()
    _audit("supplier_invoice_deleted", f"invoice_id={invoice_id} supplier={inv.supplier_name}")
    flash("Borttagen / Deleted", "success")
    return redirect(url_for("supplier_invoices_index"))

# ── Payment run ───────────────────────────────────────────────────────────────

@app.route("/supplier-invoices/payment-run", methods=["GET", "POST"])
@limiter.limit("5 per hour")
@login_required
def payment_run():
    backlog = SupplierInvoice.query.filter(
        SupplierInvoice.status == "booked",
        SupplierInvoice.payment_file_id == None,
    ).order_by(SupplierInvoice.due_date).all()
    # Payroll items only for admins — the lönebesked pages are admin-only too.
    payslip_backlog = agi_backlog = []
    if current_user.is_admin:
        payslip_backlog = Payslip.query.filter(
            Payslip.status == "approved", Payslip.payment_file_id == None,
        ).order_by(Payslip.pay_date).all()
        agi_backlog = AgiReport.query.filter(
            AgiReport.status == "approved", AgiReport.payment_file_id == None,
        ).order_by(AgiReport.skv_date).all()
    pending_files = PaymentFile.query.filter_by(status="generated").order_by(PaymentFile.created_at.desc()).all()
    if request.method == "POST":
        selected_ids = [_safe_int(i) for i in request.form.getlist("invoice_ids") if i]
        payslip_ids = [_safe_int(i) for i in request.form.getlist("payslip_ids") if i] if current_user.is_admin else []
        agi_ids = [_safe_int(i) for i in request.form.getlist("agi_ids") if i] if current_user.is_admin else []
        payment_date = _safe_date(request.form.get("payment_date")) or date.today()
        if not (selected_ids or payslip_ids or agi_ids):
            flash("Välj minst en faktura / Select at least one invoice", "error")
            return redirect(url_for("payment_run"))

        selected = SupplierInvoice.query.filter(
            SupplierInvoice.id.in_(selected_ids),
            SupplierInvoice.status == "booked",
            SupplierInvoice.payment_file_id == None,
        ).all() if selected_ids else []
        payslips = Payslip.query.filter(
            Payslip.id.in_(payslip_ids), Payslip.status == "approved", Payslip.payment_file_id == None,
        ).all() if payslip_ids else []
        reports = AgiReport.query.filter(
            AgiReport.id.in_(agi_ids), AgiReport.status == "approved", AgiReport.payment_file_id == None,
        ).all() if agi_ids else []
        if not (selected or payslips or reports):
            flash("Inga giltiga fakturor / No valid invoices", "error")
            return redirect(url_for("payment_run"))

        transfers = [_payslip_transfer(p) for p in payslips] + [_agi_transfer(r) for r in reports]
        past = [t for t in transfers if t.execution_date < date.today()]
        if past:
            flash("Betalningsdatum har passerat / Payment date is in the past: "
                  + ", ".join(f"{t.name} {t.execution_date}" for t in past), "error")
            return redirect(url_for("payment_run"))

        try:
            xml_bytes = generate_pain001(selected, payment_date, app.config, transfers=transfers)
        except Exception as e:
            flash(f"Kunde inte generera betalningsfil / Could not generate payment file: {e}", "error")
            return redirect(url_for("payment_run"))

        # Save file record — use the actual ReqdExctnDt from the pain.001 as payment_date.
        # With supplier invoices that is still their execution date (their voucher uses it);
        # a payroll-only file takes its earliest transfer date.
        if selected:
            execution_date = get_execution_date(selected)
        else:
            execution_date = min(t.execution_date for t in transfers)
        total = sum(i.amount_incl_vat for i in selected) + sum(t.amount for t in transfers)
        filename = f"betalning_{execution_date.isoformat()}_{secrets.token_hex(4)}.xml"
        pf = PaymentFile(
            filename=filename,
            payment_date=execution_date,
            total_amount=total,
            invoice_count=len(selected) + len(transfers),
            status="generated",
        )
        db.session.add(pf)
        db.session.flush()
        for inv in selected:
            inv.payment_file_id = pf.id
        for item in payslips + reports:
            item.payment_file_id = pf.id
        db.session.commit()
        _audit("payment_file_generated", f"pf_id={pf.id} file={filename} invoices={len(selected)} "
                                         f"payslips={len(payslips)} agi={len(reports)} total={total}")

        return send_file(
            io.BytesIO(xml_bytes),
            as_attachment=True,
            download_name=filename,
            mimetype="application/xml",
        )

    return render_template("supplier_invoices/payment_run.html", backlog=backlog, today=date.today(),
                           pending_files=pending_files, payslip_backlog=payslip_backlog,
                           agi_backlog=agi_backlog, net_pay=net_pay)

@app.route("/supplier-invoices/payment-files/<int:pf_id>/confirm", methods=["POST"])
@login_required
def payment_file_confirm(pf_id):
    pf = PaymentFile.query.get_or_404(pf_id)
    if (pf.payslips or pf.agi_reports) and not current_user.is_admin:
        abort(403)
    pf.status = "confirmed"
    pf.confirmed_at = datetime.utcnow()
    for inv in pf.supplier_invoices:
        inv.status = "paid"
    for item in pf.payslips + pf.agi_reports:
        item.status = "paid"
        item.paid_at = datetime.utcnow()
    db.session.commit()
    _audit("payment_file_confirmed", f"pf_id={pf_id} file={pf.filename} invoices={pf.invoice_count} total={pf.total_amount}")

    # Payroll vouchers — each dated on its own payment date.
    for p in pf.payslips:
        try:
            ref = _book_payslip(p)
            flash(f"Lön {p.employee.name} {p.period} bokförd, verifikat {ref} / "
                  f"Salary booked, voucher {ref}", "success")
        except Exception as e:
            flash(f"Lön {p.employee.name} {p.period}: Fortnox-bokföring misslyckades / "
                  f"Fortnox posting failed: {e}", "warning")
    for r in pf.agi_reports:
        try:
            for label, ref in _book_agi(r):
                flash(f"{label} {r.period_display} bokförd, verifikat {ref} / booked, voucher {ref}", "success")
        except Exception as e:
            flash(f"Skatt {r.period_display}: Fortnox-bokföring misslyckades / "
                  f"Fortnox posting failed: {e}", "warning")
    if not pf.supplier_invoices:
        return redirect(url_for("payroll_index"))

    # Post payment voucher: 2440 → 1930 for all invoices in this payment file
    # Voucher must be dated on the bank execution date, not the confirmation date.
    supplier_date = (get_execution_date(pf.supplier_invoices)
                     if (pf.payslips or pf.agi_reports) else pf.payment_date)
    if not supplier_date:
        flash("Betalningsfilen saknar betalningsdatum — Fortnox-bokföring hoppades över / "
              "Payment file has no payment date — Fortnox posting skipped", "warning")
        return redirect(url_for("supplier_invoices_index"))
    try:
        fortnox = FortnoxClient(app.config)
        invoice_ids = [inv.id for inv in pf.supplier_invoices]
        voucher_ref = fortnox.create_payment_voucher(invoice_ids, supplier_date)
        flash(
            f"Betalning bekräftad — {len(invoice_ids)} fakturor betalda, verifikat {voucher_ref} / "
            f"Payment confirmed — {len(invoice_ids)} invoices paid, voucher {voucher_ref}",
            "success",
        )
    except Exception as e:
        flash(
            f"Betalning bekräftad men Fortnox-bokföring misslyckades: {e} / "
            f"Payment confirmed but Fortnox posting failed: {e}",
            "warning",
        )

    return redirect(url_for("supplier_invoices_index"))

@app.route("/supplier-invoices/payment-files/<int:pf_id>/download")
@login_required
def payment_file_download(pf_id):
    pf = PaymentFile.query.get_or_404(pf_id)
    if (pf.payslips or pf.agi_reports) and not current_user.is_admin:
        abort(403)
    invs = [i for i in pf.supplier_invoices if i.payment_account]
    transfers = [_payslip_transfer(p) for p in pf.payslips] + [_agi_transfer(r) for r in pf.agi_reports]
    xml_bytes = generate_pain001(invs, pf.payment_date, app.config, transfers=transfers)
    return send_file(
        io.BytesIO(xml_bytes),
        as_attachment=True,
        download_name=pf.filename,
        mimetype="application/xml",
    )

def _build_outgoing_invoice_voucher_preview(inv: Invoice) -> dict:
    """
    Build the Fortnox voucher payload for an outgoing invoice (dry-run).
    Account structure:
      Debit  1510  Total incl VAT   (Kundfordringar)
      Credit 3001  Excl VAT amount  (Försäljning Sverige 25%)
      Credit 2610  VAT amount       (Utgående moms 25%)
      3740         Rounding         (if total != excl + vat)
    Financial year ID is resolved at runtime from Fortnox API; shown as
    '(looked up from /financialyears for invoice date)' in preview.
    """
    excl_vat = round(inv.subtotal, 2)
    vat      = round(inv.vat_amount, 2)
    total    = round(inv.total, 2)
    rounding = round(total - excl_vat - vat, 2)
    project_nr = (inv.project.project_number if inv.project else None) or ""
    description = f"Faktura {inv.invoice_number} {inv.client.name}"

    def _row(account, debit, credit, label, info=""):
        r = {
            "Account": str(account),
            "_label": label,
            "Debit": debit,
            "Credit": credit,
        }
        if info:
            r["TransactionInformation"] = info
        if project_nr:
            r["Project"] = project_nr
        return r

    rows = [
        _row(1510, total,    0,       "Kundfordringar (accounts receivable)", description),
        _row(3001, 0,        excl_vat, "Försäljning Sverige 25% moms",
             f"Period {inv.period_start} – {inv.period_end}"),
        _row(2610, 0,        vat,      "Utgående moms 25%", "Utgående moms 25%"),
    ]
    if rounding != 0:
        rows.append(_row(3740,
                         max(-rounding, 0),
                         max(rounding, 0),
                         "Öresavrundning (rounding)",
                         "Öresavrundning"))

    payload = {
        "_endpoint": "POST /vouchers",
        "_note": "Fortnox live sync disabled — dry-run preview",
        "_financial_year": "(looked up from GET /financialyears for invoice date)",
        "_pdf_attachment": inv.pdf_filename or "(not generated yet)",
        "Voucher": {
            "Description": description,
            "VoucherDate": inv.issue_date.isoformat() if inv.issue_date else None,
            "VoucherSeries": "A",
            "FinancialYear": "(int32 — resolved at runtime)",
            "VoucherRows": rows,
        },
    }
    return {"payload": payload}

def _build_supplier_voucher_preview(inv: SupplierInvoice) -> dict:
    """Build the Fortnox voucher payload that would be sent (for dry-run preview)."""
    description = f"{inv.supplier_name or 'Leverantör'} {inv.payment_ref or ''}".strip()
    rows = [
        {"Account": str(r["account"]), "Debit": r["debit"], "Credit": r["credit"],
         "_label": r["label"]}
        for r in supplier_invoice_rows(inv)
    ]

    payload = {
        "_endpoint": "POST /vouchers",
        "_note": "Fortnox live booking disabled — dry-run preview",
        "_vat_treatment": vat_treatment_of(inv),
        "_pdf_attachment": inv.pdf_filename or "(none)",
        "Voucher": {
            "Description": description,
            "TransactionDate": (inv.invoice_date or date.today()).isoformat(),
            "VoucherSeries": "A",
            "VoucherRows": rows,
        },
    }
    return {"payload": payload}

# def _book_supplier_invoice_fortnox(inv: SupplierInvoice):
#     """Create a supplier voucher in Fortnox and update invoice status to 'booked'."""
#     fortnox = FortnoxClient(app.config)
#     ... (commented out — enable when ready for live Fortnox sync)

# ── Fortnox dry-run preview ───────────────────────────────────────────────────

@app.route("/fortnox/test-archive")
@login_required
@admin_required
def fortnox_test_archive():
    """Dev route: try multiple archive upload methods and show raw responses."""
    from models import Invoice
    inv = Invoice.query.filter(Invoice.pdf_filename.isnot(None)).order_by(Invoice.id.desc()).first()
    if not inv:
        return "No invoice with PDF found.", 404
    pdf_path = os.path.join(app.root_path, "static", "uploads", inv.pdf_filename)
    if not os.path.exists(pdf_path):
        return f"PDF not found on disk: {pdf_path}", 404

    fortnox = FortnoxClient(app.config)
    token = Settings.get("fortnox_access_token")
    base = "https://api.fortnox.se/3"
    filename = inv.pdf_filename
    results = {}

    with open(pdf_path, "rb") as f:
        pdf_bytes = f.read()

    auth_header = {"Authorization": f"Bearer {token}"}

    # 1. GET /archive to see root structure
    r = requests.get(f"{base}/archive", headers={**auth_header, "Accept": "application/json"})
    results["1_GET_archive_root"] = {"status": r.status_code, "body": r.text[:800]}

    # 2. POST /archive — multipart, field name "file", no params
    r = requests.post(f"{base}/archive", headers=auth_header,
                      files={"file": (filename, pdf_bytes, "application/pdf")})
    results["2_POST_multipart_file"] = {"status": r.status_code, "body": r.text}

    # 3. POST /archive — multipart, field name "attachment"
    r = requests.post(f"{base}/archive", headers=auth_header,
                      files={"attachment": (filename, pdf_bytes, "application/pdf")})
    results["3_POST_multipart_attachment"] = {"status": r.status_code, "body": r.text}

    # 4. POST /archive?path=/Verifikat
    r = requests.post(f"{base}/archive", headers=auth_header,
                      params={"path": "/Verifikat"},
                      files={"file": (filename, pdf_bytes, "application/pdf")})
    results["4_POST_path_Verifikat"] = {"status": r.status_code, "body": r.text}

    # 5. POST /archive/root — direct to root folder id
    r = requests.post(f"{base}/archive/root", headers=auth_header,
                      files={"file": (filename, pdf_bytes, "application/pdf")})
    results["5_POST_archive_root_id"] = {"status": r.status_code, "body": r.text}

    # 6. GET /inbox to see if inbox is an alternative
    r = requests.get(f"{base}/inbox", headers={**auth_header, "Accept": "application/json"})
    results["6_GET_inbox"] = {"status": r.status_code, "body": r.text[:400]}

    return f"<pre>{json.dumps(results, indent=2, ensure_ascii=False)}</pre>"

@app.route("/fortnox/preview")
@login_required
def fortnox_preview():
    preview = session.pop("fortnox_preview", None)
    if not preview:
        return redirect(url_for("dashboard"))
    return render_template("fortnox/preview.html",
        payload_json=json.dumps(preview.get("payload", {}), indent=2, ensure_ascii=False),
        back_url=preview.get("back_url", url_for("dashboard")),
        back_label=preview.get("back_label", "← Back"),
    )

# ── Payroll (lönebesked) ──────────────────────────────────────────────────────
# create (copy) slip → register payment → payment file → acknowledge (salary voucher)
# AGI XML → acknowledge import → Skatteverket's amount + date → payment file →
# acknowledge (tax voucher). Calculations live in payroll.py.

PAYROLL_SETTING_KEYS = {
    # key: (default, Swedish label, English label)
    "payroll_account_bank":          ("1930", "Bankkonto (nettolön)", "Bank account (net pay)"),
    "payroll_account_ag_cost":       ("7510", "Arbetsgivaravgifter, kostnad", "Employer contributions, cost"),
    "payroll_account_ag_liability":  ("2731", "Avräkning sociala avgifter", "Employer contributions, liability"),
    "payroll_account_tax_liability": ("2710", "Personalskatt", "Employee tax liability"),
    "payroll_account_tax_payment":   ("1930", "Betalning till Skatteverket från", "Tax paid from account"),
    "payroll_account_tax_account":   ("1630", "Skattekonto", "Tax account"),
    "payroll_account_rounding":      ("3740", "Öresavrundning", "Rounding"),
    "payroll_voucher_series":        ("A", "Verifikationsserie", "Voucher series"),
    "payroll_skv_bankgiro":          (SKV_BANKGIRO, "Skatteverkets bankgiro", "Skatteverket bankgiro"),
    "payroll_skv_ocr":               ("", "OCR skattekonto", "Tax account OCR"),
    "payroll_agi_contact_name":      ("", "AGI kontaktperson", "AGI contact name"),
    "payroll_agi_contact_phone":     ("", "AGI telefon", "AGI contact phone"),
    "payroll_agi_contact_email":     ("", "AGI e-post", "AGI contact email"),
}


def _payroll_setting(key):
    return Settings.get(key) or PAYROLL_SETTING_KEYS[key][0]


def _payroll_accounts():
    return {k: _payroll_setting(f"payroll_account_{k}") for k in DEFAULT_PAYROLL_ACCOUNTS}


def _payroll_folder(sub):
    base = os.path.join(app.root_path, "instance", "uploads", sub)
    os.makedirs(base, exist_ok=True)
    return base


def _period_bounds(agi_period):
    """"202606" → (2026-06-01, 2026-06-30)."""
    y, m = int(agi_period[:4]), int(agi_period[4:6])
    return date(y, m, 1), date(y, m, calendar.monthrange(y, m)[1])


def _payslips_in_agi_period(agi_period, include_drafts=False):
    start, end = _period_bounds(agi_period)
    q = Payslip.query.filter(Payslip.pay_date >= start, Payslip.pay_date <= end,
                             Payslip.status != "historical")   # already declared outside onedesk
    if not include_drafts:
        q = q.filter(Payslip.status != "draft")
    return q.join(Employee).order_by(Employee.last_name, Employee.first_name, Payslip.spec_nr).all()


def _agi_report(agi_period, create=False):
    r = AgiReport.query.filter_by(period=agi_period).first()
    if r is None and create:
        r = AgiReport(period=agi_period, status="open")
        db.session.add(r)
        db.session.flush()
    return r


def _next_spec_nr(employee_id, pay_date, exclude_id=None):
    start = pay_date.replace(day=1)
    end = date(pay_date.year, pay_date.month, calendar.monthrange(pay_date.year, pay_date.month)[1])
    q = Payslip.query.filter(Payslip.employee_id == employee_id,
                             Payslip.pay_date >= start, Payslip.pay_date <= end)
    if exclude_id:
        q = q.filter(Payslip.id != exclude_id)
    used = {p.spec_nr for p in q.all()}
    n = 1
    while n in used:
        n += 1
    return n


def _payslip_ytd(payslip):
    """Calendar-year totals up to and including this slip (excluding other drafts)."""
    start = date(payslip.pay_date.year, 1, 1)
    slips = Payslip.query.filter(
        Payslip.employee_id == payslip.employee_id,
        Payslip.pay_date >= start,
        Payslip.pay_date <= payslip.pay_date,
        db.or_(Payslip.status != "draft", Payslip.id == payslip.id),
    ).all()
    out = {"gross": 0.0, "tax": 0.0, "benefits": 0.0}
    for p in slips:
        s = payslip_summary(p)
        out["gross"] += s["gross"]
        out["tax"] += s["tax"]
        out["benefits"] += s["benefits"]
    return out


def _payslip_transfer(p):
    emp = p.employee
    return Transfer(
        name=emp.name,
        amount=float(net_pay(p)),
        execution_date=p.pay_date,
        purpose="SALA",
        account_type="bban",
        account=emp.bank_account or "",
        clearing=emp.bank_clearing or "",
        reference=f"Lön {p.period}",
        end_to_end=f"LON-{p.id}",
    )


def _agi_transfer(r):
    return Transfer(
        name="Skatteverket",
        amount=float(r.skv_amount or 0),
        execution_date=r.skv_date,
        purpose="SUPP",
        account_type="bg",
        account=_payroll_setting("payroll_skv_bankgiro"),
        reference=r.skv_ocr or "",
        end_to_end=f"SKV-{r.period}",
    )


def _book_payslip(p):
    """Post the salary voucher. Returns the voucher ref; raises on Fortnox errors."""
    rows = salary_voucher_rows(p, _payroll_accounts())
    if not voucher_balanced(rows):
        raise Exception("Lönevoucher balanserar inte / Salary voucher does not balance")
    ref = FortnoxClient(app.config).create_voucher(
        rows, salary_voucher_description(p), p.pay_date, series=_payroll_setting("payroll_voucher_series"))
    p.voucher_ref = ref
    db.session.commit()
    _audit("payslip_booked", f"payslip_id={p.id} voucher={ref}")
    if ref:
        _send_payslip_to_fortnox_inbox(p, ref)
    return ref


def _send_payslip_to_fortnox_inbox(p, voucher_ref):
    """File the payslip in Fortnox's inbox, named after its voucher like other attachments.

    Never raises — the voucher is already booked; a missed PDF is logged and can be
    attached by hand.
    """
    try:
        name = f"{voucher_ref} - {_payslip_filename(p)}"
        path = os.path.join(_payroll_folder("payslips"), name)
        with open(path, "wb") as fh:
            fh.write(generate_payslip_pdf(p, app.config, ytd=_payslip_ytd(p)))
        _email_pdf_to_fortnox_inbox(path, name, voucher_ref, app.config)
    except Exception as e:
        app.logger.error("Payslip to Fortnox inbox failed — payslip=%s voucher=%s: %s", p.id, voucher_ref, e)


def _book_agi(r):
    """Post whichever of the two tax vouchers is still missing. Returns [(label, ref)].

    1. payment date: bank → skattekonto (1930 → 1630)
    2. tax date:     skattekonto → personalskatt and arbetsgivaravgifter (1630 → 2710/2731)
    Each is committed as soon as it is posted, so a failure on the second (e.g. the
    tax date falls in a financial year not yet opened in Fortnox) can be retried alone.
    """
    accounts = _payroll_accounts()
    series = _payroll_setting("payroll_voucher_series")
    booked = []
    if not r.voucher_ref:
        rows = tax_payment_rows(r.skv_amount, accounts)
        r.voucher_ref = FortnoxClient(app.config).create_voucher(
            rows, f"Inbetalning skattekonto {r.period_display}", r.skv_date, series=series)
        db.session.commit()
        _audit("agi_tax_payment_booked", f"period={r.period} voucher={r.voucher_ref}")
        booked.append(("Inbetalning skattekonto", r.voucher_ref))
    if not r.settle_voucher_ref:
        rows, _diff = tax_settlement_rows(_payslips_in_agi_period(r.period), r.skv_amount, accounts)
        if not voucher_balanced(rows):
            raise Exception("Skattevoucher balanserar inte / Tax voucher does not balance")
        r.settle_voucher_ref = FortnoxClient(app.config).create_voucher(
            rows, f"Skatt och arbetsgivaravgifter {r.period_display}",
            r.skv_debit_date or skv_due_date(r.period), series=series)
        db.session.commit()
        _audit("agi_tax_settlement_booked", f"period={r.period} voucher={r.settle_voucher_ref}")
        booked.append(("Skatt och arbetsgivaravgifter", r.settle_voucher_ref))
    return booked


def _decimal_input(value):
    """"1 149,50" → 1149.5; blank or garbage → None."""
    text = str(value or "").replace("\u00a0", "").replace(" ", "").replace(",", ".")
    try:
        return float(text) if text else None
    except ValueError:
        return None


def _payroll_lang(sv, en):
    return sv if g.get("lang", "sv") == "sv" else en


@app.route("/payroll")
@login_required
@admin_required
def payroll_index():
    payslips = (Payslip.query.options(joinedload(Payslip.employee))
                .order_by(Payslip.pay_date.desc(), Payslip.id).all())
    periods = OrderedDict()
    for p in payslips:
        periods.setdefault(p.agi_period, []).append(p)
    reports = {r.period: r for r in AgiReport.query.all()}
    summaries = {p.id: payslip_summary(p) for p in payslips}
    employees = Employee.query.filter_by(active=True).order_by(Employee.last_name, Employee.first_name).all()
    return render_template("payroll/index.html", periods=periods, reports=reports,
                           summaries=summaries, employees=employees)


# ── Employees ──

def _employee_from_form(emp):
    f = request.form
    emp.first_name = (f.get("first_name") or "").strip()
    emp.last_name = (f.get("last_name") or "").strip()
    emp.personnummer = normalize_personnummer(f.get("personnummer"))
    emp.address = (f.get("address") or "").strip()
    emp.postal_code = (f.get("postal_code") or "").strip()
    emp.city = (f.get("city") or "").strip()
    emp.email = (f.get("email") or "").strip()
    emp.bank_clearing = payroll_digits(f.get("bank_clearing"))
    emp.bank_account = payroll_digits(f.get("bank_account"))
    emp.salary_account = (f.get("salary_account") or "7210").strip()
    emp.ag_rate = _safe_float(f.get("ag_rate_pct"), 31.42) / 100
    emp.tax_table = (f.get("tax_table") or "").strip()
    emp.tax_column = _safe_int(f.get("tax_column")) if _safe_int(f.get("tax_column")) in TAX_COLUMNS else 1
    emp.active = f.get("active", "1") == "1"
    if not emp.first_name or not emp.last_name:
        raise ValueError(_payroll_lang("Namn krävs", "Name is required"))


@app.route("/payroll/employees", methods=["GET", "POST"])
@login_required
@admin_required
def payroll_employees():
    if request.method == "POST":
        emp = Employee()
        try:
            _employee_from_form(emp)
        except ValueError as e:
            flash(str(e), "error")
            return redirect(url_for("payroll_employees"))
        db.session.add(emp)
        db.session.commit()
        _audit("employee_create", f"employee_id={emp.id}")
        flash(_payroll_lang("Anställd skapad", "Employee created"), "success")
        return redirect(url_for("payroll_employees"))
    employees = Employee.query.order_by(Employee.active.desc(), Employee.last_name, Employee.first_name).all()
    return render_template("payroll/employees.html", employees=employees, edit=None, tax_columns=TAX_COLUMNS)


@app.route("/payroll/employees/<int:employee_id>", methods=["GET", "POST"])
@login_required
@admin_required
def payroll_employee_edit(employee_id):
    emp = Employee.query.get_or_404(employee_id)
    if request.method == "POST":
        try:
            _employee_from_form(emp)
        except ValueError as e:
            db.session.rollback()
            flash(str(e), "error")
            return redirect(url_for("payroll_employee_edit", employee_id=emp.id))
        db.session.commit()
        _audit("employee_update", f"employee_id={emp.id}")
        flash(_payroll_lang("Anställd sparad", "Employee saved"), "success")
        return redirect(url_for("payroll_employees"))
    employees = Employee.query.order_by(Employee.active.desc(), Employee.last_name, Employee.first_name).all()
    return render_template("payroll/employees.html", employees=employees, edit=emp, tax_columns=TAX_COLUMNS)


# ── Payslips ──

@app.route("/payroll/payslips/new", methods=["POST"])
@login_required
@admin_required
def payroll_payslip_new():
    emp = Employee.query.get_or_404(_safe_int(request.form.get("employee_id")))
    pay_date = _safe_date(request.form.get("pay_date")) or date.today().replace(day=25)
    p = Payslip(employee_id=emp.id, period=pay_date.strftime("%Y-%m"), pay_date=pay_date,
                spec_nr=_next_spec_nr(emp.id, pay_date), status="draft")
    db.session.add(p)
    db.session.commit()
    _audit("payslip_create", f"payslip_id={p.id} employee_id={emp.id}")
    return redirect(url_for("payroll_payslip", payslip_id=p.id))


def _copy_payslip(src):
    """New draft for the month after ``src``, same lines."""
    pay_date = add_month(src.pay_date)
    p = Payslip(employee_id=src.employee_id, period=next_period(src.period), pay_date=pay_date,
                spec_nr=_next_spec_nr(src.employee_id, pay_date), note=src.note, status="draft")
    for line in src.lines:
        if not line.salary_type.active:
            continue
        p.lines.append(PayslipLine(salary_type_id=line.salary_type_id, description=line.description,
                                   quantity=line.quantity, rate=line.rate, amount=line.amount,
                                   sort_order=line.sort_order))
    db.session.add(p)
    return p


@app.route("/payroll/payslips/<int:payslip_id>/copy", methods=["POST"])
@login_required
@admin_required
def payroll_payslip_copy(payslip_id):
    src = Payslip.query.get_or_404(payslip_id)
    p = _copy_payslip(src)
    db.session.commit()
    _audit("payslip_copy", f"from={src.id} to={p.id}")
    flash(_payroll_lang(f"Kopierat till {p.period}", f"Copied to {p.period}"), "success")
    return redirect(url_for("payroll_payslip", payslip_id=p.id))


@app.route("/payroll/copy-latest", methods=["POST"])
@login_required
@admin_required
def payroll_copy_latest():
    """Copy every active employee's latest lönebesked to the following month."""
    created = 0
    for emp in Employee.query.filter_by(active=True).all():
        latest = (Payslip.query.filter_by(employee_id=emp.id)
                  .order_by(Payslip.pay_date.desc(), Payslip.id.desc()).first())
        if latest:
            _copy_payslip(latest)
            created += 1
    db.session.commit()
    _audit("payslip_copy_latest", f"created={created}")
    flash(_payroll_lang(f"{created} lönebesked kopierade", f"{created} payslips copied"), "success")
    return redirect(url_for("payroll_index"))


def _tax_table_nr(employee):
    """The employee's table number as an int, or None when it isn't a plain table (e.g. jämkning)."""
    text = (employee.tax_table or "").strip()
    return int(text) if text.isdigit() else None


def _table_tax(year, table_nr, column, income):
    """(tax, row) from the stored table, or (None, None) when no bracket covers the income."""
    row = (TaxTableRow.query
           .filter(TaxTableRow.year == year, TaxTableRow.table_nr == table_nr,
                   TaxTableRow.income_from <= income)
           .order_by(TaxTableRow.income_from.desc()).first())
    if not row or (row.income_to is not None and income > row.income_to):
        return None, None
    return tax_from_row(row, income, column), row


def _payslip_tax_info(p):
    """What the table says for the saved payslip, for the hint beside the button."""
    table_nr = _tax_table_nr(p.employee)
    column = p.employee.tax_column if p.employee.tax_column in TAX_COLUMNS else 1
    info = {"table_nr": table_nr, "column": column, "year": p.pay_date.year,
            "loaded": False, "base": tax_base(p), "tax": None, "current": None}
    if table_nr is None:
        return info
    info["loaded"] = TaxTableRow.query.filter_by(year=info["year"], table_nr=table_nr).first() is not None
    if info["loaded"]:
        info["tax"] = _table_tax(info["year"], table_nr, column, info["base"])[0] if info["base"] > 0 else 0
    tax_lines = [l for l in p.lines if l.salary_type.kind == "tax"]
    if tax_lines:
        info["current"] = sum(l.amount or 0 for l in tax_lines)
    return info


def _recalc_payslip_tax(p):
    """Set the payslip's preliminary tax line from the tax table. Returns (ok, message)."""
    emp = p.employee
    table_nr = _tax_table_nr(emp)
    if table_nr is None:
        return False, _payroll_lang(
            "Sparat, men skatten räknades inte om: den anställde saknar skattetabell (ett tabellnummer, t.ex. 34).",
            "Saved, but the tax was not recalculated: the employee has no tax table number (e.g. 34).")
    year = p.pay_date.year
    column = emp.tax_column if emp.tax_column in TAX_COLUMNS else 1
    if not TaxTableRow.query.filter_by(year=year, table_nr=table_nr).first():
        return False, _payroll_lang(
            f"Sparat, men skatten räknades inte om: skattetabell {table_nr} för {year} är inte inläst. "
            "Hämta den under Inställningar → Lön → Skattetabeller.",
            f"Saved, but the tax was not recalculated: tax table {table_nr} for {year} is not loaded. "
            "Fetch it under Settings → Payroll → Tax tables.")
    base = tax_base(p)
    tax, _row = _table_tax(year, table_nr, column, base) if base > 0 else (0, None)
    if tax is None:
        return False, _payroll_lang(
            f"Sparat, men skatten räknades inte om: tabell {table_nr} saknar rad för {base} kr.",
            f"Saved, but the tax was not recalculated: table {table_nr} has no row for {base} kr.")
    tax_lines = [l for l in p.lines if l.salary_type.kind == "tax"]
    if tax_lines:
        line = tax_lines[0]
    else:
        st = (SalaryType.query.filter_by(kind="tax", active=True)
              .order_by(SalaryType.sort_order, SalaryType.code).first())
        if not st:
            return False, _payroll_lang("Sparat, men det finns ingen aktiv löneart för preliminärskatt.",
                                        "Saved, but there is no active salary type for preliminary tax.")
        line = PayslipLine(salary_type_id=st.id, salary_type=st, description=st.name,
                           sort_order=max([l.sort_order or 0 for l in p.lines] + [0]) + 1)
        p.lines.append(line)
    line.quantity, line.rate, line.amount = None, None, float(tax)
    db.session.commit()
    _audit("payslip_recalc_tax", f"payslip_id={p.id} table={table_nr} column={column} base={base} tax={tax}")
    extra = ""
    if len(tax_lines) > 1:
        extra = _payroll_lang(" Övriga skatterader är orörda.", " Other tax lines were left as they are.")
    return True, _payroll_lang(
        f"Preliminär skatt omräknad: {tax} kr (tabell {table_nr}, kolumn {column}, {year}, underlag {base} kr).",
        f"Preliminary tax recalculated: {tax} kr (table {table_nr}, column {column}, {year}, base {base} kr).") + extra


@app.route("/payroll/payslips/<int:payslip_id>", methods=["GET", "POST"])
@login_required
@admin_required
def payroll_payslip(payslip_id):
    p = Payslip.query.get_or_404(payslip_id)
    if request.method == "POST":
        if p.is_locked:
            flash(_payroll_lang("Lönebeskedet är låst", "Payslip is locked"), "error")
            return redirect(url_for("payroll_payslip", payslip_id=p.id))
        f = request.form
        p.period = (f.get("period") or p.period).strip()[:7]
        new_pay_date = _safe_date(f.get("pay_date")) or p.pay_date
        if new_pay_date.strftime("%Y%m") != p.pay_date.strftime("%Y%m"):
            p.spec_nr = _next_spec_nr(p.employee_id, new_pay_date, exclude_id=p.id)
        p.pay_date = new_pay_date
        p.note = (f.get("note") or "").strip()
        types = {st.id: st for st in SalaryType.query.all()}
        p.lines.clear()
        ids, descs = f.getlist("salary_type_id"), f.getlist("description")
        qtys, rates, amounts = f.getlist("quantity"), f.getlist("rate"), f.getlist("amount")
        for i, st_id in enumerate(ids):
            st = types.get(_safe_int(st_id))
            if not st:
                continue
            quantity = _decimal_input(qtys[i] if i < len(qtys) else "")
            rate = _decimal_input(rates[i] if i < len(rates) else "")
            amount = line_amount(quantity, rate, _decimal_input(amounts[i] if i < len(amounts) else "") or 0.0)
            if not amount and quantity is None:
                continue
            p.lines.append(PayslipLine(
                salary_type_id=st.id,
                description=(descs[i] if i < len(descs) else "").strip() or st.name,
                quantity=quantity, rate=rate, amount=amount, sort_order=i))
        db.session.commit()
        _audit("payslip_update", f"payslip_id={p.id}")
        if f.get("action") == "recalc_tax":
            ok, message = _recalc_payslip_tax(p)
            flash(message, "success" if ok else "error")
        else:
            flash(_payroll_lang("Sparat", "Saved"), "success")
        return redirect(url_for("payroll_payslip", payslip_id=p.id))

    summary = payslip_summary(p)
    salary_types = SalaryType.query.filter_by(active=True).order_by(SalaryType.sort_order, SalaryType.code).all()
    agi = _agi_report(p.agi_period)
    warnings = []
    if p.pay_date.weekday() >= 5 or p.pay_date in swedish_bank_holidays(p.pay_date.year):
        warnings.append(_payroll_lang("Utbetalningsdagen är inte en bankdag.",
                                      "The pay date is not a banking day."))
    if not p.employee.bank_account:
        warnings.append(_payroll_lang("Den anställde saknar bankkonto.", "The employee has no bank account."))
    for line in mileage_over_limit(p):
        warnings.append(_payroll_lang(
            f"”{line.description}”: över Skatteverkets skattefria "
            f"{SKV_MILEAGE_RATE_PER_KM:.2f}".replace(".", ",") + " kr/km (25 kr/mil). "
            "Överskjutande del är lön och ska redovisas som kontant ersättning.",
            f"“{line.description}”: above Skatteverket's tax-free {SKV_MILEAGE_RATE_PER_KM:.2f} kr/km "
            f"(25 kr/mil). The excess is taxable pay and belongs on a cash salary type."))
    rows = salary_voucher_rows(p, _payroll_accounts())
    salary_types_json = json.dumps({st.id: {"name": st.name, "rate": st.default_rate, "unit": st.unit}
                                    for st in salary_types}).replace("<", "\\u003c")
    return render_template("payroll/payslip.html", p=p, summary=summary, salary_types=salary_types,
                           salary_types_json=salary_types_json,
                           kinds=KINDS, agi=agi, warnings=warnings, voucher_rows=rows,
                           tax_info=_payslip_tax_info(p),
                           voucher_description=salary_voucher_description(p),
                           balanced=voucher_balanced(rows))


@app.route("/payroll/payslips/<int:payslip_id>/pdf")
@login_required
@admin_required
def payroll_payslip_pdf(payslip_id):
    p = Payslip.query.get_or_404(payslip_id)
    pdf = generate_payslip_pdf(p, app.config, ytd=_payslip_ytd(p))
    return send_file(io.BytesIO(pdf), mimetype="application/pdf",
                     as_attachment=request.args.get("download") == "1",
                     download_name=_payslip_filename(p))


def _payslip_filename(p):
    return f"Lonebesked_{secure_filename(p.employee.name)}_{p.period}.pdf"


@app.route("/payroll/payslips/<int:payslip_id>/email", methods=["POST"])
@login_required
@admin_required
def payroll_payslip_email(payslip_id):
    p = Payslip.query.get_or_404(payslip_id)
    to = _sanitize_header((request.form.get("to") or "").strip())
    if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", to):
        flash(_payroll_lang("Ogiltig e-postadress", "Invalid email address"), "error")
        return redirect(url_for("payroll_payslip", payslip_id=p.id))
    pdf = generate_payslip_pdf(p, app.config, ytd=_payslip_ytd(p))
    filename = _payslip_filename(p)
    with open(os.path.join(_payroll_folder("payslips"), f"{p.id}_{filename}"), "wb") as fh:
        fh.write(pdf)
    try:
        _send_payslip_email(p, to, pdf, filename, app.config)
    except Exception as e:
        app.logger.error("Payslip email failed: %s", e)
        flash(_payroll_lang(f"Kunde inte skicka: {e}", f"Could not send: {e}"), "error")
        return redirect(url_for("payroll_payslip", payslip_id=p.id))
    p.emailed_to = to
    p.emailed_at = datetime.utcnow()
    db.session.commit()
    _audit("payslip_emailed", f"payslip_id={p.id} to={to}")
    flash(_payroll_lang(f"Lönebesked skickat till {to}", f"Payslip sent to {to}"), "success")
    return redirect(url_for("payroll_payslip", payslip_id=p.id))


def _send_payslip_email(p, to, pdf_bytes, filename, config):
    company = _sanitize_header(config["COMPANY_NAME"])
    msg = MIMEMultipart()
    msg["From"] = _sanitize_header(config["SMTP_FROM"])
    msg["To"] = to
    msg["Subject"] = f"Lönebesked {p.period} – {company}"
    msg.attach(MIMEText(
        f"Hej {p.employee.first_name},\n\nBifogat finner du ditt lönebesked för {p.period} "
        f"med utbetalning {p.pay_date.isoformat()}.\n\nMed vänliga hälsningar,\n{company}\n",
        "plain", "utf-8"))
    part = MIMEBase("application", "pdf")
    part.set_payload(pdf_bytes)
    encoders.encode_base64(part)
    part.add_header("Content-Disposition", f'attachment; filename="{filename}"')
    msg.attach(part)
    with smtplib.SMTP(config["SMTP_HOST"], config["SMTP_PORT"]) as server:
        server.starttls(context=ssl.create_default_context())
        server.login(config["SMTP_USER"], config["SMTP_PASSWORD"])
        server.sendmail(msg["From"], [to], msg.as_string())


@app.route("/payroll/payslips/<int:payslip_id>/approve", methods=["POST"])
@login_required
@admin_required
def payroll_payslip_approve(payslip_id):
    """Register for payment: locks the slip and puts it in the payment run."""
    p = Payslip.query.get_or_404(payslip_id)
    back = redirect(url_for("payroll_payslip", payslip_id=p.id))
    if p.status != "draft":
        return back
    if not p.lines:
        flash(_payroll_lang("Lönebeskedet har inga rader", "The payslip has no lines"), "error")
        return back
    if not voucher_balanced(salary_voucher_rows(p, _payroll_accounts())):
        flash(_payroll_lang("Konteringen balanserar inte", "The voucher does not balance"), "error")
        return back
    if net_pay(p) <= 0:
        flash(_payroll_lang("Nettolönen måste vara positiv", "Net pay must be positive"), "error")
        return back
    if not (p.employee.bank_clearing and p.employee.bank_account):
        flash(_payroll_lang("Den anställde saknar clearing- och kontonummer",
                            "The employee has no clearing and account number"), "error")
        return back
    agi = _agi_report(p.agi_period)
    if agi and agi.status not in ("open", "generated"):
        flash(_payroll_lang(f"AGI för {agi.period_display} är redan inlämnad",
                            f"The AGI for {agi.period_display} is already submitted"), "error")
        return back
    p.status = "approved"
    db.session.commit()
    _audit("payslip_approved", f"payslip_id={p.id} net={net_pay(p)}")
    flash(_payroll_lang("Registrerad för betalning — finns nu i betalningsunderlaget",
                        "Registered for payment — now in the payment run"), "success")
    return back


@app.route("/payroll/payslips/<int:payslip_id>/reopen", methods=["POST"])
@login_required
@admin_required
def payroll_payslip_reopen(payslip_id):
    p = Payslip.query.get_or_404(payslip_id)
    agi = _agi_report(p.agi_period)
    if p.status != "approved" or p.payment_file_id:
        flash(_payroll_lang("Kan inte låsas upp — redan i en betalningsfil",
                            "Cannot reopen — already in a payment file"), "error")
    elif agi and agi.status not in ("open", "generated"):
        flash(_payroll_lang("Kan inte låsas upp — AGI är inlämnad",
                            "Cannot reopen — AGI already submitted"), "error")
    else:
        p.status = "draft"
        if agi and agi.status == "generated":
            agi.status = "open"   # the generated file no longer matches
        db.session.commit()
        _audit("payslip_reopened", f"payslip_id={p.id}")
    return redirect(url_for("payroll_payslip", payslip_id=p.id))


@app.route("/payroll/payslips/<int:payslip_id>/historical", methods=["POST"])
@login_required
@admin_required
def payroll_payslip_historical(payslip_id):
    """Mark a sideloaded slip as already paid, booked and declared outside onedesk (or undo that).

    Historical slips count towards the year-to-date totals and serve as copy sources,
    but never reach a payment file, a Fortnox voucher or an AGI file.
    """
    p = Payslip.query.get_or_404(payslip_id)
    if p.status == "draft":
        if not p.lines:
            flash(_payroll_lang("Lönebeskedet har inga rader", "The payslip has no lines"), "error")
        else:
            p.status = "historical"
            db.session.commit()
            _audit("payslip_historical", f"payslip_id={p.id}")
            flash(_payroll_lang("Markerad som historisk — räknas i ackumulerat, ingen betalning, bokföring eller AGI",
                                "Marked historical — counts towards year-to-date, no payment, voucher or AGI"),
                  "success")
    elif p.status == "historical":
        p.status = "draft"
        db.session.commit()
        _audit("payslip_historical_undone", f"payslip_id={p.id}")
    return redirect(url_for("payroll_payslip", payslip_id=p.id))


@app.route("/payroll/payslips/<int:payslip_id>/delete", methods=["POST"])
@login_required
@admin_required
def payroll_payslip_delete(payslip_id):
    p = Payslip.query.get_or_404(payslip_id)
    if p.status != "draft":
        flash(_payroll_lang("Endast utkast kan tas bort", "Only drafts can be deleted"), "error")
        return redirect(url_for("payroll_payslip", payslip_id=p.id))
    db.session.delete(p)
    db.session.commit()
    _audit("payslip_deleted", f"payslip_id={payslip_id}")
    flash(_payroll_lang("Utkast borttaget", "Draft deleted"), "success")
    return redirect(url_for("payroll_index"))


@app.route("/payroll/payslips/<int:payslip_id>/book", methods=["POST"])
@login_required
@admin_required
def payroll_payslip_book(payslip_id):
    """Retry the salary voucher after a failed Fortnox posting."""
    p = Payslip.query.get_or_404(payslip_id)
    if p.status == "paid" and not p.voucher_ref:
        try:
            ref = _book_payslip(p)
            flash(_payroll_lang(f"Bokförd, verifikat {ref}", f"Booked, voucher {ref}"), "success")
        except Exception as e:
            flash(f"Fortnox: {e}", "error")
    return redirect(url_for("payroll_payslip", payslip_id=p.id))


# ── AGI ──

def _agi_view(period):
    if not re.fullmatch(r"\d{6}", period or ""):
        abort(404)
    payslips = _payslips_in_agi_period(period, include_drafts=True)
    final = [p for p in payslips if p.status != "draft"]
    return payslips, final


@app.route("/payroll/agi/<period>")
@login_required
@admin_required
def payroll_agi(period):
    payslips, final = _agi_view(period)
    report = _agi_report(period)
    totals = agi_totals(final)
    values = {p.id: payslip_agi_values(p) for p in payslips}
    codes = sorted({c for v in values.values() for c in v})
    fields = {f.code: f for f in AgiFieldCode.query.filter(AgiFieldCode.code.in_(codes)).all()} if codes else {}
    payment_rows = settle_rows = diff = None
    if report and report.skv_amount:
        payment_rows = tax_payment_rows(report.skv_amount, _payroll_accounts())
        settle_rows, diff = tax_settlement_rows(final, report.skv_amount, _payroll_accounts())
    return render_template("payroll/agi.html", period=period,
                           period_display=f"{period[:4]}-{period[4:]}",
                           payslips=payslips, final=final, report=report, totals=totals,
                           values=values, codes=codes, fields=fields,
                           drafts=[p for p in payslips if p.status == "draft"],
                           payment_rows=payment_rows, settle_rows=settle_rows, diff=diff,
                           tolerance=ROUNDING_TOLERANCE, default_debit_date=skv_due_date(period),
                           default_ocr=_payroll_setting("payroll_skv_ocr"),
                           default_date=skv_due_date(period))


@app.route("/payroll/agi/<period>/xml", methods=["POST"])
@login_required
@admin_required
def payroll_agi_xml(period):
    payslips, final = _agi_view(period)
    back = redirect(url_for("payroll_agi", period=period))
    if len(final) != len(payslips):
        flash(_payroll_lang("Det finns utkast i perioden — registrera eller ta bort dem först",
                            "There are drafts in the period — approve or delete them first"), "error")
        return back
    if not final:
        flash(_payroll_lang("Inga lönebesked i perioden", "No payslips in the period"), "error")
        return back
    report = _agi_report(period, create=True)
    if report.status not in ("open", "generated"):
        # Already submitted: hand back exactly what was filed.
        path = os.path.join(_payroll_folder("agi"), report.xml_filename or "")
        if report.xml_filename and os.path.exists(path):
            return send_file(path, as_attachment=True, download_name=report.xml_filename,
                             mimetype="application/xml")
        flash(_payroll_lang("Filen saknas", "File missing"), "error")
        return back
    contact = {k: _payroll_setting(f"payroll_agi_contact_{k}") for k in ("name", "phone", "email")}
    if not all(contact.values()) or not app.config.get("COMPANY_ORG_NR"):
        flash(_payroll_lang("Fyll i AGI-kontaktperson (namn, telefon, e-post) under Löneinställningar och COMPANY_ORG_NR",
                            "Fill in the AGI contact (name, phone, email) in payroll settings, and COMPANY_ORG_NR"), "error")
        return back
    fields = {f.code: f for f in AgiFieldCode.query.all()}
    try:
        xml = build_agi_xml(period, final, fields,
                            {"org_nr": app.config["COMPANY_ORG_NR"], "program": "onedesk"}, contact)
    except ValueError as e:
        flash(str(e), "error")
        return back
    totals = agi_totals(final)
    report.xml_filename = f"AGI_{period}.xml"
    with open(os.path.join(_payroll_folder("agi"), report.xml_filename), "wb") as fh:
        fh.write(xml)
    report.status = "generated"
    report.generated_at = datetime.utcnow()
    report.sum_ag_fees = totals["ag_fees"]
    report.sum_tax = totals["tax"]
    db.session.commit()
    _audit("agi_generated", f"period={period} ag_fees={totals['ag_fees']} tax={totals['tax']}")
    return send_file(io.BytesIO(xml), as_attachment=True, download_name=report.xml_filename,
                     mimetype="application/xml")


@app.route("/payroll/agi/<period>/submitted", methods=["POST"])
@login_required
@admin_required
def payroll_agi_submitted(period):
    report = _agi_report(period)
    if report and report.status == "generated":
        report.status = "submitted"
        report.submitted_at = datetime.utcnow()
        db.session.commit()
        _audit("agi_submitted", f"period={period}")
        flash(_payroll_lang("Import kvitterad", "Import acknowledged"), "success")
    return redirect(url_for("payroll_agi", period=period))


@app.route("/payroll/agi/<period>/assess", methods=["POST"])
@login_required
@admin_required
def payroll_agi_assess(period):
    """Skatteverket's amount and date → registered for payment."""
    report = _agi_report(period)
    back = redirect(url_for("payroll_agi", period=period))
    if not report or report.status not in ("submitted", "approved") or report.payment_file_id:
        return back
    amount = _safe_float(request.form.get("skv_amount"), 0.0)
    skv_date = _safe_date(request.form.get("skv_date"))
    debit_date = _safe_date(request.form.get("skv_debit_date")) or skv_due_date(period)
    ocr = payroll_digits(request.form.get("skv_ocr"))
    if amount <= 0 or not skv_date:
        flash(_payroll_lang("Belopp och datum krävs", "Amount and date are required"), "error")
        return back
    if not ocr:
        flash(_payroll_lang("OCR-nummer krävs", "OCR number is required"), "error")
        return back
    report.skv_amount = round(amount, 2)
    report.skv_date = skv_date
    report.skv_debit_date = debit_date
    report.skv_ocr = ocr
    report.status = "approved"
    db.session.commit()
    expected = (report.sum_ag_fees or 0) + (report.sum_tax or 0)
    _audit("agi_assessed", f"period={period} amount={amount} date={skv_date}")
    if abs(amount - expected) > ROUNDING_TOLERANCE:
        flash(_payroll_lang(
            f"Obs: beloppet avviker från AGI ({expected:,.0f} kr). Skillnaden bokförs mot 2731.".replace(",", " "),
            f"Note: the amount differs from the AGI ({expected:,.0f} kr). The difference is booked to 2731."), "warning")
    else:
        flash(_payroll_lang("Registrerad för betalning", "Registered for payment"), "success")
    return back


@app.route("/payroll/agi/<period>/book", methods=["POST"])
@login_required
@admin_required
def payroll_agi_book(period):
    report = _agi_report(period)
    if report and report.status == "paid" and not (report.voucher_ref and report.settle_voucher_ref):
        try:
            for label, ref in _book_agi(report):
                flash(_payroll_lang(f"{label}: verifikat {ref}", f"{label}: voucher {ref}"), "success")
        except Exception as e:
            flash(f"Fortnox: {e}", "error")
    return redirect(url_for("payroll_agi", period=period))


# ── Payroll settings, lönearter, redovisningstyper ──

@app.route("/settings/payroll", methods=["GET", "POST"])
@login_required
@admin_required
def payroll_settings():
    if request.method == "POST":
        for key in PAYROLL_SETTING_KEYS:
            Settings.set(key, (request.form.get(key) or "").strip())
        _audit("payroll_settings_update")
        flash(_payroll_lang("Sparat", "Saved"), "success")
        return redirect(url_for("payroll_settings"))
    values = {k: _payroll_setting(k) for k in PAYROLL_SETTING_KEYS}
    return render_template("settings/payroll.html", keys=PAYROLL_SETTING_KEYS, values=values)


@app.route("/settings/tax-tables", methods=["GET", "POST"])
@login_required
@admin_required
def tax_tables():
    if request.method == "POST":
        f = request.form
        year, table_nr = _safe_int(f.get("year")), _safe_int(f.get("table_nr"))
        if not (2000 <= (year or 0) <= 2100) or not (1 <= (table_nr or 0) <= 99):
            flash(_payroll_lang("Ange år och tabellnummer", "Enter a year and a table number"), "error")
            return redirect(url_for("tax_tables"))
        if f.get("action") == "fetch":
            try:
                rows = fetch_skv_tax_table(year, table_nr)
            except Exception as exc:
                app.logger.warning("Tax table fetch failed: %s", exc)
                flash(_payroll_lang("Kunde inte hämta tabellen från Skatteverket. Försök igen senare.",
                                    "Could not fetch the table from Skatteverket. Try again later."), "error")
                return redirect(url_for("tax_tables"))
            if not rows:
                flash(_payroll_lang(f"Skatteverket har ingen tabell {table_nr} för {year}.",
                                    f"Skatteverket has no table {table_nr} for {year}."), "error")
                return redirect(url_for("tax_tables"))
            TaxTableRow.query.filter_by(year=year, table_nr=table_nr).delete()
            db.session.add_all([TaxTableRow(year=year, table_nr=table_nr, **row) for row in rows])
            db.session.commit()
            _audit("tax_table_fetch", f"{year}/{table_nr} rows={len(rows)}")
            flash(_payroll_lang(f"Tabell {table_nr} för {year} inläst ({len(rows)} rader).",
                                f"Table {table_nr} for {year} loaded ({len(rows)} rows)."), "success")
            return redirect(url_for("tax_tables", year=year, table_nr=table_nr))
        if f.get("action") == "delete":
            TaxTableRow.query.filter_by(year=year, table_nr=table_nr).delete()
            db.session.commit()
            _audit("tax_table_delete", f"{year}/{table_nr}")
        return redirect(url_for("tax_tables"))

    loaded = (db.session.query(TaxTableRow.year, TaxTableRow.table_nr,
                               db.func.count(TaxTableRow.id), db.func.max(TaxTableRow.fetched_at))
              .group_by(TaxTableRow.year, TaxTableRow.table_nr)
              .order_by(TaxTableRow.year.desc(), TaxTableRow.table_nr).all())
    year, table_nr = _safe_int(request.args.get("year")), _safe_int(request.args.get("table_nr"))
    rows, lookup = [], None
    if year and table_nr:
        rows = (TaxTableRow.query.filter_by(year=year, table_nr=table_nr)
                .order_by(TaxTableRow.income_from).all())
        income = _safe_int(_decimal_input(request.args.get("income")))
        if rows and income:
            lookup = {"income": income,
                      "taxes": [_table_tax(year, table_nr, c, income)[0] for c in TAX_COLUMNS]}
    used = sorted({n for n in (_tax_table_nr(e) for e in Employee.query.filter_by(active=True)) if n})
    return render_template("settings/tax_tables.html", loaded=loaded, rows=rows, lookup=lookup,
                           year=year, table_nr=table_nr, used=used, columns=TAX_COLUMNS,
                           default_year=date.today().year)


@app.route("/settings/salary-types", methods=["GET", "POST"])
@login_required
@admin_required
def salary_types():
    if request.method == "POST":
        f = request.form
        action = f.get("action")
        st = SalaryType.query.get(_safe_int(f.get("st_id"))) if f.get("st_id") else None
        if action in ("new", "save"):
            code = (f.get("code") or "").strip()
            name = (f.get("name") or "").strip()
            if not code or not name or f.get("kind") not in KINDS:
                flash(_payroll_lang("Kod, namn och typ krävs", "Code, name and kind are required"), "error")
                return redirect(url_for("salary_types"))
            clash = SalaryType.query.filter(SalaryType.code == code,
                                            SalaryType.id != (st.id if st else 0)).first()
            if clash:
                flash(_payroll_lang(f"Löneart {code} finns redan", f"Salary type {code} already exists"), "error")
                return redirect(url_for("salary_types"))
            if action == "new":
                st = SalaryType(active=True)
                db.session.add(st)
            if st:
                st.code = code
                st.name = name
                st.kind = f.get("kind")
                st.agi_field_id = _safe_int(f.get("agi_field_id")) or None
                st.debit_account = (f.get("debit_account") or "").strip()
                st.credit_account = (f.get("credit_account") or "").strip()
                st.default_rate = _safe_float(f.get("default_rate"), 0.0) or None
                st.unit = (f.get("unit") or "").strip()
                st.sort_order = _safe_int(f.get("sort_order"))
                db.session.commit()
                _audit(f"salary_type_{action}", code)
                flash(_payroll_lang("Löneart sparad", "Salary type saved"), "success")
        elif action in ("activate", "deactivate") and st:
            st.active = action == "activate"
            db.session.commit()
        elif action == "delete" and st:
            if PayslipLine.query.filter_by(salary_type_id=st.id).count():
                flash(_payroll_lang("Används — inaktivera i stället", "In use — deactivate instead"), "error")
            else:
                db.session.delete(st)
                db.session.commit()
                _audit("salary_type_delete", st.code)
        return redirect(url_for("salary_types"))
    types = SalaryType.query.order_by(SalaryType.sort_order, SalaryType.code).all()
    usage = {st.id: PayslipLine.query.filter_by(salary_type_id=st.id).count() for st in types}
    fields = (AgiFieldCode.query.filter_by(selectable=True, active=True)
              .order_by(AgiFieldCode.code).all())
    return render_template("settings/salary_types.html", types=types, usage=usage,
                           kinds=KINDS, fields=fields)


@app.route("/settings/agi-codes", methods=["GET", "POST"])
@login_required
@admin_required
def agi_codes():
    if request.method == "POST":
        f = request.form
        action = f.get("action")
        if action == "new":
            code = payroll_digits(f.get("code")).zfill(3)
            element = (f.get("element") or "").strip()
            if not re.fullmatch(r"\d{3}", code) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9]*", element):
                flash(_payroll_lang("Ogiltig fältkod eller elementnamn", "Invalid field code or element"), "error")
            elif AgiFieldCode.query.filter_by(code=code).first():
                flash(_payroll_lang(f"Fältkod {code} finns redan", f"Field {code} already exists"), "error")
            else:
                vt = f.get("value_type") if f.get("value_type") in ("amount", "checkbox", "text") else "amount"
                db.session.add(AgiFieldCode(code=code, element=element, value_type=vt,
                                            description=(f.get("description") or "").strip(),
                                            selectable=True, ag_base=f.get("ag_base") == "1", active=True))
                db.session.commit()
                _audit("agi_code_create", code)
        else:
            fc = AgiFieldCode.query.get(_safe_int(f.get("field_id")))
            if fc and action == "save":
                fc.description = (f.get("description") or "").strip()
                fc.selectable = f.get("selectable") == "1"
                fc.ag_base = f.get("ag_base") == "1"
                fc.active = f.get("active") == "1"
                db.session.commit()
                _audit("agi_code_update", fc.code)
        return redirect(url_for("agi_codes"))
    codes = AgiFieldCode.query.order_by(AgiFieldCode.code).all()
    used = {st.agi_field_id for st in SalaryType.query.all()}
    return render_template("settings/agi_codes.html", codes=codes, used=used)


def _seed_payroll():
    """Seed Skatteverket's field codes and a starting set of lönearter (only when empty)."""
    if not AgiFieldCode.query.first():
        for code, element, vt, desc in AGI_FIELD_SEED:
            db.session.add(AgiFieldCode(
                code=code, element=element, value_type=vt, description=desc,
                selectable=agi_field_selectable(code, vt),
                ag_base=code in AGI_AG_BASE_CODES, active=True))
        db.session.commit()
    if not SalaryType.query.first():
        fields = {f.code: f.id for f in AgiFieldCode.query.all()}
        for order, (code, name, kind, agi, debit, credit, rate, unit) in enumerate(SALARY_TYPE_SEED, start=1):
            db.session.add(SalaryType(code=code, name=name, kind=kind, agi_field_id=fields.get(agi),
                                      debit_account=debit, credit_account=credit, default_rate=rate,
                                      unit=unit, sort_order=order * 10, active=True))
        db.session.commit()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _sanitize_header(value):
    """Strip newlines to prevent email header injection."""
    return str(value or "").replace("\r", "").replace("\n", "")


def _email_pdf_to_fortnox_inbox(pdf_path, filename, voucher_ref, config):
    """Email a PDF to the Fortnox arkivplats inbox address."""
    inbox = config.get("FORTNOX_INBOX_EMAIL", "")
    if not inbox:
        app.logger.info("Fortnox inbox email not configured — skipping PDF email")
        return
    if not os.path.exists(pdf_path):
        app.logger.warning("Fortnox inbox email — PDF not found: %s", pdf_path)
        return
    # Use filename as-is — callers are responsible for naming the attachment correctly.
    attachment_name = os.path.basename(filename)
    msg = MIMEMultipart()
    msg["From"] = _sanitize_header(config["SMTP_FROM"])
    msg["To"] = inbox
    msg["Subject"] = f"Verifikat {voucher_ref} — {attachment_name}"
    msg.attach(MIMEText(f"Verifikat {voucher_ref}", "plain", "utf-8"))
    with open(pdf_path, "rb") as f:
        part = MIMEBase("application", "octet-stream")
        part.set_payload(f.read())
        encoders.encode_base64(part)
        part.add_header("Content-Disposition", f'attachment; filename="{attachment_name}"')
        msg.attach(part)
    try:
        with smtplib.SMTP(config["SMTP_HOST"], config["SMTP_PORT"]) as server:
            server.starttls(context=ssl.create_default_context())
            server.login(config["SMTP_USER"], config["SMTP_PASSWORD"])
            server.send_message(msg)
        app.logger.info("Fortnox inbox email sent — voucher=%s to=%s", voucher_ref, inbox)
    except Exception as e:
        app.logger.error("Fortnox inbox email failed: %s", e)


def _send_invoice_email(inv, config):
    client = inv.client
    lang = inv.language

    safe_company = _sanitize_header(config['COMPANY_NAME'])
    safe_inv_nr  = _sanitize_header(inv.invoice_number)
    safe_to      = _sanitize_header(client.contact_email)
    safe_from    = _sanitize_header(config["SMTP_FROM"])
    contact_person = (inv.project and inv.project.contact_person_name) or client.contact_name or client.name
    safe_name    = _sanitize_header(contact_person)

    subject_sv = f"Faktura {safe_inv_nr} – {safe_company}"
    subject_en = f"Invoice {safe_inv_nr} – {safe_company}"
    subject = subject_sv if lang == "sv" else subject_en

    body_sv = f"""Hej {safe_name},

Bifogat finner du faktura {inv.invoice_number} avseende perioden {inv.period_start} – {inv.period_end}.

Belopp: {inv.total:,.0f} SEK inkl. moms
Förfallodatum: {inv.due_date}
Bankgiro: {config['COMPANY_BANKGIRO']}

Med vänliga hälsningar,
{config['COMPANY_NAME']}
"""
    body_en = f"""Hi {safe_name},

Please find attached invoice {safe_inv_nr} for the period {inv.period_start} – {inv.period_end}.

Amount: {inv.total:,.0f} SEK incl. VAT
Due date: {inv.due_date}
Bankgiro: {config['COMPANY_BANKGIRO']}

Best regards,
{safe_company}
"""
    body = body_sv if lang == "sv" else body_en

    # Build CC list: always-CC + project-level CC + admin email
    cc_addresses = []
    always_cc = config.get("COMPANY_CC_MAIL_ON_INVOICING", "")
    if always_cc and always_cc != safe_to:
        cc_addresses.append(_sanitize_header(always_cc))
    if inv.project and inv.project.invoice_cc_email:
        proj_cc = _sanitize_header(inv.project.invoice_cc_email)
        if proj_cc not in cc_addresses:
            cc_addresses.append(proj_cc)
    admin_email = config.get("ADMIN_EMAIL", "")
    if admin_email and admin_email != safe_to and _sanitize_header(admin_email) not in cc_addresses:
        cc_addresses.append(_sanitize_header(admin_email))

    msg = MIMEMultipart()
    msg["From"] = safe_from
    msg["To"] = safe_to
    if cc_addresses:
        msg["Cc"] = ", ".join(cc_addresses)
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain", "utf-8"))

    # Attach PDF
    if inv.pdf_filename:
        pdf_path = os.path.join(os.path.dirname(__file__), "static", "uploads", inv.pdf_filename)
        if os.path.exists(pdf_path):
            with open(pdf_path, "rb") as f:
                part = MIMEBase("application", "octet-stream")
                part.set_payload(f.read())
                encoders.encode_base64(part)
                part.add_header("Content-Disposition", f"attachment; filename=faktura-{inv.invoice_number}.pdf")
                msg.attach(part)

    with smtplib.SMTP(config["SMTP_HOST"], config["SMTP_PORT"]) as server:
        server.starttls(context=ssl.create_default_context())
        server.login(config["SMTP_USER"], config["SMTP_PASSWORD"])
        recipients = [safe_to] + cc_addresses
        server.sendmail(safe_from, recipients, msg.as_string())

# ── Init ──────────────────────────────────────────────────────────────────────

def _relax_time_entry_project_nullability():
    """Drop the NOT NULL on time_entry.project_id, rebuilding the table if needed.

    Internal time has no assignment, so project_id must be nullable. Databases
    created before internal codes existed declare it NOT NULL, and SQLite cannot
    relax a column constraint with ALTER TABLE — the table has to be rebuilt.

    The new table is created from the model metadata, so it always matches the
    current schema, and rows are copied across the columns the two have in common.
    Indexes are dropped with the old table; the CREATE INDEX IF NOT EXISTS
    statements in the migration list below put them back.
    """
    from sqlalchemy import text, MetaData

    with db.engine.connect() as conn:
        cols = conn.execute(text("PRAGMA table_info(time_entry)")).fetchall()
        if not cols:
            return  # fresh database — create_all() already used the current schema
        # PRAGMA table_info columns: (cid, name, type, notnull, dflt_value, pk)
        needs_rebuild = any(c[1] == "project_id" and c[3] == 1 for c in cols)
        if not needs_rebuild:
            return
        old_names = {c[1] for c in cols}

    # Copy the sibling tables across first so the new table's foreign keys can
    # resolve their targets; only the new table is actually created.
    meta = MetaData()
    for table in db.metadata.tables.values():
        if table.name != "time_entry":
            table.to_metadata(meta)
    tmp = TimeEntry.__table__.to_metadata(meta, name="time_entry__new")
    shared = [c.name for c in tmp.columns if c.name in old_names]
    col_list = ", ".join(f'"{c}"' for c in shared)

    with db.engine.connect() as conn:
        conn.execute(text("PRAGMA foreign_keys=OFF"))
        try:
            tmp.create(bind=conn)
            conn.execute(text(
                f'INSERT INTO time_entry__new ({col_list}) SELECT {col_list} FROM time_entry'))
            conn.execute(text("DROP TABLE time_entry"))
            conn.execute(text("ALTER TABLE time_entry__new RENAME TO time_entry"))
            conn.commit()
            print("Rebuilt time_entry: project_id is now nullable")
        except Exception as e:
            conn.rollback()
            print(f"time_entry rebuild skipped: {e}")
            try:
                conn.execute(text("DROP TABLE IF EXISTS time_entry__new"))
                conn.commit()
            except Exception:
                pass
        finally:
            conn.execute(text("PRAGMA foreign_keys=ON"))


def init_db():
    with app.app_context():
        db.create_all()
        # Must run before the ALTER statements below: it rebuilds time_entry from
        # the current model, so the internal_code_id ALTER then no-ops harmlessly.
        _relax_time_entry_project_nullability()
        # Incremental column migrations for existing databases
        from sqlalchemy import text
        migrations = [
            "ALTER TABLE time_entry ADD COLUMN time_type VARCHAR(50) DEFAULT 'Normal'",
            "ALTER TABLE time_entry ADD COLUMN hour_type_id INTEGER REFERENCES po_hour_type(id)",
            "ALTER TABLE po_hour_type ADD COLUMN hourly_rate REAL",
            "ALTER TABLE purchase_order ADD COLUMN po_amount REAL",
            "ALTER TABLE project ADD COLUMN project_number VARCHAR(20)",
            "ALTER TABLE client ADD COLUMN km_rate REAL DEFAULT 25.0",
            "ALTER TABLE purchase_order ADD COLUMN km_rate REAL",
            "ALTER TABLE invoice ADD COLUMN project_id INTEGER REFERENCES project(id)",
            "ALTER TABLE project ADD COLUMN accumulated_cost REAL DEFAULT 0.0",
            "ALTER TABLE project ADD COLUMN invoice_cc_email VARCHAR(200)",
            "ALTER TABLE project ADD COLUMN expense_markup_pct REAL DEFAULT 10.0",
            "ALTER TABLE project ADD COLUMN contact_person_name VARCHAR(200)",
            # v1.1 user model expansion
            "ALTER TABLE user ADD COLUMN display_name VARCHAR(200)",
            "ALTER TABLE user ADD COLUMN email VARCHAR(200)",
            "ALTER TABLE user ADD COLUMN ad_oid VARCHAR(100)",
            "ALTER TABLE user ADD COLUMN role VARCHAR(20) DEFAULT 'admin'",
            "ALTER TABLE user ADD COLUMN active BOOLEAN DEFAULT 1",
            "ALTER TABLE user ADD COLUMN created_at DATETIME",
            "ALTER TABLE user ADD COLUMN last_login_at DATETIME",
            # backfill NULL role/active for users created before the column was added
            "UPDATE \"user\" SET role='admin' WHERE role IS NULL",
            "UPDATE \"user\" SET active=1 WHERE active IS NULL",
            # v1.1 expense categories
            "ALTER TABLE expense ADD COLUMN expense_category_id INTEGER REFERENCES expense_category(id)",
            # v1.1 supplier categories
            "ALTER TABLE supplier_invoice ADD COLUMN supplier_category_id INTEGER REFERENCES supplier_category(id)",
            "ALTER TABLE supplier_invoice ADD COLUMN vat_rate REAL DEFAULT 25.0",
            # omvänd skattskyldighet (reverse charge)
            "ALTER TABLE supplier_invoice ADD COLUMN reverse_charge BOOLEAN DEFAULT 0",
            "ALTER TABLE expense ADD COLUMN reverse_charge BOOLEAN DEFAULT 0",
            # VAT treatment per supplier invoice — domestic RC vs EU services RC
            "ALTER TABLE supplier_invoice ADD COLUMN vat_treatment VARCHAR(40)",
            "ALTER TABLE supplier_invoice ADD COLUMN service_amount_excl_vat REAL DEFAULT 0.0",
            # everything booked before the column existed was domestic
            "UPDATE supplier_invoice SET vat_treatment='reverse_charge_domestic' WHERE vat_treatment IS NULL AND reverse_charge=1",
            "UPDATE supplier_invoice SET vat_treatment='domestic' WHERE vat_treatment IS NULL",
            # indexes for common filter/sort columns
            "CREATE INDEX IF NOT EXISTS ix_supplier_invoice_status ON supplier_invoice(status)",
            "CREATE INDEX IF NOT EXISTS ix_supplier_invoice_due_date ON supplier_invoice(due_date)",
            "CREATE INDEX IF NOT EXISTS ix_supplier_invoice_payment_file_id ON supplier_invoice(payment_file_id)",
            "CREATE INDEX IF NOT EXISTS ix_invoice_status ON invoice(status)",
            "CREATE INDEX IF NOT EXISTS ix_time_entry_invoiced ON time_entry(invoiced)",
            "CREATE INDEX IF NOT EXISTS ix_time_entry_project_id ON time_entry(project_id)",
            "CREATE INDEX IF NOT EXISTS ix_expense_status ON expense(status)",
            "CREATE INDEX IF NOT EXISTS ix_mileage_entry_status ON mileage_entry(status)",
            # v1.1 supplier invoice cleanup — invoice_number replaced by payment_ref
            "ALTER TABLE supplier_invoice DROP COLUMN invoice_number",
            # v1.1 payment account consolidation — bankgiro/plusgiro/iban → payment_account + type
            "ALTER TABLE supplier_invoice ADD COLUMN payment_account VARCHAR(100)",
            "ALTER TABLE supplier_invoice ADD COLUMN payment_account_type VARCHAR(10)",
            # migrate existing data into new columns
            "UPDATE supplier_invoice SET payment_account=bankgiro, payment_account_type='bg' WHERE bankgiro IS NOT NULL AND bankgiro != ''",
            "UPDATE supplier_invoice SET payment_account=plusgiro, payment_account_type='pg' WHERE plusgiro IS NOT NULL AND plusgiro != '' AND (payment_account IS NULL OR payment_account = '')",
            "UPDATE supplier_invoice SET payment_account=iban,     payment_account_type='iban' WHERE iban IS NOT NULL AND iban != '' AND (payment_account IS NULL OR payment_account = '')",
            "ALTER TABLE supplier_invoice DROP COLUMN bankgiro",
            "ALTER TABLE supplier_invoice DROP COLUMN plusgiro",
            "ALTER TABLE supplier_invoice DROP COLUMN iban",
            # auth lookup indexes
            "CREATE INDEX IF NOT EXISTS ix_user_ad_oid ON user(ad_oid)",
            "CREATE INDEX IF NOT EXISTS ix_user_email ON user(email)",
            # v1.2 internal time/cost codes — a target that can never be invoiced
            "ALTER TABLE time_entry ADD COLUMN internal_code_id INTEGER REFERENCES internal_code(id)",
            "ALTER TABLE expense ADD COLUMN internal_code_id INTEGER REFERENCES internal_code(id)",
            "ALTER TABLE mileage_entry ADD COLUMN internal_code_id INTEGER REFERENCES internal_code(id)",
            "ALTER TABLE supplier_invoice ADD COLUMN internal_code_id INTEGER REFERENCES internal_code(id)",
            # payroll: tax paid to the skattekonto, settled on the tax date
            "ALTER TABLE agi_report ADD COLUMN skv_debit_date DATE",
            "ALTER TABLE agi_report ADD COLUMN settle_voucher_ref VARCHAR(50)",
            # payroll: preliminary tax read from Skatteverket's table
            "ALTER TABLE employee ADD COLUMN tax_column INTEGER DEFAULT 1",
            "CREATE INDEX IF NOT EXISTS ix_time_entry_internal_code ON time_entry(internal_code_id)",
            "CREATE INDEX IF NOT EXISTS ix_expense_internal_code ON expense(internal_code_id)",
            "CREATE INDEX IF NOT EXISTS ix_mileage_internal_code ON mileage_entry(internal_code_id)",
            "CREATE INDEX IF NOT EXISTS ix_supplier_invoice_internal_code ON supplier_invoice(internal_code_id)",
            # additional filter indexes
            "CREATE INDEX IF NOT EXISTS ix_expense_project_status ON expense(project_id, status)",
            "CREATE INDEX IF NOT EXISTS ix_mileage_project ON mileage_entry(project_id)",
            "CREATE INDEX IF NOT EXISTS ix_time_entry_date ON time_entry(entry_date)",
        ]
        for stmt in migrations:
            try:
                with db.engine.connect() as conn:
                    conn.execute(text(stmt))
                    conn.commit()
            except Exception:
                pass  # Column already exists or table doesn't exist yet

        # Create admin user if not exists
        if not User.query.first():
            admin_email = app.config.get("ADMIN_EMAIL", "")
            user = User(
                username="admin",
                password_hash=generate_password_hash(app.config["ADMIN_PASSWORD"]),
                display_name=app.config.get("COMPANY_NAME", "Admin"),
                email=admin_email,
                role="admin",
                active=True,
                created_at=datetime.utcnow(),
            )
            db.session.add(user)
            db.session.commit()
            print("Admin user created")
        else:
            # Migrate existing admin user to have role/active set
            admin = User.query.first()
            if admin.role is None:
                admin.role = "admin"
            if admin.active is None:
                admin.active = True
            db.session.commit()

        # Seed default voucher templates if not present
        defaults = [
            {
                "transaction_type": "supplier_invoice",
                "debit_account": "6540", "debit_account_label": "IT-tjänster",
                "vat_account": "2640", "vat_rate": 25.0,
                "credit_account": "2440", "voucher_series": "L",
                "description_template": "{supplier} {invoice_nr}",
            },
            {
                "transaction_type": "expense_internal",
                "debit_account": "6570", "debit_account_label": "Representation / Övrigt",
                "vat_account": "2640", "vat_rate": 25.0,
                "credit_account": "2440", "voucher_series": "A",
                "description_template": "{merchant} {date}",
            },
            {
                "transaction_type": "expense_external",
                "debit_account": "6570", "debit_account_label": "Representation / Övrigt",
                "vat_account": "2640", "vat_rate": 25.0,
                "credit_account": "2890", "voucher_series": "A",
                "description_template": "{merchant} {date}",
            },
            {
                "transaction_type": "mileage",
                "debit_account": "7321", "debit_account_label": "Milersättning",
                "vat_account": "", "vat_rate": 0.0,
                "credit_account": "2890", "voucher_series": "A",
                "description_template": "Mil {date}",
            },
            {
                "transaction_type": "salary",
                "debit_account": "7010", "debit_account_label": "Löner",
                "vat_account": "", "vat_rate": 0.0,
                "credit_account": "2710", "voucher_series": "L",
                "description_template": "Lön {period}",
            },
        ]
        for d in defaults:
            if not VoucherTemplate.query.filter_by(transaction_type=d["transaction_type"]).first():
                db.session.add(VoucherTemplate(**d))
        db.session.commit()

        # Seed default expense categories — upsert by name so existing DBs get the full list
        from models import ExpenseCategory, SupplierCategory
        _default_expense_cats = [
            ("Resekostnader att vidarefakturera",           "4010",  1),
            ("IT tjänster",                                  "6540",  2),
            ("Bankkostnader",                                "6570",  3),
            ("Reparation och underhåll av personbilar",      "5613",  4),
            ("Programvaror",                                 "5420",  5),
            ("Förbrukningsinventarier",                      "5410",  6),
            ("Förvaltnings- och kreditförsäkringsavgifter",  "7470",  7),
            ("Övriga personalkostnader",                     "7690",  8),
            ("Extern representation",                        "6071",  9),
            ("Personalrepresentation ej avdragsgill",        "7632", 10),
            ("Kontorsmaterial och trycksaker",               "6110", 11),
            ("Böcker, tidskrifter och kurser",               "6420", 12),
        ]
        for name, account, order in _default_expense_cats:
            if not ExpenseCategory.query.filter_by(name=name).first():
                db.session.add(ExpenseCategory(name=name, debit_account=account, sort_order=order))
        db.session.commit()

        # Seed default supplier categories — upsert by name so existing DBs get the full list
        _default_supplier_cats = [
            ("Resekostnader att vidarefakturera",           "4010",  1),
            ("Leasing av personbilar",                       "5615",  2),
            ("Försäkring och skatt för personbilar",         "5612",  3),
            ("Programvaror",                                 "5420",  4),
            ("IT tjänster",                                  "6540",  5),
            ("Telekommunikation",                            "6210",  6),
            ("Företagsförsäkringar",                         "6310",  7),
            ("Förvaltnings- och kreditförsäkringsavgifter",  "7470",  8),
            ("Kontorsmaterial och trycksaker",               "6110",  9),
            ("Böcker, tidskrifter och kurser",               "6420", 10),
        ]
        for name, account, order in _default_supplier_cats:
            if not SupplierCategory.query.filter_by(name=name).first():
                db.session.add(SupplierCategory(name=name, debit_account=account, sort_order=order))
        db.session.commit()

        _seed_internal_codes()
        _seed_payroll()


def _seed_internal_codes():
    """Seed the starting set of internal codes and adopt pre-v1.2 internal time.

    Before internal codes existed, non-billable time was logged as a ``time_type``
    string on an entry that still pointed at a client assignment. Those rows are
    moved onto the matching code and detached from the assignment, so they stop
    counting toward that client and can never be picked up by an invoice.
    Entries already tied to an invoice are left alone — rewriting a billed row
    would change an invoice after the fact.
    """
    # (code, name, kind, legacy time_type this replaces)
    seeds = [
        ("SALES",  "Försäljning",    "sales",   None),
        ("SJUK",   "Sjukfrånvaro",   "absence", "Sjuk"),
        ("SEM",    "Semester",       "absence", "Semester"),
        ("VAB",    "VAB",            "absence", "VAB"),
        ("ADMIN",  "Administration", "admin",   None),
        ("UTB",    "Utbildning",     "admin",   None),
    ]
    created = {}
    for order, (code, name, kind, _legacy) in enumerate(seeds, start=1):
        existing = InternalCode.query.filter_by(code=code).first()
        if existing is None:
            existing = InternalCode(
                code=code, name=name, kind=kind,
                track_time=True, track_cost=True,
                sort_order=order, active=True,
            )
            db.session.add(existing)
        created[code] = existing
    db.session.commit()

    moved = 0
    for code, _name, _kind, legacy in seeds:
        if not legacy:
            continue
        target = created[code]
        rows = (TimeEntry.query
                .filter(TimeEntry.internal_code_id == None,
                        TimeEntry.hour_type_id == None,
                        TimeEntry.time_type == legacy,
                        TimeEntry.invoiced == False,
                        TimeEntry.invoice_id == None)
                .all())
        for row in rows:
            row.internal_code_id = target.id
            row.project_id = None
            moved += 1
    if moved:
        db.session.commit()
        print(f"Migrated {moved} internal time entries onto internal codes")

if __name__ == "__main__":
    init_db()
    os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)
    app.run(debug=app.config.get("DEBUG", False), host="0.0.0.0", port=5000)
