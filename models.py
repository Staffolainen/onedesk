import os
import base64
import hashlib
import sqlite3
from datetime import datetime, date
from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin
from sqlalchemy import event
from sqlalchemy.engine import Engine

# Omvänd skattskyldighet (reverse charge): the supplier invoices without VAT and
# the buyer self-assesses it. Accounts and voucher rules live in bookkeeping.py.
from bookkeeping import VAT_DOMESTIC

# Fallback mileage rate when neither a PO, a client nor an internal code sets one.
DEFAULT_KM_RATE = 25.0


def _fernet():
    from cryptography.fernet import Fernet
    secret = os.environ.get("SECRET_KEY", "dev-secret-change-me")
    key = base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest())
    return Fernet(key)

_ENCRYPTED_KEYS = {"fortnox_access_token", "fortnox_refresh_token"}

def _encrypt(value: str) -> str:
    return _fernet().encrypt(value.encode()).decode()

def _decrypt(value: str) -> str:
    try:
        return _fernet().decrypt(value.encode()).decode()
    except Exception:
        return value


def fiscal_year(d, start_month=5):
    """Return the fiscal year integer for a given date.
    E.g. with start_month=5: 2026-01-15 → 2025,  2026-06-01 → 2026."""
    if d.month >= start_month:
        return d.year
    return d.year - 1


def fy_start(fy, start_month=5):
    """First day of the given fiscal year."""
    return date(fy, start_month, 1)


def fy_end(fy, start_month=5):
    """Last day of the given fiscal year."""
    import calendar
    end_month = start_month - 1 or 12
    end_year = fy + 1 if start_month > 1 else fy
    last_day = calendar.monthrange(end_year, end_month)[1]
    return date(end_year, end_month, last_day)

db = SQLAlchemy()


@event.listens_for(Engine, "connect")
def _set_sqlite_pragmas(dbapi_connection, connection_record):
    """Tune SQLite for the multi-worker Gunicorn setup.

    Guarded on the real sqlite3 connection type, so this is a no-op on any other
    backend (e.g. PostgreSQL). WAL lets readers and the single writer proceed
    concurrently instead of locking the whole DB, and busy_timeout makes a writer
    wait for a held lock rather than failing immediately with "database is locked".
    """
    if isinstance(dbapi_connection, sqlite3.Connection):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()


class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=True)   # legacy/admin only
    password_hash = db.Column(db.String(256), nullable=True)           # legacy/admin only
    display_name = db.Column(db.String(200))
    email = db.Column(db.String(200))
    ad_oid = db.Column(db.String(100))                                 # Azure AD Object ID
    role = db.Column(db.String(20), default='admin')                   # admin, employee, viewer
    active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    last_login_at = db.Column(db.DateTime)

    @property
    def is_admin(self):
        return self.role == 'admin'

class Settings(db.Model):
    """Key-value store for runtime settings (Fortnox tokens, etc.)"""
    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(100), unique=True, nullable=False)
    value = db.Column(db.Text)

    @classmethod
    def get(cls, key, default=None):
        row = cls.query.filter_by(key=key).first()
        if row is None:
            return default
        if key in _ENCRYPTED_KEYS and row.value:
            return _decrypt(row.value)
        return row.value

    @classmethod
    def set(cls, key, value):
        if key in _ENCRYPTED_KEYS and value:
            value = _encrypt(value)
        row = cls.query.filter_by(key=key).first()
        if row:
            row.value = value
        else:
            db.session.add(cls(key=key, value=value))
        db.session.commit()


class VoucherTemplate(db.Model):
    """Standard Fortnox account code configuration per transaction type."""
    __tablename__ = "voucher_template"
    id = db.Column(db.Integer, primary_key=True)
    transaction_type = db.Column(db.String(50), unique=True, nullable=False)
    # e.g. supplier_invoice, expense_internal, expense_external, mileage, salary
    debit_account = db.Column(db.String(10))
    debit_account_label = db.Column(db.String(100))
    vat_account = db.Column(db.String(10))
    vat_rate = db.Column(db.Float, default=25.0)
    credit_account = db.Column(db.String(10))
    cost_center = db.Column(db.String(50))
    voucher_series = db.Column(db.String(5))
    description_template = db.Column(db.String(200))


