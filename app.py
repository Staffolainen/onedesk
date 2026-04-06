import io
import os
import json
import logging
import smtplib
import secrets
import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
from datetime import datetime, date, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
from functools import wraps

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
                    fiscal_year, fy_start, fy_end)
from sqlalchemy.orm import selectinload, joinedload
from fortnox import FortnoxClient
from receipt_ocr import extract_receipt_data, extract_supplier_invoice_data
from pdf_generator import generate_invoice_pdf, render_invoice_html
from payment_file_generator import generate_pain001

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

@app.after_request
def set_security_headers(response):
    # HSTS: tell browsers to use HTTPS for 1 year (only meaningful when served over HTTPS)
    response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    return response

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
    user = getattr(current_user, "username", "—") if current_user.is_authenticated else "anon"
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
        "sup_amounts": "Belopp",
        "sup_excl_vat": "Exkl. moms",
        "sup_vat": "Moms (SEK)",
        "sup_incl_vat": "Inkl. moms",
        "sup_payment": "Betalning",
        "sup_ocr": "OCR eller fakturanummer",
        "sup_bookkeeping": "Bokföring",
        "sup_category": "Leverantörskategori",
        "sup_credit_hint": "Kredit: 2440 Leverantörsskulder",
        "sup_assignment": "Uppdrag (valfritt)",
        "sup_file": "Uppladdad fil",
        "sup_view_file": "Visa fil",
        "sup_save_book": "Spara och bokför",
        "sup_discard": "Kasta faktura",
        "sup_discard_confirm": "Ta bort fakturan?",
        "sup_pending": "Väntar på granskning",
        "sup_backlog": "Betalningsbacklog",
        "sup_payment_files": "Betalningsfiler",
        "sup_recently_paid": "Senast betalda",
        "sup_review_btn": "Granska",
        "sup_mark_paid": "Markera betald manuellt",
        "sup_paid_btn": "Betald",
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
        "sup_amounts": "Amounts",
        "sup_excl_vat": "Excl. VAT",
        "sup_vat": "VAT (SEK)",
        "sup_incl_vat": "Incl. VAT",
        "sup_payment": "Payment",
        "sup_ocr": "OCR or invoice number",
        "sup_bookkeeping": "Bookkeeping",
        "sup_category": "Supplier category",
        "sup_credit_hint": "Credit: 2440 Accounts payable",
        "sup_assignment": "Assignment (optional)",
        "sup_file": "Uploaded file",
        "sup_view_file": "View file",
        "sup_save_book": "Save & book",
        "sup_discard": "Discard invoice",
        "sup_discard_confirm": "Delete this invoice?",
        "sup_pending": "Pending review",
        "sup_backlog": "Payment backlog",
        "sup_payment_files": "Payment files",
        "sup_recently_paid": "Recently paid",
        "sup_review_btn": "Review",
        "sup_mark_paid": "Mark paid manually",
        "sup_paid_btn": "Paid",
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
})

@app.before_request
def set_language():
    g.lang = session.get("lang", "sv")
    g.t = TRANSLATIONS.get(g.lang, TRANSLATIONS["sv"])
    g.config = app.config

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

@app.route("/lang/<lang>")
def set_lang(lang):
    if lang in ("sv", "en"):
        session["lang"] = lang
    return redirect(request.referrer or url_for("dashboard"))

