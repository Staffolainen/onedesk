# onedesk

Self-hosted Flask app for solo consultant administration — time tracking, expenses with receipt OCR, mileage reimbursement, and invoicing.

## Features

- **Time tracking** — week matrix view, log hours by assignment and PO hour type, monthly summary
- **Expenses** — mobile camera capture, Claude Vision OCR extracts amount/VAT/merchant, review and approve
- **Mileage** — log km per assignment, rate resolved automatically from PO or client default
- **Invoicing** — per-assignment invoices, line items grouped by month/hour-type, PDF generation, email delivery
- **Budget tracking** — dashboard budget bars show historical cost, invoiced, uninvoiced, and PO headroom per assignment
- **Bilingual** — Swedish/English switchable UI and invoice language per client
- **Dark/light theme** — toggle with ◐, persisted in browser

---

## Quick Start

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Configure environment

```bash
cp .env.example .env
```

Edit `.env`:

| Variable | Description |
|---|---|
| `SECRET_KEY` | Long random string for Flask sessions |
| `ADMIN_PASSWORD` | Your login password |
| `COMPANY_NAME` | Your company name (appears on invoices) |
| `COMPANY_ORG_NR` | Swedish org number |
| `COMPANY_BANKGIRO` | Bankgiro for payment info |
| `COMPANY_VAT_NR` | VAT number (Momsreg.nr) |
| `ANTHROPIC_API_KEY` | From console.anthropic.com (for receipt OCR) |
| `SMTP_HOST` / `SMTP_USER` / `SMTP_PASSWORD` | For sending invoices by email |
| `FY_START_MONTH` | Fiscal year start month (default: 5 = May) |
| `FLASK_DEBUG` | Set to `1` for development only |
| `SESSION_COOKIE_SECURE` | Set to `1` when running behind HTTPS |

### 3. Add your logo

Place your company logo at `static/img/logo.png`.
Recommended: PNG with transparent background, max 400×120px.

### 4. Run

```bash
python run.py
```