class ExpenseCategory(db.Model):
    """Configurable expense categories with debit account routing."""
    __tablename__ = "expense_category"
    id           = db.Column(db.Integer, primary_key=True)
    name         = db.Column(db.String(200), nullable=False)
    debit_account = db.Column(db.String(10), nullable=False)
    sort_order   = db.Column(db.Integer, default=0)
    active       = db.Column(db.Boolean, default=True)


class SupplierCategory(db.Model):
    """Configurable supplier invoice categories with debit account routing."""
    __tablename__ = "supplier_category"
    id            = db.Column(db.Integer, primary_key=True)
    name          = db.Column(db.String(200), nullable=False)
    debit_account = db.Column(db.String(10), nullable=False)
    sort_order    = db.Column(db.Integer, default=0)
    active        = db.Column(db.Boolean, default=True)


class InternalCode(db.Model):
    """An internal time/cost code — behaves like an assignment, but can never be invoiced.

    Sales, sick leave, vacation, admin: real hours and real costs that belong to the
    company rather than to a client. Entries booked against a code carry
    ``project_id = NULL``, and every invoicing query filters on ``project_id``, so
    internal work stays out of invoices by construction. ``billable`` is forced off
    on the way in as a second line of defence.

    Purely a reporting dimension — the code never changes account routing. Vouchers
    are built from the expense/supplier category exactly as they are for client work.
    """
    __tablename__ = "internal_code"
    id          = db.Column(db.Integer, primary_key=True)
    code        = db.Column(db.String(20), unique=True, nullable=False)   # e.g. SALES
    name        = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text)
    # Groups codes in reporting: absence is excluded from "internal work" totals.
    kind        = db.Column(db.String(20), default="other")   # sales, absence, admin, other
    track_time  = db.Column(db.Boolean, default=True)
    track_cost  = db.Column(db.Boolean, default=True)
    km_rate     = db.Column(db.Float)   # internal mileage rate; falls back to DEFAULT_KM_RATE
    sort_order  = db.Column(db.Integer, default=0)
    active      = db.Column(db.Boolean, default=True)
    created_at  = db.Column(db.DateTime, default=datetime.utcnow)

    time_entries    = db.relationship("TimeEntry", backref="internal_code", lazy=True)
    expenses        = db.relationship("Expense", backref="internal_code", lazy=True)
    mileage_entries = db.relationship("MileageEntry", backref="internal_code", lazy=True)
    supplier_invoices = db.relationship("SupplierInvoice", backref="internal_code", lazy=True)

    KINDS = ("sales", "absence", "admin", "other")

    @property
    def label(self):
        return f"{self.code} – {self.name}"

    def get_km_rate(self):
        return self.km_rate or DEFAULT_KM_RATE


class _TargetMixin:
    """Shared read helpers for rows that hang off either an assignment or an internal code.

    Exactly one of ``project_id`` / ``internal_code_id`` is set; the routes enforce
    that, and these helpers never assume which one it is.
    """

    @property
    def is_internal(self):
        return self.internal_code_id is not None

    @property
    def target_label(self):
        """Human label for whatever this row is booked against."""
        if self.internal_code_id and self.internal_code:
            return self.internal_code.label
        if self.project_id and self.project:
            return f"{self.project.client.name} / {self.project.name}"
        return "—"

    @property
    def target_key(self):
        """Value used by the combined assignment/internal-code <select>."""
        if self.internal_code_id:
            return f"i:{self.internal_code_id}"
        if self.project_id:
            return f"p:{self.project_id}"
        return ""


class Client(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), nullable=False)
    org_nr = db.Column(db.String(50))
    contact_name = db.Column(db.String(200))
    contact_email = db.Column(db.String(200))
    address = db.Column(db.Text)
    vat_nr = db.Column(db.String(50))
    hourly_rate = db.Column(db.Float, nullable=False, default=1500.0)
    km_rate = db.Column(db.Float, default=25.0)   # default mileage rate SEK/km
    currency = db.Column(db.String(10), default="SEK")
    payment_days = db.Column(db.Integer, default=30)
    invoice_language = db.Column(db.String(10), default="sv")  # sv or en
    fortnox_customer_nr = db.Column(db.String(50))
    active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    projects = db.relationship("Project", backref="client", lazy=True, cascade="all, delete-orphan")
    invoices = db.relationship("Invoice", backref="client", lazy=True)

