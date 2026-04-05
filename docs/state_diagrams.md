# onedesk — Entity State Diagrams

This document describes the lifecycle of every stateful entity in the system:
what states exist, what triggers each transition, and what side-effects occur.

---

## 1. Time Entry (`TimeEntry`)

Time entries do not have an explicit `status` column. Their lifecycle is tracked
via two boolean/FK fields: `invoiced` (bool) and `invoice_id` (FK → Invoice).

```
[registered]  →  [invoiced]
                 ↑
                 | invoice deleted / regenerated
                 ↓
             [registered]
```

| State | `invoiced` | `invoice_id` | Description |
|---|---|---|---|
| **registered** | `False` | `None` | Entry logged, not yet on any invoice |
| **invoiced** | `True` | set | Locked to an approved invoice |

### Transitions

| From | To | Trigger | Side-effects |
|---|---|---|---|
| registered | invoiced | `invoices_approve()` — invoice approved | `invoiced=True`, `invoice_id` set |
| invoiced | registered | `invoices_delete()` or invoice regeneration | `invoiced=False`, `invoice_id=None` |

### Notes
- Only `invoiced=False` entries are available when generating a new invoice period.
- Entries cannot be deleted once `invoiced=True` (UI prevents this).
- Hour type (`POHourType`) determines the rate applied at the time of invoicing.

---

## 2. Mileage Entry (`MileageEntry`)

```
[approved] → [invoiced]
              ↑
              | invoice deleted
              ↓
          [approved]
```

| State | Description |
|---|---|
| **approved** | Registered and ready to be included on an invoice |
| **invoiced** | Locked to an approved invoice |

### Transitions

| From | To | Trigger | Side-effects |
|---|---|---|---|
| — | approved | `mileage_add()` — new entry saved | Created directly in `approved` state |
| approved | invoiced | `invoices_approve()` | `status="invoiced"`, `invoice_id` set |
| invoiced | approved | `invoices_delete()` | `status="approved"`, `invoice_id=None` |

### Notes
- Mileage entries are created as `approved` immediately on save; there is no pending/review step.
- Rate is taken from the active PurchaseOrder at entry date (`get_km_rate()`), or falls back to client default.

---

## 3. Expense / Receipt (`Expense`)

```
[pending] → [approved] → [invoiced]
                ↑            |
                |  invoice   |
                |  deleted   |
                └────────────┘
```

| State | Description |
|---|---|
| **pending** | Uploaded, OCR running or awaiting review |
| **approved** | Reviewed, amounts confirmed, Fortnox voucher created |
| **invoiced** | Locked to an approved outgoing invoice (billable expenses only) |

### Transitions

| From | To | Trigger | Side-effects |
|---|---|---|---|
| — | approved | `expenses_review()` POST — user confirms OCR data | Expense saved; Fortnox voucher created (series A); receipt PDF emailed to Fortnox inbox |
| approved | invoiced | `invoices_approve()` — invoice approved | `status="invoiced"`, `invoice_id` set |
| invoiced | approved | `invoices_delete()` | `status="approved"`, `invoice_id=None` |

### Accounting on `pending → approved`
```
Debit   [category.debit_account]   amount_excl_vat    (e.g. 5410, 4010, 5613)
Debit   2641  Debiterad ingående moms                  vat_amount  (if > 0)
Credit  1930  Bank / Företagskort                      amount_incl_vat  (paid_by=company)
  — or —
Credit  2893  Personalens löneskulder                  amount_incl_vat  (paid_by=personal)
```

### Notes
- Internal (non-billable) expenses stop at `approved`; they are never `invoiced`.
- Billable expenses are included on the outgoing invoice and transition to `invoiced` when the invoice is approved.
- `fortnox_voucher_nr` is set on `pending → approved`.

---

## 4. Outgoing Invoice (`Invoice`)

```
[draft] → [approved] → [sent] → [paid]
   ↑            |
   |  delete    |
   └────────────┘
```

| State | Description |
|---|---|
| **draft** | Generated from time entries / expenses; PDF not yet finalized |
| **approved** | PDF generated; time entries and expenses locked; Fortnox voucher created |
| **sent** | PDF emailed to client |
| **paid** | Payment received and confirmed |

### Transitions

| From | To | Trigger | Side-effects |
|---|---|---|---|
| — | draft | `invoices_create()` — period + project selected | Invoice record created; time entries and expenses linked (still `invoiced=False`) |
| draft | approved | `invoices_approve()` | PDF generated; time entries `invoiced=True`; expenses/mileage `status="invoiced"`; Fortnox voucher created; PDF emailed to Fortnox inbox |
| approved | sent | `invoices_send()` — email sent successfully | `sent_at` set; `status="sent"` |
| sent / approved | paid | `invoices_mark_paid()` — manual | Status set to `paid` |
| sent / approved | paid | `fortnox_sync_paid()` — Fortnox sync finds matching paid invoice | Batch update from Fortnox invoice list |
| draft / approved | deleted | `invoices_delete()` | Time entries and expenses unlinked (`invoiced=False`, `status="approved"`) |

### Accounting on `draft → approved` (Fortnox)
```
Debit   1510  Kundfordringar      total (incl. VAT)
Credit  3001  Försäljning 25%     amount excl. VAT
Credit  2610  Utgående moms 25%   VAT amount
Debit/Credit 3740  Öresavrundning (rounding, if any)
```

