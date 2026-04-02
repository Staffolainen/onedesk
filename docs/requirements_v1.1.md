# onedesk v1.1 — Requirements Specification

Branch: v1.1-dev  
Status: Draft  
Author: Staffan Edlund  

---

## 1. Infrastructure

### 1.1 Custom Domain
- Configure custom domain (e.g. `onedesk.edlundkonsult.io`) on Azure App Service
- Managed SSL certificate (free via Azure)
- DNS CNAME pointing to `onedesk-app.azurewebsites.net`

### 1.2 Automated Backups
- Daily scheduled backup of the SQLite database (`/app/instance/onedesk.db`)
- Backup destination: Azure Blob Storage (new container `onedesk-backups`)
- Retention: 30 days
- Implementation: Azure Logic App or timer-triggered Azure Function
- Backup naming: `onedesk-YYYYMMDD.db`
- Optional: email notification on failure

### 1.3 Azure AD as Primary Login
- Azure AD Easy Auth (already configured) becomes the primary and required authentication
- Current Flask password login becomes a backup/emergency access method only
- After AD auth, Flask session is established automatically (no second password prompt for normal use)
- The Flask password prompt is shown only if AD auth is bypassed or unavailable
- Settings page shows currently logged-in AD user identity

---

## 2. Outgoing Invoicing — Fortnox Live Integration

### 2.1 Enable Live Fortnox Sync
- Remove dry-run mode; enable real API calls when Fortnox OAuth tokens are present
- On invoice "Send": create invoice in Fortnox via API, attach PDF
- On expense approval: create voucher in Fortnox with receipt attachment
- Show Fortnox invoice number / voucher number in onedesk UI after sync
- Error handling: show clear error if Fortnox call fails, allow retry

---

## 3. Bookkeeping Settings — Fortnox Voucher Templates

A new **Settings → Bokföring** section allows configuring the standard Fortnox account code mappings used when creating vouchers. This replaces hard-coded account codes and makes the system adaptable without code changes.

### 3.1 Voucher Template Model

One configurable template per transaction type:

| Transaction Type | Description |
|---|---|
| `supplier_invoice` | Incoming supplier invoices |
| `expense_internal` | Internal expenses (paid from company card) |
| `expense_external` | External expenses (paid from personal card, reimbursed) |
| `mileage` | Mileage reimbursement |
| `salary` | Monthly salary payment |

### 3.2 Per-Template Fields

Each template stores:
- `debit_account` — primary debit account (Bas-kontoplan code, e.g. `6540`)
- `debit_account_label` — human-readable label (e.g. "IT-tjänster")
- `vat_account` — VAT debit account (e.g. `2640` for 25% ingående moms; blank if N/A)
- `vat_rate` — VAT rate in percent (e.g. `25`, `12`, `6`, `0`)
- `credit_account` — credit account (e.g. `2440` Leverantörsskulder, `2890` reimbursement, `2710` salary tax)
- `cost_center` — default cost center code (optional)
- `voucher_series` — Fortnox voucher series letter (e.g. `A`, `B`, `L`)
- `description_template` — default voucher text template (e.g. `"Lön {period}"`)

### 3.3 UI
- Settings page lists all five template types
- Each row: edit button opens a form with the fields above
- Changes saved immediately to DB; no app restart required
- Show Fortnox account lookup helper (type account code, see name from Bas-kontoplan)

---

## 4. Incoming Invoice & Payment Management (New Module)

This is the largest new feature. It handles the full lifecycle of supplier invoices received by Staffan Edlund Konsult AB.

### 4.1 Overview — Workflow

```
Upload PDF/image
      ↓
OCR & extract invoice data (Claude Vision)
      ↓
Review & confirm extracted data
      ↓
Add to Payment Backlog
      ↓
Create Bokföringsorder → Fortnox (with PDF attachment)
      ↓
Mark as ready for payment
      ↓
Generate Payment File (Handelsbanken LB format)
      ↓
Import to bank (manual download)
```

