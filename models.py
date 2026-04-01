from datetime import datetime, date
from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin


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
    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)

class Settings(db.Model):
    """Key-value store for runtime settings (Fortnox tokens, etc.)"""
    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(100), unique=True, nullable=False)
    value = db.Column(db.Text)

    @classmethod
    def get(cls, key, default=None):
        row = cls.query.filter_by(key=key).first()
        return row.value if row else default

    @classmethod
    def set(cls, key, value):
        row = cls.query.filter_by(key=key).first()
        if row:
            row.value = value
        else:
            db.session.add(cls(key=key, value=value))
        db.session.commit()

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
        """Return the applicable hourly rate for the given date.
        Finds the PO active on for_date, then:
          1. Matches by hour type name within that PO — uses its rate if set.
          2. Falls back to the active PO's base hourly_rate.
          3. Falls back to the client's default rate if no PO applies."""
        po = self.get_active_po(for_date)
        if po is None:
            return self.client.hourly_rate
        if hour_type is not None:
            # Find a matching hour type by name in the active PO
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
        """Return the applicable km rate for the given date.
        Uses the active PO's km_rate if set, else falls back to client default."""
        po = self.get_active_po(for_date)
        if po is not None and po.km_rate is not None:
            return po.km_rate
        return self.client.km_rate or 25.0


class PurchaseOrder(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey("project.id"), nullable=False)
    po_number = db.Column(db.String(100))       # PO reference / number from customer
    description = db.Column(db.Text)
    po_amount = db.Column(db.Float, nullable=True)  # Total PO budget (excl. VAT), None = open
    hourly_rate = db.Column(db.Float, nullable=False)
    km_rate = db.Column(db.Float, nullable=True)   # None = inherit from client
    currency = db.Column(db.String(10), default="SEK")
    valid_from = db.Column(db.Date)             # None = from the beginning
    valid_to = db.Column(db.Date)               # None = no expiry
    active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    hour_types = db.relationship(
        "POHourType", backref="po", lazy=True,
        cascade="all, delete-orphan",
        order_by="POHourType.sort_order",
    )


class POHourType(db.Model):
    """A billable (or non-billable) hour category tied to a specific PO.
    Internal categories like sick leave are handled separately and are never
    linked to a PO."""
    __tablename__ = "po_hour_type"
    id = db.Column(db.Integer, primary_key=True)
    po_id = db.Column(db.Integer, db.ForeignKey("purchase_order.id"), nullable=False)
    name = db.Column(db.String(100), nullable=False)
    hourly_rate = db.Column(db.Float, nullable=True)  # None = inherit PO rate
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
    time_type = db.Column(db.String(50), default="Normal")  # used for internal types (Sjuk etc.)
    hour_type_id = db.Column(db.Integer, db.ForeignKey("po_hour_type.id"), nullable=True)
    hour_type = db.relationship("POHourType", back_populates="time_entries", lazy=True)
    description = db.Column(db.Text)
    invoiced = db.Column(db.Boolean, default=False)
    invoice_id = db.Column(db.Integer, db.ForeignKey("invoice.id"), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    @property
    def display_type(self):
        """Human-readable time type name regardless of whether it's PO-linked or internal."""
        return self.hour_type.name if self.hour_type_id and self.hour_type else (self.time_type or "Normal")

    @property
    def effective_rate(self):
        """Hourly rate resolved from the PO active on entry_date.
        Matches by hour type name in that PO, falls back to PO base rate,
        then client default. This ensures December hours always use the
        December PO's rates regardless of which PO the hour type belongs to."""
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
    status = db.Column(db.String(20), default="pending")  # pending, approved, invoiced
    fortnox_voucher_nr = db.Column(db.String(50))
    invoice_id = db.Column(db.Integer, db.ForeignKey("invoice.id"), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    # OCR extracted data stored raw
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

    # Amounts
    subtotal = db.Column(db.Float, default=0.0)
    vat_amount = db.Column(db.Float, default=0.0)
    total = db.Column(db.Float, default=0.0)
    currency = db.Column(db.String(10), default="SEK")

    # State
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
        """Generate invoice number based on fiscal year, e.g. 2025-001.
        FY2025 = May 2025 – Apr 2026, so Jan 2026 invoices are numbered 2025-NNN."""
        d = self.issue_date or date.today()
        fy = fiscal_year(d, fy_start_month)
        count = Invoice.query.filter(
            Invoice.invoice_number.like(f"{fy}-%")
        ).count() + 1
        self.invoice_number = f"{fy}-{count:03d}"

    def line_groups(self):
        """Group time entries by (year, month, po_id, hour_type_name, rate).
        Primary label is 'YYYY MMM [hour_type]'. PO number is added in
        parentheses only when multiple POs fall in the same (year, month,
        hour_type_name) bucket — i.e. only when disambiguation is needed.
        Returns a list of dicts sorted by (year, month, po_number, sort_order)."""
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
                    "show_po":        False,   # resolved below
                    "hours":          0.0,
                    "rate":           rate,
                    "sort_key":       (yr, mo, po_num, ht.sort_order if ht else 99),
                }
            groups[key]["hours"] = round(groups[key]["hours"] + e.hours, 4)

        # Determine which (year, month, hour_type_name) buckets have >1 distinct PO
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
    """Mileage reimbursement (Reseersättning) entry."""
    __tablename__ = "mileage_entry"
    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey("project.id"), nullable=True)
    entry_date = db.Column(db.Date, nullable=False, default=date.today)
    km = db.Column(db.Float, nullable=False)
    km_rate = db.Column(db.Float, nullable=False)   # rate resolved and stored at entry time
    description = db.Column(db.Text)
    billable = db.Column(db.Boolean, default=True)
    status = db.Column(db.String(20), default="approved")  # approved, invoiced
    invoice_id = db.Column(db.Integer, db.ForeignKey("invoice.id"), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    @property
    def amount(self):
        return round(self.km * self.km_rate, 2)