class Project(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    client_id = db.Column(db.Integer, db.ForeignKey("client.id"), nullable=False)
    project_number = db.Column(db.String(20))   # e.g. P2601
    name = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text)
    start_date = db.Column(db.Date)
    end_date = db.Column(db.Date)
    active = db.Column(db.Boolean, default=True)
    accumulated_cost = db.Column(db.Float, default=0.0)  # pre-system historical cost
    invoice_cc_email = db.Column(db.String(200))  # contact person email, CC on invoice emails
    contact_person_name = db.Column(db.String(200))  # contact person name for invoice greeting
    expense_markup_pct = db.Column(db.Float, default=10.0)  # markup on vidarefakturerade kostnader
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    @classmethod
    def generate_number(cls, year=None):
        """Generate next project number for the given year, e.g. P2601, P2602."""
        if year is None:
            year = date.today().year
        yy = str(year)[-2:]
        prefix = f"P{yy}"
        existing = cls.query.filter(cls.project_number.like(f"{prefix}%")).all()
        max_seq = 0
        for p in existing:
            try:
                seq = int(p.project_number[len(prefix):])
                if seq > max_seq:
                    max_seq = seq
            except (ValueError, TypeError):
                pass
        return f"{prefix}{max_seq + 1:02d}"

    time_entries = db.relationship("TimeEntry", backref="project", lazy=True, cascade="all, delete-orphan")
    expenses = db.relationship("Expense", backref="project", lazy=True)
    mileage_entries = db.relationship("MileageEntry", backref="project", lazy=True, cascade="all, delete-orphan")
    purchase_orders = db.relationship(
        "PurchaseOrder", backref="project", lazy=True,
        cascade="all, delete-orphan",
        order_by="PurchaseOrder.valid_from",
    )
    supplier_invoices = db.relationship("SupplierInvoice", backref="project", lazy=True)

    def get_active_po(self, for_date=None):
        """Return the PO active on for_date (latest valid_from wins), or None."""
        if for_date is None:
            for_date = date.today()
        candidates = [
            po for po in self.purchase_orders
            if po.active
            and (po.valid_from is None or po.valid_from <= for_date)
            and (po.valid_to is None or po.valid_to >= for_date)
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda po: po.valid_from or date.min)

    def get_hourly_rate(self, for_date=None, hour_type=None):
        """Return the applicable hourly rate for the given date."""
        po = self.get_active_po(for_date)
        if po is None:
            return self.client.hourly_rate
        if hour_type is not None:
            matched = next(
                (ht for ht in po.hour_types
                 if ht.active and ht.name.lower() == hour_type.name.lower()),
                None,
            )
            if matched and matched.hourly_rate is not None:
                return matched.hourly_rate
        return po.hourly_rate

    @property
    def current_hourly_rate(self):
        return self.get_hourly_rate(date.today())

    def get_km_rate(self, for_date=None):
        """Return the applicable km rate for the given date."""
        po = self.get_active_po(for_date)
        if po is not None and po.km_rate is not None:
            return po.km_rate
        return self.client.km_rate or DEFAULT_KM_RATE


class PurchaseOrder(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey("project.id"), nullable=False)
    po_number = db.Column(db.String(100))
    description = db.Column(db.Text)
    po_amount = db.Column(db.Float, nullable=True)
    hourly_rate = db.Column(db.Float, nullable=False)
    km_rate = db.Column(db.Float, nullable=True)
    currency = db.Column(db.String(10), default="SEK")
    valid_from = db.Column(db.Date)
    valid_to = db.Column(db.Date)
    active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    hour_types = db.relationship(
        "POHourType", backref="po", lazy=True,
        cascade="all, delete-orphan",
        order_by="POHourType.sort_order",
    )