### Notes
- Invoice number is assigned on approval using fiscal year format `YYYY-NNN` (e.g. `2025-001`).
- Fiscal year starts May 1 (`FY_START_MONTH=5`).
- Only one non-deleted invoice per project + period is permitted (conflict check on create).

---

## 5. Supplier Invoice (`SupplierInvoice`)

This is the most complex entity, involving two separate Fortnox vouchers at
different points in the lifecycle.

```
[pending] → [approved] → [booked] → [paid]
                                       ↑
                                       | manual mark-paid
                                       | (bypasses payment file)
                             [booked] ─┘
```

| State | Description |
|---|---|
| **pending** | Uploaded; OCR extraction attempted but not yet reviewed by user |
| **approved** | User has reviewed and confirmed the data; ready for payment backlog |
| **booked** | Fortnox voucher A created (arrival booking); invoice appears in payment backlog |
| **paid** | Payment file confirmed uploaded to bank; Fortnox payment voucher created |

### Transitions

| From | To | Trigger | Side-effects |
|---|---|---|---|
| — | pending | `supplier_invoices_upload()` — file uploaded | OCR extraction attempted (PDF regex → Claude Vision → Ollama); raw OCR stored in `ocr_raw` |
| pending | approved | `supplier_invoices_review()` POST — user saves | Data saved; **Fortnox arrival voucher created** (see below); `fortnox_voucher_nr` set; PDF file renamed to `{voucher_ref}-{original}` on disk and emailed to Fortnox inbox |
| pending | deleted | `supplier_invoices_review()` POST `action=discard` | File deleted from disk; record deleted |
| pending | deleted | `supplier_invoices_delete()` | Same as above |
| approved | booked | Part of `pending → approved` transition | Set by `create_supplier_invoice_voucher()` in `fortnox.py` |
| booked | paid | `payment_file_confirm()` — user confirms file uploaded to bank | `PaymentFile.status="confirmed"`; **Fortnox payment voucher created** (see below) |
| booked | paid | `supplier_invoices_mark_paid()` — manual (no payment file) | Status set to `paid`; **no Fortnox voucher created** — must reconcile manually |

### Accounting voucher 1 — Invoice arrival (`pending → booked`)
Date: `invoice_date` (or `due_date` as fallback)
```
Debit   [supplier_category.debit_account]   amount_excl_vat    (e.g. 6540, 4010, 5010)
Debit   2641  Debiterad ingående moms        vat_amount         (if > 0)
Credit  2440  Leverantörsskulder             amount_incl_vat
```

### Accounting voucher 2 — Payment confirmation (`booked → paid`)
Date: `PaymentFile.payment_date`
```
Debit   2440  Leverantörsskulder             amount_incl_vat    (one row per invoice)
Credit  1930  Bank                           total              (single row, sum of all)
```

### Notes
- Payment execution date in the pain.001 XML = last banking day before `due_date` (skips weekends and Swedish bank holidays).
- OCR reference is validated with Luhn (modulo-10) before being sent as structured SCOR reference. If Luhn fails, falls back to unstructured `Ustrd`.
- BG payments route via Bankgirot clearing `MmbId=9900`; PG via `MmbId=9500`.
- Debtor account in pain.001 uses `COMPANY_BBAN` (clearing+account digits).

---

## 6. Payment File (`PaymentFile`)

```
[generated] → [confirmed]
```

| State | Description |
|---|---|
| **generated** | pain.001 XML file created and downloaded; invoices linked but not yet confirmed as sent to bank |
| **confirmed** | User has confirmed the file was uploaded to the bank; Fortnox payment voucher posted |

### Transitions

| From | To | Trigger | Side-effects |
|---|---|---|---|
| — | generated | `payment_run()` POST — invoices selected | pain.001 XML generated and downloaded; `PaymentFile` record created; linked `SupplierInvoice` records get `payment_file_id` set |
| generated | confirmed | `payment_file_confirm()` POST | `PaymentFile.confirmed_at` set; all linked invoices → `status="paid"`; Fortnox payment voucher created (2440 → 1930) |

### Notes
- A payment file can be re-downloaded at any time after generation.
- The confirm button ("Bekräfta betalorder registrerad") is available both on the supplier invoices index page and on the payment run page.
- Once confirmed, the file cannot be un-confirmed (no rollback path — manual Fortnox correction required if needed).

---

## 7. Summary — All States

| Entity | States |
|---|---|
| `TimeEntry` | `registered` → `invoiced` |
| `MileageEntry` | `approved` → `invoiced` |
| `Expense` | `pending` → `approved` → `invoiced` |
| `Invoice` (outgoing) | `draft` → `approved` → `sent` → `paid` |
| `SupplierInvoice` | `pending` → `approved`+`booked` → `paid` |
| `PaymentFile` | `generated` → `confirmed` |

---

## 8. Cross-Entity Relationships at Invoice Approval

When an outgoing `Invoice` is approved, multiple entities change state simultaneously:

```
Invoice: draft → approved
   ├─ TimeEntry (all linked):   invoiced=False → invoiced=True
   ├─ Expense (billable):       approved       → invoiced
   └─ MileageEntry (billable):  approved       → invoiced
```

When an outgoing `Invoice` is deleted, this is fully reversed:

```
Invoice: deleted
   ├─ TimeEntry (all linked):   invoiced=True  → invoiced=False, invoice_id=None
   ├─ Expense (linked):         invoiced       → approved,       invoice_id=None
   └─ MileageEntry (linked):    invoiced       → approved,       invoice_id=None
```
