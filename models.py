import os
import base64
import hashlib
from datetime import datetime, date
from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin

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
    invoice_cc_email = db.Column(db.String(200))  # CC on invoice emails (e.g. client PM)
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
        return self.client.km_rate or 25.0


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


class TimeEntry(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey("project.id"), nullable=False)
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
        return self.hour_type.name if self.hour_type_id and self.hour_type else (self.time_type or "Normal")

    @property
    def effective_rate(self):
        return self.project.get_hourly_rate(self.entry_date, self.hour_type)

class Expense(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey("project.id"), nullable=True)
    expense_date = db.Column(db.Date, nullable=False, default=date.today)
    merchant = db.Column(db.String(200))
    description = db.Column(db.Text)
    amount_excl_vat = db.Column(db.Float, nullable=False)
    vat_amount = db.Column(db.Float, default=0.0)
    vat_rate = db.Column(db.Float, default=25.0)
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


class MileageEntry(db.Model):
    __tablename__ = "mileage_entry"
    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey("project.id"), nullable=True)
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


class SupplierInvoice(db.Model):
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
    project_id = db.Column(db.Integer, db.ForeignKey('project.id'), nullable=True)
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