Open [http://localhost:5000](http://localhost:5000) and log in with your `ADMIN_PASSWORD`.

---

## Docker

```bash
docker compose up -d
```

The database and uploads are mounted as a volume so they survive container restarts.

---

## Production Deployment (Azure App Service)

See **[Design & Architecture](#design--architecture)** section below for full Azure setup.

Quick steps:
1. Push image to Azure Container Registry
2. Create App Service (Linux, B1)
3. Mount Azure Files share at `/app/instance` for persistent DB + uploads
4. Set all env vars as App Settings
5. Enable managed SSL certificate

---

## Workflow

### Time tracking
1. Go to **Tidrapportering**
2. Enter hours directly in the weekly matrix
3. Select PO hour type per row (Normal, Restid, etc.)
4. Hit ↵ to save a row — rows are locked once the invoice is approved

### Expenses
1. On mobile: **Utlägg** → **📷 Nytt utlägg** → take a photo
2. Claude Vision extracts merchant, date, amount and VAT automatically
3. Review, correct if needed, approve

### Mileage
1. Go to **Reseersättning** → fill in date, assignment and km
2. Rate is resolved automatically: PO rate → client default → 25 SEK/km

### Invoicing
1. **Fakturor** → **Ny faktura** → select assignment and period
2. All uninvoiced hours + approved billable expenses + mileage are included
3. Review the proforma — approve to lock entries and generate PDF
4. Send by email

---

## Running Tests

```bash
pytest
```

Tests use an in-memory SQLite database and cover auth, model logic, time saving, invoice creation/locking, and security controls.

---

## File Structure

```
consultadmin/
├── app.py              # Flask app — all routes and business logic
├── models.py           # SQLAlchemy models + Invoice.line_groups()
├── config.py           # Configuration loaded from .env
├── pdf_generator.py    # ReportLab invoice PDF generation
├── receipt_ocr.py      # Claude Vision / Ollama receipt OCR
├── fortnox.py          # Fortnox REST API client (dry-run mode)
├── run.py              # Dev server entry point
├── requirements.txt
├── pytest.ini
├── tests/              # Test suite
├── static/
│   ├── img/logo.png    ← Put your logo here
│   └── uploads/        ← Receipts and generated PDFs
└── templates/
    ├── base.html       ← Nav, theme, CSRF meta tag
    ├── dashboard.html
    ├── auth/
    ├── time/
    ├── expenses/
    ├── invoices/
    ├── mileage/
    ├── clients/
    └── settings.html
```

---

---

# Design & Architecture

## Overview

onedesk is a single-user Flask web application for a Swedish sole-consultant business. It replaces a combination of spreadsheets and manual Fortnox data entry with a purpose-built tool optimised for the consultant workflow: log time → review monthly totals → create per-assignment invoices → send to client.

The application is intentionally simple: one admin user, SQLite database, no background workers, no message queues. Complexity is kept low so that a single developer can maintain it.

---

## System Architecture

```
Browser
  │
  ▼
Azure App Service (Linux container)
  │
  ├── Gunicorn (WSGI)
  │     └── Flask app (app.py)
  │           ├── Flask-Login      (session auth)
  │           ├── Flask-WTF        (CSRF)
  │           ├── Flask-Limiter    (rate limiting)
  │           └── SQLAlchemy       (ORM)
  │
  ├── SQLite database  ──► Azure Files mount (/app/instance/)
  ├── File uploads     ──► Azure Files mount (/app/instance/uploads/)
  │
  └── External services
        ├── Anthropic API     (receipt OCR via Claude Vision)
        ├── SMTP              (invoice email delivery)
        └── Fortnox API       (accounting — currently dry-run)
```

---

## Data Model

```
Client
  ├── hourly_rate, km_rate, payment_days, invoice_language
  └── Projects (1:many)
        ├── accumulated_cost    (pre-system historical cost for budget bars)
        └── PurchaseOrders (1:many)
              ├── po_amount, hourly_rate, km_rate
              ├── valid_from / valid_to
              └── POHourTypes (1:many)
                    └── name, hourly_rate, billable, sort_order

TimeEntry          (project, date, hours, hour_type, invoiced, invoice_id)
Expense            (project, date, merchant, amount_incl_vat, vat_rate, receipt_filename, status)
MileageEntry       (project, date, km, km_rate, status)

Invoice
  ├── client_id, project_id      (one invoice per assignment)
  ├── period_start / period_end
  ├── status: draft → approved → sent → paid
  ├── TimeEntries (1:many)
  ├── Expenses    (1:many)
  └── MileageEntries (1:many)
```

### Key design decisions

**Per-assignment invoices** — each invoice is linked to one `Project`, not a `Client`. A client with multiple active assignments gets separate invoices. This matches how POs work in Swedish consulting.

**Rate resolution chain** — hourly rate is resolved at time-of-save:
`POHourType.hourly_rate` → `PurchaseOrder.hourly_rate` → `Client.hourly_rate`

Same chain for km rate: `PurchaseOrder.km_rate` → `Client.km_rate` → 25.0 SEK/km

**Invoice line grouping** (`Invoice.line_groups()`) — time entries are grouped by `(year, month, po_id, hour_type_name, rate)`. The primary label is `YYYY MMM  HourType`. The PO number is added in parentheses only when the same month+hour_type spans multiple POs — i.e. disambiguation on demand.

**Entry locking** — when an invoice is approved, all linked `TimeEntry.invoiced`, `Expense.status`, and `MileageEntry.status` are set to locked. Locked entries are read-only in the time matrix.

**Fiscal year** — configurable start month (default May). Invoice numbers are `YYYY-NNN` where YYYY is the fiscal year. March 2026 with FY_START_MONTH=5 → invoice number `2025-NNN`.

---

## Security

| Control | Implementation |
|---|---|
| Authentication | Single admin user, bcrypt password hash via Werkzeug |
| Session | `SESSION_COOKIE_HTTPONLY`, `SESSION_COOKIE_SAMESITE=Lax`, `SECURE` via env var |
| CSRF | `Flask-WTF CSRFProtect` on all forms; JS meta-tag injection for form-attribute pattern |
| Rate limiting | `Flask-Limiter` — 10/min, 30/hour on `/login` |
| Security headers | `X-Frame-Options: DENY`, `X-Content-Type-Options`, `CSP`, `Referrer-Policy` |
| Upload access | Receipts and PDFs served through `@login_required` route with path-traversal guard |
| Input validation | `_safe_float()`, `_safe_int()`, `_safe_date()` helpers on all form inputs |
| Email injection | `_sanitize_header()` strips `\r\n` from all SMTP header values |
| Debug mode | Off by default; enabled only via `FLASK_DEBUG=1` env var |

---

## Invoice PDF Layout

Generated with **ReportLab** (`pdf_generator.py`).

Two-frame layout per page:
- **Main frame** — invoice header, line items, timesheet detail
- **Totals frame** — pinned above footer, height `TOTALS_H = 50mm`
- **Footer** — company details, org number, VAT number, bankgiro

Line items show: `YYYY MMM [PO]  HourType | qty h | rate SEK | amount SEK`

Subtotal → VAT (25%) → Öresavrundning → **Total**

---

## Deployment (Azure App Service)

### Infrastructure

```
Resource Group: onedesk-rg (swedencentral)
├── Container Registry (Basic)   — stores Docker image
├── Storage Account              — Azure Files share for DB + uploads
│     └── File Share: onedesk-data  mounted at /app/instance
└── App Service Plan (B1 Linux)
      └── Web App                — runs the Docker container
```

### Environment variables (App Settings)

```
SECRET_KEY                (generated, min 32 chars)
ADMIN_PASSWORD            (strong password)
FLASK_DEBUG               0
SESSION_COOKIE_SECURE     1
DATABASE_URL              sqlite:////app/instance/onedesk.db
COMPANY_NAME
COMPANY_ORG_NR
COMPANY_BANKGIRO
COMPANY_VAT_NR
COMPANY_EMAIL
ANTHROPIC_API_KEY
SMTP_HOST / SMTP_PORT / SMTP_USER / SMTP_PASSWORD / SMTP_FROM
FY_START_MONTH            5
WEBSITES_PORT             5000
```

### Deploy update

```bash
docker build -t onedeskregistry.azurecr.io/onedesk:latest .
docker push onedeskregistry.azurecr.io/onedesk:latest
az webapp restart --name onedesk-app --resource-group onedesk-rg
```

### Backup

The database lives on the Azure Files share. Backup by downloading the file:

```bash
az storage file download --account-name onedeskstorage \
  --share-name onedesk-data --path onedesk.db \
  --dest ./backups/onedesk-$(date +%Y%m%d).db \
  --account-key "<key>"
```

Automate with a daily Azure Logic App or a cron job on any machine with the Azure CLI.

---

## Known Limitations & Future Work

| Area | Current state | Potential improvement |
|---|---|---|
| Fortnox | Dry-run (logs payload only) | Enable live OAuth flow when credentials are configured |
| Multi-user | Single admin user | Add role-based access if team grows |
| Database | SQLite | Migrate to Azure SQL / Postgres if concurrent writes become an issue |
| Backup | Manual | Automate daily download via Azure Logic App |
| OCR fallback | Ollama (local) or Anthropic | Works offline with Ollama if `OLLAMA_MODEL` is set |
| Mobile UX | Responsive but not native | PWA manifest could improve mobile experience |