### 4.2 Supplier Invoice Model

Fields extracted/entered per invoice:
- `supplier_name` — supplier name
- `supplier_org_nr` — supplier org number
- `invoice_number` — supplier's invoice number
- `invoice_date` — invoice date
- `due_date` — payment due date
- `amount_excl_vat` — amount excluding VAT
- `vat_amount` — VAT amount
- `amount_incl_vat` — total amount including VAT
- `currency` — currency (default SEK)
- `payment_ref` — OCR/reference number for payment
- `bankgiro` / `plusgiro` / `iban` — payment destination
- `account_code` — bookkeeping account (from voucher template or manual override)
- `cost_center` / `project_id` — optional link to assignment
- `pdf_filename` — stored PDF/image
- `status` — `pending` → `approved` → `booked` → `paid`
- `fortnox_voucher_nr` — populated after Fortnox sync
- `payment_file_id` — populated when added to a payment file

### 4.3 Step 1 — Upload & OCR

- Upload PDF or image (photo of paper invoice)
- Claude Vision extracts: supplier, invoice number, date, due date, amounts, VAT, OCR reference, bankgiro/plusgiro/IBAN
- Review screen similar to existing expense capture flow
- User confirms or corrects extracted data
- Pre-fill bookkeeping account from `supplier_invoice` voucher template; allow override

### 4.4 Step 2 — Payment Backlog

- List view of all approved supplier invoices not yet paid
- Shows: supplier, invoice number, due date, amount, status, days until due (red if overdue)
- Sort by due date
- Select one or multiple invoices to include in a payment file
- Mark individual invoices as paid manually (for cases handled outside the system)

### 4.5 Step 3 — Bokföringsorder to Fortnox

- For each approved invoice, create a supplier voucher in Fortnox using the `supplier_invoice` template:
  - Debit: configured debit account (default `6540`)
  - Debit: VAT account (default `2640` Ingående moms 25%)
  - Credit: configured credit account (default `2440` Leverantörsskulder)
- Attach the scanned PDF to the Fortnox voucher
- Record Fortnox voucher number in onedesk
- Status updates to `booked`

### 4.6 Step 4 — Payment File Generation (Handelsbanken)

- Select one or more invoices from the payment backlog
- Generate a payment file in **Bankgirot LB format** for **Handelsbanken**
  - File format: fixed-width text, Bankgirot LB specification
  - Supports bankgiro (BG) and plusgiro (PG) payments
  - Payment date selectable (default: due date, min: today)
- Download file for manual import into Handelsbanken internet bank
- Mark included invoices as `paid` after download confirmation
- Store payment file record with timestamp and included invoice list

### 4.7 ISO 20022 / pain.001 (Future)
- Alternative payment file format
- Defer to v1.2

---

## 5. User Management & Personal Logins

### 5.1 Overview
Replace the current single shared password with individual named user accounts. Each user maps to an Azure AD identity and has a role that controls access. This lays the groundwork for multi-user operations (e.g. a finance assistant) and the future salary module (v1.2).

### 5.2 User Model

| Field | Description |
|---|---|
| `id` | Primary key |
| `display_name` | Full name (e.g. "Staffan Edlund") |
| `email` | Primary identifier; must match AD account email |
| `ad_oid` | Azure AD Object ID — populated on first login, used for reliable matching |
| `role` | `admin` / `employee` / `viewer` |
| `active` | Boolean — inactive users cannot log in |
| `created_at` | Timestamp |
| `last_login_at` | Timestamp — updated on each successful login |

### 5.3 Roles

| Role | Access |
|---|---|
| `admin` | Full access — all features, settings, user management |
| `employee` | Standard access — time, expenses, mileage, invoices (own data) — **also appears in payroll in v1.2** |
| `viewer` | Read-only — can view dashboards and reports, no write access |

### 5.4 Login Flow with Azure AD

