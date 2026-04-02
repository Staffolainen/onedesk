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

## 5. Salary Reporting — Skatteverket AGI

### 5.1 Overview
Generate the monthly employer tax declaration (AGI — Arbetsgivardeklaration) for upload to Skatteverket's e-tjänst.

### 5.2 Salary Model

Fields per salary payment:
- `employee_name` — name (Staffan Edlund)
- `personal_nr` — personnummer
- `period` — YYYYMM (reporting month)
- `gross_salary` — bruttolön (fixed monthly amount, configured in settings)
- `employer_contribution` — arbetsgivaravgift (calculated, 31.42% standard rate)
- `tax_withheld` — A-skatt (calculated from tax table 34)
- `net_salary` — nettolön
- `benefit_car` — förmånsvärde tjänstebil (monthly amount, configured in settings)
- `benefit_bicycle_1` — förmånsvärde cykel 1 (configured; taxable if value > 3 000 SEK/year)
- `benefit_bicycle_2` — förmånsvärde cykel 2 (configured; same rule)

### 5.3 A-skatt Calculation

- Tax table: **Table 34**
- Skatteverket publishes machine-readable tax tables via API
- On first use (and annually on January 1), fetch and cache table 34 from Skatteverket API
- Apply table lookup: gross salary + taxable benefits → tax withheld per month

### 5.4 Benefits Handling

| Benefit | Treatment |
|---|---|
| Company car (tjänstebil) | Taxable benefit added to gross for tax calculation; reported on KU10 as `FormansvardeBil` |
| Bicycle 1 & 2 | If annual förmånsvärde ≤ 3 000 SEK: tax-free (no reporting). If > 3 000 SEK: excess is taxable; reported on KU10 |
| Bicycle threshold | Configured per bicycle; system auto-calculates taxable portion |

### 5.5 AGI File Generation
- Generate XML file in Skatteverket AGI format (INK2 / KU10 schema)
- Elements: `Avsandare`, `Blankettgemensamt` (AG), `Blankett` (KU10 per employee)
- KU10 fields: personnummer, period, lön, förmåner, A-skatt, arbetsgivaravgiftsunderlag
- Download XML for upload to **Skatteverket e-tjänst** (not via accounting firm)
- Store submitted declarations per period; prevent duplicate submission for same period

### 5.6 Settings for Salary Module

Configurable in Settings → Lön:
- Fixed monthly gross salary (SEK)
- Company car monthly benefit value (SEK)
- Bicycle 1 annual benefit value (SEK)
- Bicycle 2 annual benefit value (SEK)
- Employer org number (used in AGI XML)
- Tax table number (default: 34)

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
| 2 | Azure AD as primary login | Medium |
| 3 | Automated backups | Low |
| 4 | Bookkeeping settings / voucher templates | Medium |
| 5 | Fortnox live integration | Medium |
| 6 | Incoming invoice — Upload & OCR | Medium |
| 7 | Incoming invoice — Payment backlog | Medium |
| 8 | Incoming invoice — Bokföringsorder | Medium |
| 9 | Payment file (Handelsbanken LB) | High |
| 10 | Salary / AGI reporting | High |

---

## 8. Resolved Questions

| # | Question | Answer |
|---|---|---|
| 1 | Bank for payment file | **Handelsbanken** — use Bankgirot LB dialect for Handelsbanken |
| 2 | A-skatt tax table | **Table 34** — fetch annually from Skatteverket API |
| 3 | Bas-kontoplan account codes | Replaced by configurable **voucher templates per transaction type** (see section 3) |
| 4 | Salary — fixed or variable | **Fixed monthly**, with company car benefit and two company bicycles with special tax treatment |
| 5 | AGI submission channel | **Skatteverket e-tjänst** — generate XML file for manual upload |
