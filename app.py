import os
import json
import smtplib
import secrets
from datetime import datetime, date, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
from functools import wraps

from flask import (Flask, render_template, request, redirect, url_for,
                   flash, session, jsonify, send_file, abort, g)
from flask_login import LoginManager, login_user, logout_user, login_required, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename

from config import Config
from models import db, User, Client, Project, PurchaseOrder, POHourType, TimeEntry, Expense, Invoice, Settings, fiscal_year, fy_start, fy_end
from fortnox import FortnoxClient
from receipt_ocr import extract_receipt_data
from pdf_generator import generate_invoice_pdf, render_invoice_html

app = Flask(__name__)
app.config.from_object(Config)

db.init_app(app)

login_manager = LoginManager(app)
login_manager.login_view = "auth_login"
login_manager.login_message = ""

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

# ── Language ──────────────────────────────────────────────────────────────────

TRANSLATIONS = {
    "sv": {
        "dashboard": "Start", "time": "Tidrapportering", "expenses": "Reseräkning",
        "invoices": "Fakturor", "clients": "Kunder", "logout": "Logga ut",
        "save": "Spara", "cancel": "Avbryt", "delete": "Ta bort", "edit": "Redigera",
        "approve": "Godkänn", "send": "Skicka", "hours": "Timmar", "date": "Datum",
        "description": "Beskrivning", "client": "Kund", "project": "Projekt",
        "amount": "Belopp", "vat": "Moms", "total": "Totalt", "status": "Status",
        "invoice": "Faktura", "draft": "Utkast", "approved": "Godkänd",
        "sent": "Skickad", "paid": "Betald", "pending": "Väntande",
        "billable": "Debiterbart", "internal": "Internt", "new": "Ny",
        "back": "Tillbaka", "settings": "Inställningar", "currency": "SEK",
    },
    "en": {
        "dashboard": "Start", "time": "Time Tracking", "expenses": "Travel Expenses",
        "invoices": "Invoices", "clients": "Clients", "logout": "Log out",
        "save": "Save", "cancel": "Cancel", "delete": "Delete", "edit": "Edit",
        "approve": "Approve", "send": "Send", "hours": "Hours", "date": "Date",
        "description": "Description", "client": "Client", "project": "Project",
        "amount": "Amount", "vat": "VAT", "total": "Total", "status": "Status",
        "invoice": "Invoice", "draft": "Draft", "approved": "Approved",
        "sent": "Sent", "paid": "Paid", "pending": "Pending",
        "billable": "Billable", "internal": "Internal", "new": "New",
        "back": "Back", "settings": "Settings", "currency": "SEK",
    }
}

@app.before_request
def set_language():
    g.lang = session.get("lang", "sv")
    g.t = TRANSLATIONS.get(g.lang, TRANSLATIONS["sv"])
    g.config = app.config

@app.route("/lang/<lang>")
def set_lang(lang):
    if lang in ("sv", "en"):
        session["lang"] = lang
    return redirect(request.referrer or url_for("dashboard"))

def fmt_amount(amount):
    """Format number as Swedish currency string"""
    return f"{amount:,.0f}".replace(",", " ")

app.jinja_env.filters["fmt_amount"] = fmt_amount

# ── Auth ──────────────────────────────────────────────────────────────────────

@app.route("/login", methods=["GET", "POST"])
def auth_login():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))
    if request.method == "POST":
        password = request.form.get("password")
        user = User.query.first()
        if user and check_password_hash(user.password_hash, password):
            login_user(user, remember=True)
            return redirect(url_for("dashboard"))
        flash("Fel lösenord / Wrong password", "error")
    return render_template("auth/login.html")