1. User hits any protected page → redirected to Azure AD Easy Auth
2. AD token arrives; Flask reads `X-MS-CLIENT-PRINCIPAL` header (already in place)
3. Look up user by `ad_oid` (preferred) or `email` from token
4. If found and active: establish Flask session with user id + role
5. If not found: show "Access not provisioned" page (not a 403 — friendly message with contact info)
6. If found but inactive: show "Account disabled" message
7. Flask password login (existing) remains as emergency backdoor for the admin account only

### 5.5 User Management UI (Admin only)

- **Settings → Användare** — list of all users with name, email, role, active status, last login
- **Add user**: enter email + display name + role; `ad_oid` populated automatically on first login
- **Edit user**: change role or deactivate/reactivate
- **Delete user**: soft-delete (sets `active=False`); never hard-delete to preserve audit trail
- Admin cannot deactivate themselves

### 5.6 Migration from Single Password

- On first startup after upgrade: existing `User` row becomes the `admin` account
- `email` set to `ADMIN_EMAIL` env var (new required config); `ad_oid` populated on first AD login
- `ADMIN_PASSWORD` env var continues to work for emergency Flask login
- No data migration needed for business data — only the user table changes

### 5.7 Future (v1.2) — Salary Integration
- Any user with role `employee` appears automatically in the payroll module
- Per-employee salary settings (gross salary, benefits, tax table, personnummer) stored on the user record
- Enables running payroll for multiple employees (e.g. Staffan + spouse in finance role)

---

## 6. Non-Functional Requirements

| Area | Requirement |
|---|---|
| Security | All new routes `@login_required`, file uploads through protected route |
| Languages | All UI text dual Swedish/English, all flash messages bilingual |
| Storage | Uploaded supplier invoices stored in `/app/instance/uploads/supplier/` |
| DB migrations | All new columns via incremental `ALTER TABLE` try/except pattern |
| Testing | pytest tests for OCR extraction, payment file generation, AGI calculation, tax table lookup |
| Backup | New supplier invoice PDFs included in daily backup scope |

---

## 7. Implementation Order

| Priority | Feature | Effort |
|---|---|---|
| 1 | Custom domain | Low |
| 2 | **User management & personal logins** | Medium |
| 3 | Azure AD as primary login (tied to user accounts) | Medium |
| 4 | Automated backups | Low |
| 5 | Bookkeeping settings / voucher templates | Medium |
| 6 | Fortnox live integration | Medium |
| 7 | Incoming invoice — Upload & OCR | Medium |
| 8 | Incoming invoice — Payment backlog | Medium |
| 9 | Incoming invoice — Bokföringsorder | Medium |
| 10 | Payment file (Handelsbanken LB) | High |

---

## 8. Deferred to v1.2

| Feature | Notes |
|---|---|
| Salary / AGI reporting | Deferred; `employee` role in user model is the prerequisite. Salary settings will be stored per user. Bicycle 2 förmånsvärde is > 3 000 SEK/year → taxable excess must be handled. Tax table 34 fetched from Skatteverket API annually. |
| ISO 20022 / pain.001 payment format | Only needed if Handelsbanken LB support is dropped |

---

## 9. Resolved Questions

| # | Question | Answer |
|---|---|---|
| 1 | Bank for payment file | **Handelsbanken** — use Bankgirot LB dialect for Handelsbanken |
| 2 | A-skatt tax table | **Table 34** — fetch annually from Skatteverket API |
| 3 | Bas-kontoplan account codes | Replaced by configurable **voucher templates per transaction type** (see section 3) |
| 4 | Salary — fixed or variable | **Fixed monthly**, with company car benefit and two company bicycles with special tax treatment |
| 5 | AGI submission channel | **Skatteverket e-tjänst** — generate XML file for manual upload (deferred to v1.2) |
| 6 | Salary scope | Deferred to v1.2; `employee` role on user record is the prerequisite. Bike 2 is above 3 000 SEK threshold — taxable excess handling required. |
