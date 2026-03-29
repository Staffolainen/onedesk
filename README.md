# ConsultAdmin

Self-hosted Flask app for consultant administration — time tracking, expenses with receipt OCR, and invoicing with Fortnox integration.

## Features

- **Time tracking** — week view, log hours by client/project, monthly summary
- **Expenses** — mobile camera capture, Claude Vision OCR extracts amount/VAT/merchant, approve and sync to Fortnox with receipt attachment
- **Invoicing** — monthly proforma with timesheet breakdown, PDF generation, email delivery, Fortnox invoice creation
- **Bilingual** — Swedish/English switchable UI and invoices
- **Fortnox integration** — OAuth2, invoices, vouchers, receipt uploads

---

## Quick Start

### 1. Install dependencies

```bash
cd consultadmin
pip install -r requirements.txt
```

WeasyPrint requires system libraries. On Ubuntu/Debian:
```bash
sudo apt-get install python3-cffi python3-brotli libpango-1.0-0 libpangoft2-1.0-0
```

On macOS:
```bash
brew install pango
```

### 2. Configure environment

```bash
cp .env.example .env
```

Edit `.env` with your details:

| Variable | Description |
|---|---|
| `SECRET_KEY` | Long random string for Flask sessions |
| `ADMIN_PASSWORD` | Your login password |
| `COMPANY_NAME` | Your company name (on invoices) |
| `COMPANY_ORG_NR` | Swedish org number |
| `COMPANY_BANKGIRO` | Bankgiro for payment info |
| `ANTHROPIC_API_KEY` | From console.anthropic.com (for receipt OCR) |
| `FORTNOX_CLIENT_ID` | From Fortnox developer portal |
| `FORTNOX_CLIENT_SECRET` | From Fortnox developer portal |
| `SMTP_HOST` / `SMTP_USER` / `SMTP_PASSWORD` | For sending invoices by email |

### 3. Add your logo

Place your company logo at:
```
static/img/logo.png
```
Recommended: PNG with transparent background, max 400×120px.

### 4. Run

```bash
python run.py
```

Open [http://localhost:5000](http://localhost:5000) and log in with your `ADMIN_PASSWORD`.

---

## Fortnox Setup

1. Log in to Fortnox → **Inställningar** → **API-anslutningar**
2. Create a new integration with scopes: `invoice`, `payment`, `bookkeeping`
3. Set redirect URI to: `http://localhost:5000/fortnox/callback`
4. Copy **Client ID** and **Client Secret** to `.env`
5. In ConsultAdmin → **Settings** → click **Anslut Fortnox**
6. Approve the OAuth flow — tokens are stored in the database

---

## Workflow

### Time tracking
1. Go to **Tidrapportering** (Time)
2. Click **+** on any day to log hours
3. Select client/project, enter hours and description
4. View monthly summaries under **Månadsvy**

### Expenses
1. On mobile: go to **Utlägg** → **Ta bild på kvitto**
2. Take a photo — Claude Vision extracts all data automatically
3. Review the extracted data, correct if needed, approve
4. Expense is saved and a voucher is created in Fortnox

### Invoicing
1. Go to **Fakturor** → **Ny faktura**
2. Select client and billing period
3. All uninvoiced hours + approved billable expenses are included automatically
4. Review the proforma — approve to generate PDF
5. Send by email (SMTP) — invoice is simultaneously posted to Fortnox

---

## Production Deployment

For production, use Gunicorn behind Nginx:

```bash
# Install
pip install gunicorn

# Run
gunicorn -w 2 -b 0.0.0.0:5000 app:app
```

Nginx config snippet:
```nginx
server {
    listen 80;
    server_name yourdomain.com;

    location / {
        proxy_pass http://127.0.0.1:5000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }

    location /static {
        alias /path/to/consultadmin/static;
    }

    client_max_body_size 16M;
}
```

For HTTPS, use Certbot:
```bash
sudo certbot --nginx -d yourdomain.com
```

---

## Database

SQLite database is stored at `consultadmin.db` in the project root. Back it up regularly:
```bash
cp consultadmin.db consultadmin.db.backup
```

---

## File Structure

```
consultadmin/
├── app.py              # Flask app, all routes
├── models.py           # SQLAlchemy models
├── config.py           # Configuration from .env
├── fortnox.py          # Fortnox REST API client
├── receipt_ocr.py      # Claude Vision receipt extraction
├── pdf_generator.py    # WeasyPrint invoice + timesheet PDF
├── run.py              # Dev server startup
├── requirements.txt
├── .env.example
├── static/
│   ├── img/logo.png    ← Put your logo here
│   └── uploads/        ← Receipts and generated PDFs
└── templates/
    ├── base.html
    ├── dashboard.html
    ├── auth/login.html
    ├── time/
    ├── expenses/
    ├── invoices/
    ├── clients/
    └── settings.html
```