@app.route("/logout")
@login_required
def auth_logout():
    logout_user()
    return redirect(url_for("auth_login"))

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

    return render_template("dashboard.html",
        hours_by_client=hours_by_client,
        pending_expenses=pending_expenses,
        open_invoices=open_invoices,
        recent_entries=recent_entries,
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
            hourly_rate=float(request.form.get("hourly_rate") or app.config["DEFAULT_HOURLY_RATE"]),
            payment_days=int(request.form.get("payment_days") or 30),
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
        c.hourly_rate = float(request.form.get("hourly_rate") or 1500)
        c.payment_days = int(request.form.get("payment_days") or 30)
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
            name=request.form["name"],
            description=request.form.get("description"),
            start_date=datetime.strptime(request.form["start_date"], "%Y-%m-%d").date() if request.form.get("start_date") else None,
        )
        db.session.add(p)
        db.session.commit()
        flash("Projekt skapat / Project created", "success")
        return redirect(url_for("clients_list"))
    return render_template("clients/project_form.html", client=c)

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
        valid_from = datetime.strptime(request.form["valid_from"], "%Y-%m-%d").date() if request.form.get("valid_from") else None
        valid_to   = datetime.strptime(request.form["valid_to"],   "%Y-%m-%d").date() if request.form.get("valid_to")   else None
        overlap = _po_overlaps(project_id, valid_from, valid_to)
        if overlap:
            flash(f"Datumkonflik med PO {overlap.po_number or overlap.id} – perioder får ej överlappa / "
                  f"Date conflict with PO {overlap.po_number or overlap.id} – periods must not overlap", "error")
            return render_template("clients/po_form.html", project=p, po=None)
        po = PurchaseOrder(
            project_id=project_id,
            po_number=request.form.get("po_number") or None,
            description=request.form.get("description") or None,
            hourly_rate=float(request.form["hourly_rate"]),
            valid_from=valid_from,
            valid_to=valid_to,
        )
        db.session.add(po)
        db.session.flush()
        db.session.add(POHourType(po_id=po.id, name="Normal", billable=True, sort_order=0, hourly_rate=po.hourly_rate))
        db.session.add(POHourType(po_id=po.id, name="Restid", billable=True, sort_order=1, hourly_rate=po.hourly_rate))
        db.session.commit()
        flash("PO skapat / PO created", "success")
        return redirect(url_for("clients_list"))
    return render_template("clients/po_form.html", project=p, po=None)

@app.route("/purchase-orders/<int:po_id>/edit", methods=["GET", "POST"])
@login_required
def po_edit(po_id):
    po = PurchaseOrder.query.get_or_404(po_id)
    project = po.project
    if request.method == "POST":
        valid_from = datetime.strptime(request.form["valid_from"], "%Y-%m-%d").date() if request.form.get("valid_from") else None
        valid_to   = datetime.strptime(request.form["valid_to"],   "%Y-%m-%d").date() if request.form.get("valid_to")   else None
        overlap = _po_overlaps(po.project_id, valid_from, valid_to, exclude_po_id=po.id)
        if overlap:
            flash(f"Datumkonflik med PO {overlap.po_number or overlap.id} – perioder får ej överlappa / "
                  f"Date conflict with PO {overlap.po_number or overlap.id} – periods must not overlap", "error")
            return redirect(url_for("po_edit", po_id=po.id))
        po.po_number = request.form.get("po_number") or None
        po.description = request.form.get("description") or None
        po.hourly_rate = float(request.form["hourly_rate"])
        po_amount_str = request.form.get("po_amount", "").strip()
        po.po_amount = float(po_amount_str) if po_amount_str else None
        po.valid_from = valid_from
        po.valid_to   = valid_to
        db.session.commit()
        flash("PO uppdaterat / PO updated", "success")
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
    flash("PO borttaget / PO deleted", "success")
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

    import json
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
        hour_types_map_json=json.dumps(hour_types_map),
    )

