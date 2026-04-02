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

## 3. Incoming Invoice & Payment Management (New Module)

This is the largest new feature. It handles the full lifecycle of supplier invoices received by Staffan Edlund Konsult AB.

### 3.1 Overview — Workflow

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
Generate Payment File (Bankgirot/ISO 20022)
      ↓
Import to bank (manual download)
```

### 3.2 Supplier Invoice Model

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
- `account_code` — bookkeeping account (Bas-kontoplan, e.g. 6540 IT-tjänster)
- `cost_center` / `project_id` — optional link to assignment
- `pdf_filename` — stored PDF/image
- `status` — `pending` → `approved` → `booked` → `paid`
- `fortnox_voucher_nr` — populated after Fortnox sync
- `payment_file_id` — populated when added to a payment file

### 3.3 Step 1 — Upload & OCR

- Upload PDF or image (photo of paper invoice)
- Claude Vision extracts: supplier, invoice number, date, due date, amounts, VAT, OCR reference, bankgiro/plusgiro/IBAN
- Review screen similar to existing expense capture flow
- User confirms or corrects extracted data
- Suggest bookkeeping account (Bas-kontoplan) based on supplier name / description

### 3.4 Step 2 — Payment Backlog

- List view of all approved supplier invoices not yet paid
- Shows: supplier, invoice number, due date, amount, status, days until due (red if overdue)
- Sort by due date
- Select one or multiple invoices to include in a payment file
- Mark individual invoices as paid manually (for cases handled outside the system)

### 3.5 Step 3 — Bokföringsorder to Fortnox

- For each approved invoice, create a supplier voucher in Fortnox:
  - Debit: selected account code (e.g. 6540)
  - Debit: VAT account (2640 Ingående moms 25%)
  - Credit: 2440 Leverantörsskulder
- Attach the scanned PDF to the Fortnox voucher
- Record Fortnox voucher number in onedesk
- Status updates to `booked`

### 3.6 Step 4 — Payment File Generation

- Select one or more invoices from the payment backlog
- Generate a payment file in **Bankgirot LB format** (standard Swedish bank import format)
  - File format: fixed-width text, Bankgirot specification
  - Supports bankgiro payments (BG) and plusgiro (PG)
  - Payment date selectable (default: today or due date)
- Download file for manual import into internet bank (SEB, Handelsbanken, etc.)
- Mark included invoices as `paid` after download confirmation
- Store payment file record with timestamp and included invoice list

### 3.7 ISO 20022 / pain.001 (Future)
- Alternative payment file format for banks supporting SEPA/ISO 20022
- Defer to v1.2 unless bank requires it

---

## 4. Salary Reporting — Skatteverket AGI

### 4.1 Overview
Generate the monthly employer tax declaration (AGI — Arbetsgivardeklaration) for upload to Skatteverket.

### 4.2 Salary Model

Fields per salary payment:
- `employee_name` — name (initially: Staffan Edlund, owner/employee)
- `personal_nr` — personnummer
- `period` — YYYYMM (reporting month)
- `gross_salary` — bruttolön
- `employer_contribution` — arbetsgivaravgift (calculated)
- `tax_withheld` — preliminary tax (A-skatt)
- `net_salary` — nettolön

### 4.3 AGI File Generation
- Generate XML file in Skatteverket's AGI format (technical specification from SKV)
- Fields: KU10 (lön), AG (arbetsgivare), arbetstagare
- Download XML for upload to Skatteverket's e-tjänst or Kivra
- Store submitted declarations per period

### 4.4 Scope for v1.1
- Single employee (owner) only
- Manual entry of salary amount per month
- Auto-calculate arbetsgivaravgift (31.42% standard rate)
- Auto-calculate A-skatt based on configured tax table
- Generate and download AGI XML

---

## 5. Non-Functional Requirements

| Area | Requirement |
|---|---|
| Security | All new routes `@login_required`, file uploads through protected route |
| Languages | All UI text dual Swedish/English, all flash messages bilingual |
| Storage | Uploaded supplier invoices stored in `/app/instance/uploads/supplier/` |
| DB migrations | All new columns via incremental `ALTER TABLE` try/except pattern |
| Testing | pytest tests for OCR extraction, payment file generation, AGI calculation |
| Backup | New supplier invoice PDFs included in daily backup scope |

---

## 6. Implementation Order

| Priority | Feature | Effort |
|---|---|---|
| 1 | Custom domain | Low |
| 2 | Azure AD as primary login | Medium |
| 3 | Automated backups | Low |
| 4 | Fortnox live integration | Medium |
| 5 | Incoming invoice — Upload & OCR | Medium |
| 6 | Incoming invoice — Payment backlog | Medium |
| 7 | Incoming invoice — Bokföringsorder | Medium |
| 8 | Payment file (Bankgirot LB) | High |
| 9 | Salary / AGI reporting | High |

---

## 7. Open Questions

1. **Bank** — Which bank for payment file import? (SEB, Handelsbanken, Swedbank) — affects exact LB file dialect
2. **Tax table** — Which A-skatt table number applies to Staffan?
3. **Accounting** — Confirm Bas-kontoplan account codes to use for common supplier types
4. **Salary** — Is there a regular fixed monthly salary, or does it vary?
5. **AGI submission** — Preferred submission channel: Skatteverket e-tjänst or via accounting firm?