@app.after_request
def set_security_headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; "
        "font-src 'self';"
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

    # This month's hours per client
    monthly_entries = (TimeEntry.query
        .join(Project).join(Client)
        .filter(TimeEntry.entry_date >= month_start)
        .all())

    hours_by_client = {}
    for e in monthly_entries:
        cname = e.project.client.name
        hours_by_client[cname] = hours_by_client.get(cname, 0) + e.hours

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

    # Build matrix rows ordered by first appearance
    seen_keys = []
    matrix = {}
    for e in entries:
        key = (e.project_id, e.hour_type_id, e.time_type)
        if key not in matrix:
            seen_keys.append(key)
            matrix[key] = {
                "project": e.project,
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
    projects = Project.query.filter_by(active=True).join(Client).order_by(Client.name, Project.name).all()

    # Build project → PO hour types map for JS-driven add-row dropdown
    hour_types_map = {}
    for p in projects:
        active_po = None
        for po in p.purchase_orders:
            if po.active and \
               (po.valid_from is None or po.valid_from <= today) and \
               (po.valid_to is None or po.valid_to >= today):
                if active_po is None or (po.valid_from or date.min) > (active_po.valid_from or date.min):
                    active_po = po
        if active_po:
            hour_types_map[p.id] = [
                {"key": f"ht_{ht.id}", "name": ht.name}
                for ht in active_po.hour_types if ht.active
            ]
        else:
            hour_types_map[p.id] = []

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
        projects=projects,
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
    project_id_str = request.form.get("project_id", "").strip()
    week = request.form.get("week")

    if not project_id_str:
        flash("Välj ett uppdrag / Select an assignment", "error")
        return redirect(url_for("time_index", week=week))

    project_id = int(project_id_str)

    # Resolve hour type — either a PO hour type or an internal type string
    # Existing rows pass hour_type_id (int) or time_type (str) directly.
    # New rows from the add-row form pass type_key like "ht_123" or "it_Sjuk".
    hour_type_id = None
    time_type = None

    hour_type_id_raw = request.form.get("hour_type_id", "").strip()
    time_type_raw = request.form.get("time_type", "").strip()
    type_key = request.form.get("type_key", "").strip()

    if hour_type_id_raw:
        hour_type_id = int(hour_type_id_raw)
    elif type_key.startswith("ht_"):
        hour_type_id = int(type_key[3:])
    elif type_key.startswith("it_"):
        time_type = type_key[3:]
    elif time_type_raw:
        time_type = time_type_raw
    else:
        time_type = "Normal"

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

        if hour_type_id:
            existing = TimeEntry.query.filter_by(
                project_id=project_id,
                entry_date=entry_date,
                hour_type_id=hour_type_id,
            ).first()
        else:
            existing = TimeEntry.query.filter(
                TimeEntry.project_id == project_id,
                TimeEntry.entry_date == entry_date,
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
        if hour_type_id:
            existing_entries = TimeEntry.query.filter(
                TimeEntry.project_id == project_id,
                TimeEntry.entry_date >= ws,
                TimeEntry.entry_date <= we,
                TimeEntry.hour_type_id == hour_type_id,
            ).all()
        else:
            existing_entries = TimeEntry.query.filter(
                TimeEntry.project_id == project_id,
                TimeEntry.entry_date >= ws,
                TimeEntry.entry_date <= we,
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
        key = e.project_id
        if key not in by_project:
            by_project[key] = {"project": e.project, "entries": [], "total": 0}
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
    expenses = q.all()
    projects = Project.query.filter_by(active=True).join(Client).order_by(Client.name).all()
    return render_template("expenses/index.html", expenses=expenses, projects=projects, status_filter=status_filter)

@app.route("/expenses/capture", methods=["GET", "POST"])
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
    projects = Project.query.filter_by(active=True).join(Client).order_by(Client.name).all()
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
        vat_amount = round(_safe_float(request.form.get("vat_amount"), 0.0), 2)
        amount_excl = round(amount_incl - vat_amount, 2)
        vat_rate = round(vat_amount / amount_excl * 100, 1) if amount_excl > 0 else 0.0
        expense_date = _safe_date(request.form.get("expense_date"))
        if not expense_date:
            flash("Ogiltigt datum / Invalid date", "error")
            return redirect(url_for("expenses_review"))

        exp = Expense(
            project_id=_safe_int(request.form.get("project_id")) or None,
            expense_category_id=_safe_int(request.form.get("expense_category_id")) or None,
            expense_date=expense_date,
            merchant=request.form.get("merchant", ""),
            description=request.form.get("description", ""),
            amount_incl_vat=amount_incl,
            amount_excl_vat=amount_excl,
            vat_amount=vat_amount,
            vat_rate=vat_rate,
            billable=request.form.get("billable") == "1",
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
                attach_name = f"{voucher_ref}-{base}"
                _email_pdf_to_fortnox_inbox(receipt_path, attach_name, voucher_ref, app.config)
        except Exception as e:
            flash(f"Fortnox-fel / Fortnox error: {e}", "warning")

        flash("Utlägg sparat / Expense saved", "success")
        return redirect(url_for("expenses_index"))

    return render_template("expenses/review.html",
        pending=pending, projects=projects, categories=categories,
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
    projects = Project.query.filter_by(active=True).join(Client).order_by(Client.name).all()
    if request.method == "POST":
        project_id = _safe_int(request.form.get("project_id")) or None
        entry_date = _safe_date(request.form.get("entry_date"))
        if not entry_date:
            flash("Ogiltigt datum / Invalid date", "error")
            return redirect(url_for("mileage_index"))
        km = _safe_float(request.form.get("km"), 0.0)
        # Resolve km rate: PO for that date → client default
        km_rate = None
        if project_id:
            project = Project.query.get(project_id)
            km_rate = project.get_km_rate(entry_date)
        # Allow manual override
        km_rate_override = request.form.get("km_rate", "").strip()
        if km_rate_override:
            km_rate = _safe_float(km_rate_override, km_rate)
        if not km_rate:
            km_rate = 25.0
        m = MileageEntry(
            project_id=project_id,
            entry_date=entry_date,
            km=km,
            km_rate=km_rate,
            description=request.form.get("description", ""),
            billable=request.form.get("billable") == "1",
            status="approved",
        )
        db.session.add(m)
        db.session.commit()
        flash("Reseersättning sparad / Mileage saved", "success")
        return redirect(url_for("mileage_index"))
    entries = MileageEntry.query.order_by(MileageEntry.entry_date.desc()).all()
    return render_template("mileage/index.html", entries=entries, projects=projects, today=date.today())

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
                    TimeEntry.entry_date >= period_start,
                    TimeEntry.entry_date <= period_end,
                    TimeEntry.invoiced == False)
            .order_by(TimeEntry.entry_date)
            .all())

        # Gather approved billable expenses for this assignment
        expenses = (Expense.query
            .filter(
                Expense.project_id == project_id,
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
                MileageEntry.billable == True,
                MileageEntry.status == "approved",
                MileageEntry.invoice_id == None,
                MileageEntry.entry_date >= period_start,
                MileageEntry.entry_date <= period_end,
            ).all())

        time_subtotal = round(sum(e.hours * e.effective_rate for e in entries), 2)
        expense_subtotal = round(sum(e.amount_excl_vat for e in expenses), 2)
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
            voucher_ref = f"{voucher_series}{voucher_nr}" if voucher_nr else None
            if voucher_ref:
                inv.fortnox_invoice_nr = voucher_ref
                db.session.commit()
                # Email PDF to Fortnox arkivplats inbox
                if inv.pdf_filename:
                    pdf_path = os.path.join(app.root_path, "static", "uploads", inv.pdf_filename)
                    attach_name = f"{voucher_ref}-{inv.pdf_filename}"
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
    return render_template("settings.html",
        fortnox_connected=fortnox_connected,
        users=users,
        expense_categories=expense_cats,
        supplier_categories=supplier_cats)

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
        Settings.query.delete()
        User.query.delete()
        db.session.flush()

        # Restore in parent-first order
        _restore_rows(Client, d.get("clients", []),
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

        inv = SupplierInvoice(
            pdf_filename=filename,
            supplier_name=ocr.get("supplier_name"),
            supplier_org_nr=ocr.get("supplier_org_nr"),
            invoice_number=ocr.get("invoice_number"),
            invoice_date=_safe_date(ocr.get("invoice_date")),
            due_date=_safe_date(ocr.get("due_date")),
            amount_excl_vat=_safe_float(ocr.get("amount_excl_vat")),
            vat_amount=_safe_float(ocr.get("vat_amount")),
            amount_incl_vat=_safe_float(ocr.get("amount_incl_vat")),
            currency=ocr.get("currency") or "SEK",
            payment_ref=ocr.get("payment_ref"),
            bankgiro=ocr.get("bankgiro"),
            plusgiro=ocr.get("plusgiro"),
            iban=ocr.get("iban"),
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
    projects = Project.query.filter_by(active=True).join(Client).order_by(Client.name).all()
    categories = SupplierCategory.query.filter_by(active=True).order_by(SupplierCategory.sort_order, SupplierCategory.name).all()

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
        inv.invoice_number = request.form.get("invoice_number", "").strip()
        inv.invoice_date = _safe_date(request.form.get("invoice_date"))
        inv.due_date = _safe_date(request.form.get("due_date"))
        amount_excl = _safe_float(request.form.get("amount_excl_vat"))
        vat_amt     = _safe_float(request.form.get("vat_amount"))
        amount_incl = _safe_float(request.form.get("amount_incl_vat"))

        # Validate amounts
        if amount_incl <= 0:
            flash("Belopp inkl. moms måste vara större än noll / Amount incl. VAT must be greater than zero", "error")
            ocr = json.loads(inv.ocr_raw) if inv.ocr_raw else {}
            return render_template("supplier_invoices/review.html",
                inv=inv, projects=projects, categories=categories, ocr=ocr)
        if vat_amt < 0:
            flash("Momsbelopp kan inte vara negativt / VAT amount cannot be negative", "error")
            ocr = json.loads(inv.ocr_raw) if inv.ocr_raw else {}
            return render_template("supplier_invoices/review.html",
                inv=inv, projects=projects, categories=categories, ocr=ocr)
        expected_incl = round(amount_excl + vat_amt, 2)
        if abs(expected_incl - amount_incl) > 0.05:
            flash(
                f"Beloppen stämmer inte: {amount_excl:.2f} + {vat_amt:.2f} moms ≠ {amount_incl:.2f} inkl. moms "
                f"/ Amounts inconsistent: {amount_excl:.2f} + {vat_amt:.2f} VAT ≠ {amount_incl:.2f} incl. VAT",
                "error"
            )
            ocr = json.loads(inv.ocr_raw) if inv.ocr_raw else {}
            return render_template("supplier_invoices/review.html",
                inv=inv, projects=projects, categories=categories, ocr=ocr)

        inv.amount_excl_vat = amount_excl
        inv.vat_amount = vat_amt
        inv.amount_incl_vat = amount_incl
        inv.currency = request.form.get("currency", "SEK")
        inv.payment_ref = request.form.get("payment_ref", "").strip()
        inv.bankgiro = request.form.get("bankgiro", "").strip()
        inv.plusgiro = request.form.get("plusgiro", "").strip()
        inv.iban = request.form.get("iban", "").strip()
        inv.supplier_category_id = _safe_int(request.form.get("supplier_category_id")) or None
        inv.vat_rate = _safe_float(request.form.get("vat_rate"), 25.0)
        inv.project_id = _safe_int(request.form.get("project_id")) or None
        inv.status = "approved"
        db.session.commit()

        _audit("supplier_invoice_saved", f"invoice_id={invoice_id} supplier={inv.supplier_name} amount={inv.amount_incl_vat} {inv.currency}")
        flash("Faktura sparad / Invoice saved", "success")
        try:
            fortnox = FortnoxClient(app.config)
            voucher_ref = fortnox.create_supplier_invoice_voucher(inv)
            flash(f"Fortnox verifikat skapat: {voucher_ref} / Voucher created: {voucher_ref}", "success")
            if inv.pdf_filename and voucher_ref:
                folder = _supplier_upload_folder()
                old_path = os.path.join(folder, inv.pdf_filename)
                # Strip random hex prefix, prepend voucher ref
                base = inv.pdf_filename
                if "_" in base and len(base.split("_")[0]) == 16:
                    base = base.split("_", 1)[1]
                new_filename = f"{voucher_ref}-{base}"
                new_path = os.path.join(folder, new_filename)
                if os.path.exists(old_path):
                    os.rename(old_path, new_path)
                    inv.pdf_filename = new_filename
                    db.session.commit()
                _email_pdf_to_fortnox_inbox(new_path, new_filename, voucher_ref, app.config)
        except Exception as e:
            flash(f"Fortnox-fel / Fortnox error: {e}", "warning")
            session["fortnox_preview"] = _build_supplier_voucher_preview(inv)
            session["fortnox_preview"]["back_url"] = url_for("supplier_invoices_index")
            session["fortnox_preview"]["back_label"] = "← Leverantörsfakturor / Supplier invoices"
            return redirect(url_for("fortnox_preview"))

        return redirect(url_for("supplier_invoices_index"))

    ocr = json.loads(inv.ocr_raw) if inv.ocr_raw else {}
    return render_template("supplier_invoices/review.html",
        inv=inv, projects=projects, categories=categories, ocr=ocr)

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
    flash("Borttagen / Deleted", "success")
    return redirect(url_for("supplier_invoices_index"))

# ── Payment run ───────────────────────────────────────────────────────────────

@app.route("/supplier-invoices/payment-run", methods=["GET", "POST"])
@login_required
def payment_run():
    backlog = SupplierInvoice.query.filter(
        SupplierInvoice.status == "booked",
        SupplierInvoice.payment_file_id == None,
    ).order_by(SupplierInvoice.due_date).all()
    pending_files = PaymentFile.query.filter_by(status="generated").order_by(PaymentFile.created_at.desc()).all()
    if request.method == "POST":
        selected_ids = [_safe_int(i) for i in request.form.getlist("invoice_ids") if i]
        payment_date = _safe_date(request.form.get("payment_date")) or date.today()
        if not selected_ids:
            flash("Välj minst en faktura / Select at least one invoice", "error")
            return redirect(url_for("payment_run"))

        selected = SupplierInvoice.query.filter(
            SupplierInvoice.id.in_(selected_ids),
            SupplierInvoice.status == "booked",
            SupplierInvoice.payment_file_id == None,
        ).all()
        if not selected:
            flash("Inga giltiga fakturor / No valid invoices", "error")
            return redirect(url_for("payment_run"))

        try:
            xml_bytes = generate_pain001(selected, payment_date, app.config)
        except Exception as e:
            flash(f"Kunde inte generera betalningsfil / Could not generate payment file: {e}", "error")
            return redirect(url_for("payment_run"))

        # Save file record
        total = sum(i.amount_incl_vat for i in selected)
        filename = f"betalning_{payment_date.isoformat()}_{secrets.token_hex(4)}.xml"
        pf = PaymentFile(
            filename=filename,
            payment_date=payment_date,
            total_amount=total,
            invoice_count=len(selected),
            status="generated",
        )
        db.session.add(pf)
        db.session.flush()
        for inv in selected:
            inv.payment_file_id = pf.id
        db.session.commit()
        _audit("payment_file_generated", f"pf_id={pf.id} file={filename} invoices={len(selected)} total={total}")

        return send_file(
            io.BytesIO(xml_bytes),
            as_attachment=True,
            download_name=filename,
            mimetype="application/xml",
        )

    return render_template("supplier_invoices/payment_run.html", backlog=backlog, today=date.today(), pending_files=pending_files)

@app.route("/supplier-invoices/payment-files/<int:pf_id>/confirm", methods=["POST"])
@login_required
def payment_file_confirm(pf_id):
    pf = PaymentFile.query.get_or_404(pf_id)
    pf.status = "confirmed"
    pf.confirmed_at = datetime.utcnow()
    for inv in pf.supplier_invoices:
        inv.status = "paid"
    db.session.commit()
    _audit("payment_file_confirmed", f"pf_id={pf_id} file={pf.filename} invoices={pf.invoice_count} total={pf.total_amount}")

    # Post payment voucher: 2440 → 1930 for all invoices in this payment file
    try:
        fortnox = FortnoxClient(app.config)
        invoice_ids = [inv.id for inv in pf.supplier_invoices]
        voucher_ref = fortnox.create_payment_voucher(invoice_ids, pf.payment_date)
        flash(
            f"Betalning bekräftad — {pf.invoice_count} fakturor betalda, verifikat {voucher_ref} / "
            f"Payment confirmed — {pf.invoice_count} invoices paid, voucher {voucher_ref}",
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
    invs = [i for i in pf.supplier_invoices if i.bankgiro or i.plusgiro or i.iban]
    xml_bytes = generate_pain001(invs, pf.payment_date, app.config)
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
    debit_acc = "6540"
    if inv.supplier_category and inv.supplier_category.debit_account:
        debit_acc = inv.supplier_category.debit_account
    elif inv.account_code:
        debit_acc = inv.account_code

    vat_rate = float(inv.vat_rate or 0)
    description = f"{inv.supplier_name or 'Leverantör'} {inv.invoice_number or ''}".strip()
    rows = [
        {"Account": debit_acc, "Debit": inv.amount_excl_vat, "Credit": 0,
         "_label": "Kostnadskonto / Expense account"},
    ]
    if vat_rate > 0 and inv.vat_amount:
        rows.append({"Account": "2641", "Debit": inv.vat_amount, "Credit": 0,
                     "_label": "Debiterad ingående moms"})
    rows.append({"Account": "2440", "Debit": 0, "Credit": inv.amount_incl_vat,
                 "_label": "Leverantörsskulder (betalas vid betalfil)"})

    payload = {
        "_endpoint": "POST /vouchers",
        "_note": "Fortnox live booking disabled — dry-run preview",
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
            server.starttls()
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
    safe_name    = _sanitize_header(client.contact_name or client.name)

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

    # Build CC list: project-level CC + admin email
    cc_addresses = []
    if inv.project and inv.project.invoice_cc_email:
        cc_addresses.append(_sanitize_header(inv.project.invoice_cc_email))
    admin_email = config.get("ADMIN_EMAIL", "")
    if admin_email and admin_email != safe_to:
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
        server.starttls()
        server.login(config["SMTP_USER"], config["SMTP_PASSWORD"])
        recipients = [safe_to] + cc_addresses
        server.sendmail(safe_from, recipients, msg.as_string())

# ── Init ──────────────────────────────────────────────────────────────────────

def init_db():
    with app.app_context():
        db.create_all()
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
            # indexes for common filter/sort columns
            "CREATE INDEX IF NOT EXISTS ix_supplier_invoice_status ON supplier_invoice(status)",
            "CREATE INDEX IF NOT EXISTS ix_supplier_invoice_due_date ON supplier_invoice(due_date)",
            "CREATE INDEX IF NOT EXISTS ix_supplier_invoice_payment_file_id ON supplier_invoice(payment_file_id)",
            "CREATE INDEX IF NOT EXISTS ix_invoice_status ON invoice(status)",
            "CREATE INDEX IF NOT EXISTS ix_time_entry_invoiced ON time_entry(invoiced)",
            "CREATE INDEX IF NOT EXISTS ix_time_entry_project_id ON time_entry(project_id)",
            "CREATE INDEX IF NOT EXISTS ix_expense_status ON expense(status)",
            "CREATE INDEX IF NOT EXISTS ix_mileage_entry_status ON mileage_entry(status)",
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

        # Seed default expense categories if none exist
        from models import ExpenseCategory, SupplierCategory
        if not ExpenseCategory.query.first():
            default_cats = [
                ExpenseCategory(name="Resekostnader att vidarefakturera", debit_account="4010", sort_order=1),
                ExpenseCategory(name="Personalrepresentation ej avdragsgill", debit_account="7632", sort_order=2),
                ExpenseCategory(name="Diverse inköp", debit_account="5410", sort_order=3),
                ExpenseCategory(name="Reparation och underhåll av personbilar", debit_account="5613", sort_order=4),
            ]
            for cat in default_cats:
                db.session.add(cat)
            db.session.commit()

        # Seed default supplier categories if none exist
        if not SupplierCategory.query.first():
            default_sup_cats = [
                SupplierCategory(name="IT-tjänster och programvaror", debit_account="6540", sort_order=1),
                SupplierCategory(name="Underkonsulter", debit_account="4010", sort_order=2),
                SupplierCategory(name="Lokalhyra", debit_account="5010", sort_order=3),
                SupplierCategory(name="Telekommunikation", debit_account="6250", sort_order=4),
                SupplierCategory(name="Kontorsmaterial och trycksaker", debit_account="6110", sort_order=5),
                SupplierCategory(name="Böcker, tidskrifter och kurser", debit_account="6420", sort_order=6),
            ]
            for cat in default_sup_cats:
                db.session.add(cat)
            db.session.commit()

if __name__ == "__main__":
    init_db()
    os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)
    app.run(debug=app.config.get("DEBUG", False), host="0.0.0.0", port=5000)