@app.route("/time/save-row", methods=["POST"])
@login_required
def time_save_row():
    project_id_str = request.form.get("project_id", "").strip()
    week = request.form.get("week")

    if not project_id_str:
        flash("Välj ett projekt / Select a project", "error")
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

    projects = Project.query.filter_by(active=True).join(Client).order_by(Client.name).all()

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
        amount_incl = float(request.form["amount_incl_vat"])
        vat_rate = float(request.form.get("vat_rate", 25))
        vat_amount = round(amount_incl * vat_rate / (100 + vat_rate), 2)
        amount_excl = round(amount_incl - vat_amount, 2)

        exp = Expense(
            project_id=int(request.form["project_id"]) if request.form.get("project_id") else None,
            expense_date=datetime.strptime(request.form["expense_date"], "%Y-%m-%d").date(),
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
            fortnox.create_expense_voucher(exp)
        except Exception as e:
            flash(f"Fortnox-fel / Fortnox error: {e}", "warning")

        flash("Utlägg sparat / Expense saved", "success")
        return redirect(url_for("expenses_index"))

    return render_template("expenses/review.html",
        pending=pending, projects=projects,
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

# ── Invoices ──────────────────────────────────────────────────────────────────

@app.route("/invoices")
@login_required
def invoices_list():
    invoices = Invoice.query.order_by(Invoice.issue_date.desc()).all()
    clients = Client.query.filter_by(active=True).order_by(Client.name).all()
    return render_template("invoices/index.html", invoices=invoices, clients=clients, today=date.today())

@app.route("/invoices/new", methods=["GET", "POST"])
@login_required
def invoices_new():
    clients = Client.query.filter_by(active=True).order_by(Client.name).all()
    today = date.today()
    if request.method == "POST":
        client_id = int(request.form["client_id"])
        client = Client.query.get_or_404(client_id)
        period_start = datetime.strptime(request.form["period_start"], "%Y-%m-%d").date()
        period_end = datetime.strptime(request.form["period_end"], "%Y-%m-%d").date()

        # Gather uninvoiced time entries
        entries = (TimeEntry.query
            .join(Project)
            .filter(Project.client_id == client_id,
                    TimeEntry.entry_date >= period_start,
                    TimeEntry.entry_date <= period_end,
                    TimeEntry.invoiced == False)
            .order_by(TimeEntry.entry_date)
            .all())

        # Gather approved billable expenses
        expenses = (Expense.query
            .join(Project, isouter=True)
            .filter(
                Expense.billable == True,
                Expense.status == "approved",
                Expense.invoice_id == None,
                Expense.expense_date >= period_start,
                Expense.expense_date <= period_end,
                db.or_(
                    Project.client_id == client_id,
                    Expense.project_id == None
                )
            ).all())

        total_hours = sum(e.hours for e in entries)
        time_subtotal = round(sum(e.hours * e.effective_rate for e in entries), 2)
        expense_subtotal = round(sum(e.amount_excl_vat for e in expenses), 2)
        subtotal = time_subtotal + expense_subtotal
        vat = round(subtotal * 0.25, 2)
        total = round(subtotal + vat, 2)

        inv = Invoice(
            client_id=client_id,
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
        inv.generate_number(app.config["FY_START_MONTH"])
        db.session.add(inv)
        db.session.flush()  # get inv.id

        for e in entries:
            e.invoice_id = inv.id
            # DEBUG: locking disabled for template iteration
            # e.invoiced = True
        for exp in expenses:
            exp.invoice_id = inv.id
            # DEBUG: locking disabled for template iteration
            # exp.status = "invoiced"

        db.session.commit()
        return redirect(url_for("invoices_proforma", invoice_id=inv.id))

    # Default period: last month
    first_of_month = today.replace(day=1)
    last_month_end = first_of_month - timedelta(days=1)
    last_month_start = last_month_end.replace(day=1)

    return render_template("invoices/new.html",
        clients=clients,
        default_start=last_month_start.isoformat(),
        default_end=last_month_end.isoformat(),
    )

@app.route("/invoices/<int:invoice_id>")
@login_required
def invoices_proforma(invoice_id):
    inv = Invoice.query.get_or_404(invoice_id)
    return render_template("invoices/proforma.html", inv=inv)

@app.route("/invoices/<int:invoice_id>/approve", methods=["POST"])
@login_required
def invoices_approve(invoice_id):
    inv = Invoice.query.get_or_404(invoice_id)
    inv.status = "approved"
    # DEBUG: locking disabled for template iteration
    # for e in inv.time_entries:
    #     e.invoiced = True
    # for exp in inv.expenses:
    #     exp.status = "invoiced"
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

    # DEBUG: Fortnox API call disabled — dry-run only
    try:
        fortnox = FortnoxClient(app.config)
        customer_nr = fortnox.get_or_create_customer(inv.client)

        rows = []
        if inv.time_entries:
            total_hours = sum(e.hours for e in inv.time_entries)
            rows.append({
                "ArticleNumber": "TID",
                "Description": f"Konsulttjänster {inv.period_start} – {inv.period_end}",
                "DeliveredQuantity": str(total_hours),
                "Price": str(inv.client.hourly_rate),
                "VAT": "25",
                "Unit": "tim",
            })
        for exp in inv.expenses:
            rows.append({
                "Description": exp.description or exp.merchant or "Utlägg",
                "DeliveredQuantity": "1",
                "Price": str(exp.amount_excl_vat),
                "VAT": str(int(exp.vat_rate)),
            })

        dry_run_payload = {
            "Invoice": {
                "CustomerNumber": customer_nr,
                "InvoiceDate": inv.issue_date.isoformat(),
                "DueDate": inv.due_date.isoformat(),
                "Currency": inv.currency,
                "Language": "SV" if inv.language == "sv" else "EN",
                "InvoiceRows": rows,
                "Remarks": inv.notes or "",
            }
        }

        import json as _json
        app.logger.info(
            "DEBUG Fortnox dry-run — POST %s/invoices\n%s",
            FortnoxClient.BASE_URL,
            _json.dumps(dry_run_payload, indent=2, ensure_ascii=False),
        )
        flash(
            "DEBUG: Fortnox POST (dry-run) — se serverloggen för payload / "
            "See server log for payload",
            "info",
        )
    except Exception as e:
        flash(f"Fortnox dry-run fel / Fortnox dry-run error: {e}", "warning")

    return redirect(url_for("invoices_proforma", invoice_id=invoice_id))

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
    flash("Markerad som betald / Marked as paid", "success")
    return redirect(url_for("invoices_list"))

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
    db.session.delete(inv)
    db.session.commit()
    flash("Faktura borttagen / Invoice deleted", "success")
    return redirect(url_for("invoices_list"))

# ── Fortnox OAuth ─────────────────────────────────────────────────────────────

@app.route("/fortnox/connect")
@login_required
def fortnox_connect():
    fortnox = FortnoxClient(app.config)
    auth_url = fortnox.get_auth_url()
    return redirect(auth_url)

@app.route("/fortnox/callback")
@login_required
def fortnox_callback():
    code = request.args.get("code")
    if not code:
        flash("Fortnox-anslutning misslyckades / Fortnox connection failed", "error")
        return redirect(url_for("settings"))
    try:
        fortnox = FortnoxClient(app.config)
        tokens = fortnox.exchange_code(code)
        Settings.set("fortnox_access_token", tokens["access_token"])
        Settings.set("fortnox_refresh_token", tokens.get("refresh_token", ""))
        flash("Fortnox anslutet / Fortnox connected!", "success")
    except Exception as e:
        flash(f"Fortnox-fel: {e}", "error")
    return redirect(url_for("settings"))

@app.route("/settings")
@login_required
def settings():
    fortnox_connected = bool(Settings.get("fortnox_access_token"))
    return render_template("settings.html", fortnox_connected=fortnox_connected)

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
        user = User.query.first()
        user.password_hash = generate_password_hash(new_pw)
        db.session.commit()
        flash("Lösenord uppdaterat / Password updated", "success")
    return redirect(url_for("settings"))

# ── Helpers ───────────────────────────────────────────────────────────────────

def _send_invoice_email(inv, config):
    client = inv.client
    lang = inv.language

    subject_sv = f"Faktura {inv.invoice_number} – {config['COMPANY_NAME']}"
    subject_en = f"Invoice {inv.invoice_number} – {config['COMPANY_NAME']}"
    subject = subject_sv if lang == "sv" else subject_en

    body_sv = f"""Hej {client.contact_name or client.name},

Bifogat finner du faktura {inv.invoice_number} avseende perioden {inv.period_start} – {inv.period_end}.

Belopp: {inv.total:,.0f} SEK inkl. moms
Förfallodatum: {inv.due_date}
Bankgiro: {config['COMPANY_BANKGIRO']}

Med vänliga hälsningar,
{config['COMPANY_NAME']}
"""
    body_en = f"""Hi {client.contact_name or client.name},

Please find attached invoice {inv.invoice_number} for the period {inv.period_start} – {inv.period_end}.

Amount: {inv.total:,.0f} SEK incl. VAT
Due date: {inv.due_date}
Bankgiro: {config['COMPANY_BANKGIRO']}

Best regards,
{config['COMPANY_NAME']}
"""
    body = body_sv if lang == "sv" else body_en

    msg = MIMEMultipart()
    msg["From"] = config["SMTP_FROM"]
    msg["To"] = client.contact_email
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
        server.send_message(msg)

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
        ]
        for stmt in migrations:
            try:
                with db.engine.connect() as conn:
                    conn.execute(text(stmt))
                    conn.commit()
            except Exception:
                pass  # Column already exists
        # Create admin user if not exists
        if not User.query.first():
            user = User(
                username="admin",
                password_hash=generate_password_hash(app.config["ADMIN_PASSWORD"])
            )
            db.session.add(user)
            db.session.commit()
            print("Admin user created")

if __name__ == "__main__":
    init_db()
    os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)
    app.run(debug=True, host="0.0.0.0", port=5000)