class POHourType(db.Model):
    __tablename__ = "po_hour_type"
    id = db.Column(db.Integer, primary_key=True)
    po_id = db.Column(db.Integer, db.ForeignKey("purchase_order.id"), nullable=False)
    name = db.Column(db.String(100), nullable=False)
    hourly_rate = db.Column(db.Float, nullable=True)
    billable = db.Column(db.Boolean, default=True)
    active = db.Column(db.Boolean, default=True)
    sort_order = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    time_entries = db.relationship("TimeEntry", back_populates="hour_type", lazy=True)


class TimeEntry(_TargetMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    # Exactly one of project_id / internal_code_id is set. project_id became
    # nullable when internal codes arrived — internal time has no assignment.
    project_id = db.Column(db.Integer, db.ForeignKey("project.id"), nullable=True)
    internal_code_id = db.Column(db.Integer, db.ForeignKey("internal_code.id"), nullable=True)
    entry_date = db.Column(db.Date, nullable=False, default=date.today)
    hours = db.Column(db.Float, nullable=False)
    time_type = db.Column(db.String(50), default="Normal")
    hour_type_id = db.Column(db.Integer, db.ForeignKey("po_hour_type.id"), nullable=True)
    hour_type = db.relationship("POHourType", back_populates="time_entries", lazy=True)
    description = db.Column(db.Text)
    invoiced = db.Column(db.Boolean, default=False)
    invoice_id = db.Column(db.Integer, db.ForeignKey("invoice.id"), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    @property
    def display_type(self):
        if self.internal_code_id:
            return self.internal_code.name if self.internal_code else (self.time_type or "Internt")
        return self.hour_type.name if self.hour_type_id and self.hour_type else (self.time_type or "Normal")

    @property
    def effective_rate(self):
        """Hourly rate this entry is worth. Internal time has no rate — it is never sold."""
        if self.internal_code_id or not self.project:
            return 0.0
        return self.project.get_hourly_rate(self.entry_date, self.hour_type)

class Expense(_TargetMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey("project.id"), nullable=True)
    internal_code_id = db.Column(db.Integer, db.ForeignKey("internal_code.id"), nullable=True)
    expense_date = db.Column(db.Date, nullable=False, default=date.today)
    merchant = db.Column(db.String(200))
    description = db.Column(db.Text)
    amount_excl_vat = db.Column(db.Float, nullable=False)
    vat_amount = db.Column(db.Float, default=0.0)
    vat_rate = db.Column(db.Float, default=25.0)
    # Omvänd skattskyldighet — no VAT on the receipt; 25% is self-assessed to 2614/2647
    reverse_charge = db.Column(db.Boolean, default=False)
    amount_incl_vat = db.Column(db.Float, nullable=False)
    currency = db.Column(db.String(10), default="SEK")
    billable = db.Column(db.Boolean, default=True)
    paid_by = db.Column(db.String(20), default="personal")  # personal or company
    receipt_filename = db.Column(db.String(300))
    expense_category_id = db.Column(db.Integer, db.ForeignKey("expense_category.id"), nullable=True)
    expense_category = db.relationship("ExpenseCategory", lazy=True)
    status = db.Column(db.String(20), default="pending")  # pending, approved, invoiced
    fortnox_voucher_nr = db.Column(db.String(50))
    invoice_id = db.Column(db.Integer, db.ForeignKey("invoice.id"), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    ocr_raw = db.Column(db.Text)

class Invoice(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    client_id = db.Column(db.Integer, db.ForeignKey("client.id"), nullable=False)
    project_id = db.Column(db.Integer, db.ForeignKey("project.id"), nullable=True)
    project = db.relationship("Project", backref="invoices", lazy=True)
    invoice_number = db.Column(db.String(50), unique=True)
    period_start = db.Column(db.Date, nullable=False)
    period_end = db.Column(db.Date, nullable=False)
    issue_date = db.Column(db.Date, default=date.today)
    due_date = db.Column(db.Date)
    language = db.Column(db.String(10), default="sv")

    subtotal = db.Column(db.Float, default=0.0)
    vat_amount = db.Column(db.Float, default=0.0)
    total = db.Column(db.Float, default=0.0)
    currency = db.Column(db.String(10), default="SEK")

    status = db.Column(db.String(20), default="draft")  # draft, approved, sent, paid
    fortnox_invoice_nr = db.Column(db.String(50))
    pdf_filename = db.Column(db.String(300))
    sent_at = db.Column(db.DateTime)
    notes = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    time_entries = db.relationship("TimeEntry", backref="invoice", lazy=True, foreign_keys=[TimeEntry.invoice_id])
    expenses = db.relationship("Expense", backref="invoice", lazy=True, foreign_keys=[Expense.invoice_id])
    mileage_entries = db.relationship("MileageEntry", backref="invoice", lazy=True, foreign_keys="MileageEntry.invoice_id")

    def generate_number(self, fy_start_month=5):
        """Assign the next sequential invoice number for the fiscal year.

        Uses MAX() on existing numbers so the sequence stays correct even if
        invoices are deleted. The unique constraint on invoice_number is the
        final guard against races — callers should catch IntegrityError and retry.
        """
        d = self.issue_date or date.today()
        fy = fiscal_year(d, fy_start_month)
        from sqlalchemy import func
        max_num = db.session.query(
            func.max(Invoice.invoice_number)
        ).filter(
            Invoice.invoice_number.like(f"{fy}-%")
        ).scalar()
        if max_num:
            try:
                seq = int(max_num.split("-", 1)[1]) + 1
            except (IndexError, ValueError):
                seq = 1
        else:
            seq = 1
        self.invoice_number = f"{fy}-{seq:03d}"

    def line_groups(self):
        groups = {}
        for e in self.time_entries:
            ht      = e.hour_type
            po      = ht.po if ht else None
            rate    = e.effective_rate
            yr      = e.entry_date.year
            mo      = e.entry_date.month
            ht_name = ht.name if ht else (e.time_type or "Normal")
            po_id   = po.id if po else None
            po_num  = (po.po_number or "") if po else ""
            key     = (yr, mo, po_id, ht_name, rate)
            if key not in groups:
                groups[key] = {
                    "year":           yr,
                    "month":          mo,
                    "po_number":      po_num,
                    "hour_type_name": ht_name,
                    "show_po":        False,
                    "hours":          0.0,
                    "rate":           rate,
                    "sort_key":       (yr, mo, po_num, ht.sort_order if ht else 99),
                }
            groups[key]["hours"] = round(groups[key]["hours"] + e.hours, 4)

        from collections import defaultdict
        po_per_bucket = defaultdict(set)
        for (yr, mo, po_id, ht_name, rate), g in groups.items():
            po_per_bucket[(yr, mo, ht_name)].add(po_id)
        for key, g in groups.items():
            yr, mo, po_id, ht_name, rate = key
            g["show_po"] = len(po_per_bucket[(yr, mo, ht_name)]) > 1

        result = sorted(groups.values(), key=lambda g: g["sort_key"])
        for g in result:
            g["amount"] = round(g["hours"] * g["rate"], 2)
        return result


class MileageEntry(_TargetMixin, db.Model):
    __tablename__ = "mileage_entry"
    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey("project.id"), nullable=True)
    internal_code_id = db.Column(db.Integer, db.ForeignKey("internal_code.id"), nullable=True)
    entry_date = db.Column(db.Date, nullable=False, default=date.today)
    km = db.Column(db.Float, nullable=False)
    km_rate = db.Column(db.Float, nullable=False)
    description = db.Column(db.Text)
    billable = db.Column(db.Boolean, default=True)
    status = db.Column(db.String(20), default="approved")  # approved, invoiced
    invoice_id = db.Column(db.Integer, db.ForeignKey("invoice.id"), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    @property
    def amount(self):
        return round(self.km * self.km_rate, 2)


class PaymentFile(db.Model):
    """A generated Bankgirot LB payment file covering one or more supplier invoices."""
    __tablename__ = "payment_file"
    id = db.Column(db.Integer, primary_key=True)
    filename = db.Column(db.String(300))
    payment_date = db.Column(db.Date)
    total_amount = db.Column(db.Float, default=0.0)
    invoice_count = db.Column(db.Integer, default=0)
    status = db.Column(db.String(20), default='generated')  # generated, confirmed
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    confirmed_at = db.Column(db.DateTime)

    supplier_invoices = db.relationship("SupplierInvoice", backref="payment_file", lazy=True)
    payslips          = db.relationship("Payslip", backref="payment_file", lazy=True)
    agi_reports       = db.relationship("AgiReport", backref="payment_file", lazy=True)


class SupplierInvoice(_TargetMixin, db.Model):
    """An incoming invoice from a supplier, managed through upload → OCR → payment flow."""
    __tablename__ = "supplier_invoice"
    id = db.Column(db.Integer, primary_key=True)
    supplier_name = db.Column(db.String(200))
    supplier_org_nr = db.Column(db.String(20))
    invoice_date = db.Column(db.Date)
    due_date = db.Column(db.Date)
    amount_excl_vat = db.Column(db.Float, default=0.0)
    vat_amount = db.Column(db.Float, default=0.0)
    amount_incl_vat = db.Column(db.Float, default=0.0)
    currency = db.Column(db.String(10), default='SEK')
    payment_ref          = db.Column(db.String(100))  # OCR/reference number
    payment_account      = db.Column(db.String(100))  # BG/PG/IBAN account number
    payment_account_type = db.Column(db.String(10))   # 'bg', 'pg', 'iban'
    account_code = db.Column(db.String(10))
    supplier_category_id = db.Column(db.Integer, db.ForeignKey('supplier_category.id'), nullable=True)
    supplier_category = db.relationship('SupplierCategory', lazy=True)
    vat_rate = db.Column(db.Float, default=25.0)
    # Omvänd skattskyldighet — invoice carries no VAT; the buyer self-assesses it.
    # reverse_charge stays as the "no VAT on the invoice" flag the amount checks and
    # the payment flow read; vat_treatment picks which accounts the voucher uses.
    reverse_charge = db.Column(db.Boolean, default=False)
    vat_treatment = db.Column(db.String(40), default=VAT_DOMESTIC)
    # Reverse charge EU services: the separately specified service/repair part of
    # the net amount, taxed at 12% instead of 25%.
    service_amount_excl_vat = db.Column(db.Float, default=0.0)
    project_id = db.Column(db.Integer, db.ForeignKey('project.id'), nullable=True)
    internal_code_id = db.Column(db.Integer, db.ForeignKey('internal_code.id'), nullable=True)
    pdf_filename = db.Column(db.String(300))
    status = db.Column(db.String(20), default='pending')  # pending, approved, booked, paid
    fortnox_voucher_nr = db.Column(db.String(50))
    payment_file_id = db.Column(db.Integer, db.ForeignKey('payment_file.id'), nullable=True)
    ocr_raw = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    @property
    def days_until_due(self):
        if self.due_date:
            return (self.due_date - date.today()).days
        return None

    @property
    def payment_destination(self):
        if self.payment_account and self.payment_account_type:
            label = {"bg": "BG", "pg": "PG", "iban": "IBAN"}.get(self.payment_account_type, "")
            return f"{label} {self.payment_account}".strip()
        return "—"


# ── Payroll (lönebesked) ─────────────────────────────────────────────────────

class AgiFieldCode(db.Model):
    """A Skatteverket AGI field (fältkod), e.g. 011 Kontant ersättning.

    The "redovisningstyp" a löneart reports to. Seeded from schema 1.1 and editable,
    so a new or retired field can be followed without a code change. Only fields
    flagged ``selectable`` can be picked on a löneart — the structural ones
    (period, spec.nr, personnummer, HU totals) are written by the generator itself.
    """
    __tablename__ = "agi_field_code"
    id          = db.Column(db.Integer, primary_key=True)
    code        = db.Column(db.String(3), unique=True, nullable=False)   # "011"
    element     = db.Column(db.String(100), nullable=False)              # KontantErsattningUlagAG
    value_type  = db.Column(db.String(20), default="amount")             # amount, checkbox, text
    description = db.Column(db.String(300))
    selectable  = db.Column(db.Boolean, default=True)
    # Counts toward the arbetsgivaravgift base (FK011/012/013/018 ...).
    ag_base     = db.Column(db.Boolean, default=False)
    active      = db.Column(db.Boolean, default=True)

    @property
    def label(self):
        return f"{self.code} – {self.description or self.element}"


class SalaryType(db.Model):
    """A löneart — a line type on the lönebesked.

    ``kind`` decides the sign and how the line is booked (see payroll.KINDS);
    ``agi_field`` decides which Skatteverket box it lands in. An empty
    ``debit_account`` on a cash line falls back to the employee's salary account,
    which is how 7220 (företagsledare) and 7210 (tjänstemän) share one löneart.
    """
    __tablename__ = "salary_type"
    id              = db.Column(db.Integer, primary_key=True)
    code            = db.Column(db.String(10), unique=True, nullable=False)   # "100"
    name            = db.Column(db.String(200), nullable=False)
    kind            = db.Column(db.String(20), nullable=False, default="cash")
    agi_field_id    = db.Column(db.Integer, db.ForeignKey("agi_field_code.id"), nullable=True)
    agi_field       = db.relationship("AgiFieldCode", lazy=True)
    debit_account   = db.Column(db.String(10))
    credit_account  = db.Column(db.String(10))
    default_rate    = db.Column(db.Float)
    unit            = db.Column(db.String(20))      # st, dagar, mil, tim
    sort_order      = db.Column(db.Integer, default=0)
    active          = db.Column(db.Boolean, default=True)

    @property
    def label(self):
        return f"{self.code} {self.name}"


class Employee(db.Model):
    __tablename__ = "employee"
    id              = db.Column(db.Integer, primary_key=True)
    first_name      = db.Column(db.String(100), nullable=False)
    last_name       = db.Column(db.String(100), nullable=False)
    personnummer    = db.Column(db.String(12), nullable=False)    # 12 digits, ÅÅÅÅMMDDNNNN
    address         = db.Column(db.String(200))
    postal_code     = db.Column(db.String(10))
    city            = db.Column(db.String(100))
    email           = db.Column(db.String(200))                   # default payslip recipient
    bank_clearing   = db.Column(db.String(10))
    bank_account    = db.Column(db.String(30))
    salary_account  = db.Column(db.String(10), default="7210")    # 7220 for företagsledare
    ag_rate         = db.Column(db.Float, default=0.3142)          # arbetsgivaravgift
    tax_table       = db.Column(db.String(20))                     # Skatteverket's table number, e.g. "34"
    tax_column      = db.Column(db.Integer, default=1)             # column 1–6 in the table
    active          = db.Column(db.Boolean, default=True)
    created_at      = db.Column(db.DateTime, default=datetime.utcnow)

    payslips = db.relationship("Payslip", backref="employee", lazy=True)

    @property
    def name(self):
        return f"{self.first_name} {self.last_name}"

    @property
    def personnummer_display(self):
        p = self.personnummer or ""
        return f"{p[2:8]}-{p[8:]}" if len(p) == 12 else p

    @property
    def bank_display(self):
        if not self.bank_account:
            return "—"
        return f"{self.bank_clearing}-{self.bank_account}" if self.bank_clearing else self.bank_account


class TaxTableRow(db.Model):
    """One income bracket of Skatteverket's monthly tax table (skattetabell för månadslön).

    kind "B": the columns hold the tax in kronor. kind "%": the columns hold a
    percentage of the income (the top brackets). ``income_to`` is NULL on the last row.
    """
    __tablename__ = "tax_table_row"
    id              = db.Column(db.Integer, primary_key=True)
    year            = db.Column(db.Integer, nullable=False)
    table_nr        = db.Column(db.Integer, nullable=False)
    kind            = db.Column(db.String(1), nullable=False, default="B")
    income_from     = db.Column(db.Integer, nullable=False)
    income_to       = db.Column(db.Integer)
    col1            = db.Column(db.Integer, default=0)
    col2            = db.Column(db.Integer, default=0)
    col3            = db.Column(db.Integer, default=0)
    col4            = db.Column(db.Integer, default=0)
    col5            = db.Column(db.Integer, default=0)
    col6            = db.Column(db.Integer, default=0)
    fetched_at      = db.Column(db.DateTime, default=datetime.utcnow)

    __table_args__ = (db.Index("ix_tax_table_lookup", "year", "table_nr", "income_from"),)


class Payslip(db.Model):
    """One lönebesked: one employee, one payout.

    draft → approved (registered for payment, locked) → paid (salary voucher booked).
    The AGI period is the month of ``pay_date`` — Skatteverket reports per payout month.
    """
    __tablename__ = "payslip"
    id              = db.Column(db.Integer, primary_key=True)
    employee_id     = db.Column(db.Integer, db.ForeignKey("employee.id"), nullable=False)
    period          = db.Column(db.String(7), nullable=False)      # "2026-06", avser perioden
    pay_date        = db.Column(db.Date, nullable=False)
    spec_nr         = db.Column(db.Integer, default=1)             # FK570, unique per person and AGI period
    note            = db.Column(db.Text)
    status          = db.Column(db.String(20), default="draft")    # draft, approved, paid, historical (sideloaded)
    payment_file_id = db.Column(db.Integer, db.ForeignKey("payment_file.id"), nullable=True)
    voucher_ref     = db.Column(db.String(50))
    paid_at         = db.Column(db.DateTime)
    emailed_to      = db.Column(db.String(200))
    emailed_at      = db.Column(db.DateTime)
    created_at      = db.Column(db.DateTime, default=datetime.utcnow)

    lines = db.relationship("PayslipLine", backref="payslip", lazy=True,
                            cascade="all, delete-orphan", order_by="PayslipLine.sort_order")

    @property
    def agi_period(self):
        return self.pay_date.strftime("%Y%m") if self.pay_date else None

    @property
    def is_locked(self):
        return self.status != "draft"


class PayslipLine(db.Model):
    __tablename__ = "payslip_line"
    id              = db.Column(db.Integer, primary_key=True)
    payslip_id      = db.Column(db.Integer, db.ForeignKey("payslip.id"), nullable=False)
    salary_type_id  = db.Column(db.Integer, db.ForeignKey("salary_type.id"), nullable=False)
    salary_type     = db.relationship("SalaryType", lazy=True)
    description     = db.Column(db.String(200))
    quantity        = db.Column(db.Float)
    rate            = db.Column(db.Float)
    amount          = db.Column(db.Float, default=0.0)     # always entered positive; kind gives the sign
    sort_order      = db.Column(db.Integer, default=0)


class AgiReport(db.Model):
    """The arbetsgivardeklaration for one period and its tax payment.

    open → generated (XML downloaded) → submitted (import acknowledged) →
    approved (Skatteverket's amount and date entered, registered for payment) →
    paid. Paying books two vouchers: bank → skattekonto on the payment date
    (voucher_ref), and skattekonto → 2710/2731 on the tax date (settle_voucher_ref).
    """
    __tablename__ = "agi_report"
    id              = db.Column(db.Integer, primary_key=True)
    period          = db.Column(db.String(6), unique=True, nullable=False)   # "202606"
    status          = db.Column(db.String(20), default="open")
    xml_filename    = db.Column(db.String(200))
    generated_at    = db.Column(db.DateTime)
    submitted_at    = db.Column(db.DateTime)
    sum_ag_fees     = db.Column(db.Integer)       # FK487 as filed
    sum_tax         = db.Column(db.Integer)       # FK497 as filed
    skv_amount      = db.Column(db.Float)         # what Skatteverket says to pay
    skv_date        = db.Column(db.Date)          # payment date (bank → 1630)
    skv_debit_date  = db.Column(db.Date)          # tax date, Skatteverket debits 1630 (normally the 12th)
    skv_ocr         = db.Column(db.String(30))
    payment_file_id = db.Column(db.Integer, db.ForeignKey("payment_file.id"), nullable=True)
    voucher_ref     = db.Column(db.String(50))    # payment: 1930 → 1630
    settle_voucher_ref = db.Column(db.String(50)) # tax date: 1630 → 2710/2731
    paid_at         = db.Column(db.DateTime)
    created_at      = db.Column(db.DateTime, default=datetime.utcnow)

    @property
    def period_display(self):
        return f"{self.period[:4]}-{self.period[4:]}"
