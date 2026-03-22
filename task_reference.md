# Known Task Types

**SCORING RULE: GET requests are FREE and do not count toward efficiency. Only write calls (POST, PUT, DELETE, PATCH) are counted. Use GETs liberally to gather data and validate before writing. 4xx errors on writes reduce your score.**

## Tier 1 -- Simple (x1 multiplier)

### TASK-01: Opprett ansatt (Create Employee)
Lookup department (GET /department), then POST /employee with department ref.
Required: `userType` ("STANDARD" or "NO_ACCESS"), `department: {id: N}`.
May also need to set entitlements via PUT /employee/entitlement/:grantEntitlementsByTemplate.

**If employment details are needed** (percentage, salary, working hours):
1. GET /department (if not found, POST /department)
2. GET /employee/employment/occupationCode -- if the prompt/PDF contains a numeric STYRK code (e.g. "2511"), search with `code` parameter (NOT `nameNO`). For text-based searches, use broad Norwegian terms like "kontor", "personal", "ingeniør". Do NOT search exact job titles (e.g. "HR-rådgiver") as they rarely match. If no match, use occupationCode `{id: 3}` as a safe fallback.
3. GET /division -- if empty, POST /division with `{name: "Hovedkontor", organizationNumber: "999999999", startDate, municipality: {id: 1}, municipalityDate}`
4. POST /employee -- include `employments` with inline `employmentDetails` containing `percentageOfFullTimeEquivalent`, `annualSalary`, `occupationCode`, `remunerationType: "MONTHLY_WAGE"`, `workingHoursScheme: "NOT_SHIFT"`, `employmentType: "ORDINARY"`, `maritimeEmployment: {shipRegister: "NIS", shipType: "OTHER", tradeArea: "DOMESTIC"}`
5. POST /employee/standardTime with `{employee: {id}, fromDate, hoursPerDay}` (only if working hours specified)

**WRONG field name:** `employmentPercentage` does NOT exist. Use `percentageOfFullTimeEquivalent` on employmentDetails.
**WRONG field placement:** `startDate` is NOT a top-level Employee field. Employment start date goes in `employments[0].startDate`.

### TASK-02: Opprett kunde med org.nr (Create Customer)
GET /customer (by organizationNumber) to check if already exists. If not, POST /customer with all fields from prompt. ALWAYS set `isCustomer: true`.
Extract: name, organizationNumber, email, phone, addresses.

### TASK-03: Opprett produkt med MVA (Create Product)
GET /ledger/vatType to find the right VAT type, then POST /product.
Match VAT rate from prompt: 25% -> vatType 3, 15% -> vatType 31, 12% -> vatType 32, 0% -> vatType 5 or 6.

### TASK-04: Opprett leverandør (Create Supplier)
GET /supplier (by organizationNumber) to check if already exists. If not, POST /supplier with all fields. Set `isSupplier: true`.

### TASK-05: Opprett avdelinger (Create Departments)
POST /department/list with an array of department objects to create ALL departments in ONE call. Minimal body is enough: `[{"name": "Dept1"}, {"name": "Dept2"}]`.
If you choose to include `departmentNumber`, make sure the numbers are unique in that company. In persistent sandboxes, reusing `1`, `2`, `3` can cause 422s.

### TASK-06: Opprett enkel faktura (Create Simple Invoice)
GET /customer + GET /invoice/paymentType + GET /ledger/vatType (batch all GETs upfront, they're free) -> POST /invoice with inline order.
If customer doesn't exist, create them first. Use a SINGLE POST /invoice with a new Order embedded in the `orders` array:
```json
{"invoiceDate": "...", "invoiceDueDate": "...", "customer": {"id": N},
 "orders": [{"customer": {"id": N}, "orderDate": "...", "deliveryDate": "...",
   "orderLines": [{"description": "...", "count": 1, "unitPriceExcludingVatCurrency": 100, "vatType": {"id": 3}}]}]}
```
This creates the order AND invoice in ONE write call. The `sendToCustomer` query param defaults to true, so no separate send call is needed.

### TASK-07: Registrer utgående betaling (Register Payment on Existing Invoice)
Find a customer's pending invoice and register full payment.

Optimal sequence:
1. GET /customer (by organizationNumber) → get customer ID
2. GET /invoice/paymentType (`fields=id,description`) → find "Betalt til bank" payment type
3. GET /invoice (`customerId=X`, `fields=id,invoiceNumber,amount,amountOutstanding`) → find the pending invoice
4. PUT /invoice/{id}/:payment (`paymentDate`, `paymentTypeId`, `paidAmount=amountOutstanding`)

**Key:** Use the invoice's `amountOutstanding` (VAT-inclusive) as `paidAmount`, NOT the VAT-exclusive amount from the prompt.

**Deterministic solver:** `REGISTER_PAYMENT` — handled automatically (4 API calls, ~10s).

### TASK-08: Opprett prosjekt knyttet til kunde (Create Project)
GET /customer -> POST /project with customer ref.
If project manager mentioned, look up employee first.
**NOTE:** POST /project does NOT accept `fixedPrice` or `isFixedPrice`. Create the project first, then PUT /project/{id} to set these fields.

## Tier 2 -- Multi-step (x2 multiplier)

### TASK-09: Faktura med flere produktlinjer og ulik MVA (Invoice with Multiple VAT Rates)
GET /customer -> GET /product (pre-existing in sandbox) -> POST /invoice with inline order (containing multiple orderLines, each with its own vatType).
Products and customer are typically pre-created in the sandbox for this task. Each order line gets a different vatType based on the rate specified. Use POST /invoice with a new Order embedded in `orders` array to save 1 write vs creating order + invoice separately.

### TASK-10: Ordre til faktura til betaling (Order -> Invoice -> Payment)
GET /customer + GET /invoice/paymentType -> POST /order -> PUT /order/{id}/:invoice (with payment params).
The `PUT /order/{id}/:invoice` endpoint creates the invoice AND registers payment in ONE call via query params: `invoiceDate` (required), `paymentTypeId`, `paidAmount`. This saves 1 write vs separate POST /invoice + PUT /invoice/:payment.

### TASK-11: Registrer leverandørfaktura (Register Supplier Invoice)
Register an incoming supplier invoice with correct expense account, VAT, and supplier reference.

Optimal sequence:
1. Batch all GETs in one parallel call (they're free): GET /supplier (by orgNumber), GET /ledger/voucherType, GET /ledger/vatType, GET /ledger/account (expense account + 2400)
2. If supplier not found, POST /supplier to create it (include `postalAddress` if available from PDF)
3. POST /ledger/voucher with voucherType "Leverandørfaktura":
   - Row 1 (expense): debit expense account, `amountGross` = +GROSS_AMOUNT, `vatType: {id: N}`, `invoiceNumber` = inv number
   - Row 2 (AP 2400): credit AP, `amountGross` = -GROSS_AMOUNT, `supplier: {id: N}`, `invoiceNumber` = inv number
   - The API auto-generates a VAT posting (row 0) splitting input VAT to 2710

**Key rules:**
- ALWAYS set `invoiceNumber` on BOTH postings so scoring can match
- ALWAYS include `supplier: {id: N}` on the 2400 posting
- Use GROSS (VAT-inclusive) amounts. Do NOT manually calculate net or create VAT postings.
- VAT type mapping: 25% → vatType 1, 15% → vatType 11, 12% → vatType 12

**Deterministic solver:** `REGISTER_SUPPLIER_INVOICE` — handled automatically (1 write call + optional supplier creation).

### TASK-12: Lønnskjøring med tillegg (Payroll Run)
POST /salary/transaction requires the employee to have an employment record. Fresh sandboxes have none, so you must create prerequisites first:
1. GET /employee (find employee)
2. GET /division -- if empty, POST /division with `{name, organizationNumber, startDate, municipality: {id: 1}, municipalityDate}`
3. Ensure employee has `dateOfBirth` -- if missing, PUT /employee/{id} to set it
4. POST /employee/employment with `{employee: {id}, startDate, division: {id}, employmentDetails: [{date, employmentType: "ORDINARY", maritimeEmployment: {shipRegister: "NIS", shipType: "OTHER", tradeArea: "DOMESTIC"}, remunerationType: "MONTHLY_WAGE", workingHoursScheme: "NOT_SHIFT", occupationCode: {id: 3}}]}`
5. GET /salary/type (find salary type IDs)
6. POST /salary/transaction with `{date, year, month, payslips: [{employee: {id}, specifications: [{salaryType: {id}, amount, rate: <same as amount>, count: 1}]}]}`
Multiple salary types (base + bonus) go as multiple items in `specifications`. The `rate` field is REQUIRED and should equal `amount` for fixed amounts.

### TASK-13: Registrer reiseregning med utlegg (Travel Expense with Costs)
Optimal sequence:
1. GET /employee (find employee by email)
2. GET /travelExpense/costCategory (fields=`id,description,displayName`, count=100 -- get ALL in one call, do NOT paginate or re-fetch)
3. GET /travelExpense/paymentType (fields=`*`)
4. POST /travelExpense (create the travel expense with employee ref and travelDetails)
5. POST /travelExpense/cost for EACH expense (flight, taxi, etc.) -- body: `{travelExpense: {id}, date, costCategory: {id}, paymentType: {id}, amountCurrencyIncVat: AMOUNT}`. Do NOT use `description`, `rate`, `count`, or `currency` fields -- they don't exist.
6. If per diem/daily allowance:
   a. GET /travelExpense/rateCategory with `type=PER_DIEM`, `isValidDomestic=true` (for innland), `dateFrom=YYYY-01-01`, `dateTo=YYYY-12-31` matching the travel year. Pick the right category based on trip duration and overnight status.
   b. GET /travelExpense/rate with `rateCategoryId=N` -- ALWAYS do this, even when the prompt specifies a custom rate. You need the `rateType` ID from the response.
   c. POST /travelExpense/perDiemCompensation with: `{travelExpense: {id}, rateCategory: {id}, rateType: {id: RATE_TYPE_ID}, overnightAccommodation: "HOTEL"/"NONE", location: "City", count: DAYS, rate: RATE, isDeductionForBreakfast: false}`. The `rateType` field is REQUIRED -- without it, deliver will 422. For DOMESTIC trips do NOT include `countryCode` (causes "Country not enabled").
7. PUT /travelExpense/:deliver (query: `id=N`)
8. PUT /travelExpense/:approve (query: `id=N`)
9. PUT /travelExpense/:createVouchers (query: `id=N`, `date=YYYY-MM-DD` -- date is REQUIRED)

**Notes:**
- GET /travelExpense/perDiemCompensation before creating the travel expense returns 0 (useless).
- Get all costCategories in one call with count=100. GETs are free -- look up everything you need upfront.

### TASK-14: Krediter faktura / kreditnota (Credit Note)
GET /invoice (search by customer/description) -> PUT /invoice/{id}/:createCreditNote.
Credit note date goes as query param `date`.

**IMPORTANT:** Always search invoices with wide date ranges (`invoiceDateFrom=2020-01-01`, `invoiceDateTo=2030-12-31`). Invoice dates may be in the future -- never assume they are in past years only. The credit note `date` must be on or after the invoice's `invoiceDate` -- using an earlier date causes 422.

**Deterministic solver:** `CREDIT_NOTE` — handled automatically (~3 API calls).

### TASK-15: Sett fastpris og fakturer prosjekt (Fixed Price Project Invoice)
**IMPORTANT:** POST /project does NOT accept `fixedPrice` or `isFixedPrice`. You MUST:
1. POST /project (without fixedPrice fields)
2. PUT /project/{id} with `{id, version, isFixedPrice: true, fixedprice: AMOUNT}` (note lowercase `fixedprice`)
3. POST /order (with project ref and orderLines) -> POST /invoice

Optimal sequence: GET /customer + GET /employee (if project manager) + GET /invoice/paymentType + GET /ledger/vatType (batch all GETs upfront, they're free) -> POST /project -> PUT /project/{id} -> POST /invoice with inline order.
Use POST /invoice with a new Order embedded in `orders` array (including `project: {id}` and `orderLines`) to save 1 write vs separate POST /order + POST /invoice. Look up any additional data you need -- GETs are free.

### TASK-16: Timeføring og fakturering (Time Tracking & Billing / Project Lifecycle)
Optimal sequence:
1. Batch all GETs upfront (they're free): GET /customer, GET /employee (for each person), GET /supplier (if supplier cost mentioned), GET /activity, GET /ledger/voucherType, GET /ledger/account for any accounts you'll need (4300, 2400, 1920, etc.), GET /invoice/paymentType
2. POST /project -> PUT /project/{id} (if budget/fixedPrice needed)
3. POST /project/participant/list to add ALL non-manager participants in ONE call (body: array of `{project: {id}, employee: {id}}`)
4. POST /timesheet/entry/list to create ALL timesheet entries in ONE call (body: array of entry objects)
5. If supplier cost: POST /ledger/voucher (debit 4300, credit 2400)
6. POST /invoice with inline order (embed new Order with `project: {id}` and `orderLines` in the `orders` array -- saves 1 write vs separate POST /order + POST /invoice)

**Existing project variant:** If the project already exists (prompt references it by name), use GET /project with `customerId` and include `startDate` in `fields`. Always set timesheet `date` on or after the project's `startDate` to avoid 422.

**Project manager entitlement gotcha:** If POST /project fails with a validation error saying the chosen `projectManager.id` does not have project-manager access, call `PUT /employee/entitlement/:grantEntitlementsByTemplate` with `employeeId=<pm_id>` and `template=DEPARTMENT_LEADER`, then retry the project create.

**Participant add gotcha:** `POST /project/participant/list` can return 422 if a participant is already linked to the project. Treat "already exists/already linked" participant errors as non-fatal and continue with timesheet registration.

**Fixed-price lifecycle variant:** If the prompt gives a project budget/fixed price but only asks to register hours (no hourly rates), still treat it as TASK-16. Set the project's `fixedprice` via `PUT /project/{id}` and create the invoice from that fixed-price amount rather than trying to derive invoice value from hourly rates.

### TASK-17: Opprett fri dimensjon og bokfør bilag (Custom Accounting Dimensions)
**IMPORTANT: The endpoint is NOT `/freeAccountingDimension` (404). The correct endpoints are:**
- `/ledger/accountingDimensionName` (create/list dimension names)
- `/ledger/accountingDimensionValue` (create/list dimension values)

Optimal sequence:
1. POST /ledger/accountingDimensionName with `{"dimensionName": "Prosjekttype"}` → returns `{id, dimensionIndex}` (dimensionIndex is auto-assigned: 1, 2, or 3)
2. POST /ledger/accountingDimensionValue for EACH value with `{"displayName": "Forskning", "dimensionIndex": 1}` (use the dimensionIndex from step 1)
3. GET /ledger/voucherType (need type ID for voucher)
4. GET /ledger/account for the expense account + credit account (e.g. 1920 for bank)
5. POST /ledger/voucher with dimension ref on the posting. Use `freeAccountingDimension1: {"id": VALUE_ID}` on the posting (use `freeAccountingDimension2`/`3` if dimensionIndex was 2/3).

No module activation needed -- dimensions work in the default sandbox.

### TASK-18: Reverser betaling / bankretur (Reverse Payment)
Optimal sequence:
1. GET /ledger/voucher (dateFrom, dateTo -- both REQUIRED) with `fields=id,number,date,description,voucherType(id,name),postings(id,account(id,number),amountGross,amountGrossCurrency)` -- find the payment voucher (type "Betaling") matching the invoice
2. PUT /ledger/voucher/{id}/:reverse with query param `date=YYYY-MM-DD` (date is REQUIRED -- will 422 without it)

If the prompt references a specific invoice, optionally GET /invoice (invoiceDateFrom + invoiceDateTo both REQUIRED) first to confirm context, but this is not strictly necessary if voucher descriptions are clear enough.

## Tier 3 -- Complex (x3 multiplier)

### TASK-19: Ansatt fra arbeidskontrakt / PDF (Employee from Employment Contract)
Same flow as TASK-01 with employment details, but data is extracted from a PDF employment contract.
Follow the TASK-01 "If employment details are needed" sequence. Parse the PDF for: name, date of birth, start date, department, occupation code (STYRK), salary, employment percentage, working hours.

### TASK-20: Leverandørfaktura fra PDF (Supplier Invoice from PDF)
Register a supplier invoice where data is extracted from a PDF. Combines PDF parsing with the TASK-11 supplier invoice flow.

Optimal sequence:
1. Parse the PDF for: supplier name, organization number, invoice number, amounts (gross), VAT rate, expense account, due date, postal address
2. Batch all GETs: GET /supplier (by orgNumber), GET /ledger/voucherType, GET /ledger/vatType, GET /ledger/account (expense + 2400)
3. If supplier not found, POST /supplier with `name`, `organizationNumber`, `isSupplier: true`, and `postalAddress` only. Do NOT include `bankAccounts` -- causes 422.
4. **PREFERRED:** POST /incomingInvoice?sendTo=ledger — creates SupplierInvoice entity + voucher in ONE call (see TASK-11)
5. **FALLBACK:** POST /ledger/voucher with voucherType "Leverandørfaktura" (see TASK-11)

**Scoring-critical:** Use voucherType **"Leverandørfaktura"**. Do **NOT** use voucherType **"Ansattutlegg"** just because the receipt says "Bedriftskort" / company card.

### TASK-21: Komplett onboarding fra tilbudsbrev / PDF (Complete Onboarding from Offer Letter)
Full employee onboarding from a PDF offer letter. This is an extended TASK-01 that includes all employment details.
Follow the TASK-01 "If employment details are needed" sequence end-to-end. Parse the PDF for: name, date of birth, start date, department, occupation code, salary, employment percentage, working hours, entitlements.

May also need to set entitlements via PUT /employee/entitlement/:grantEntitlementsByTemplate after employee creation.

### TASK-22: Utgift fra kvittering / PDF på avdeling (Expense from Receipt/PDF on Department)
Post an expense with correct account, VAT treatment, and optional department.

Optimal sequence:
1. Batch all GETs: GET /department (if mentioned), GET /supplier (by orgNumber), GET /ledger/voucherType, GET /ledger/vatType, GET /ledger/account (expense + 2400)
2. If supplier not found, POST /supplier with `name`, `organizationNumber`, `isSupplier: true`, and `postalAddress`
3. POST /ledger/voucher with postings:
   - Debit: expense account (e.g. 7140) with appropriate vatType (e.g. 12 for travel)
   - Credit: 2400 (leverandorgjeld) with `supplier: {id: N}`
   - If department specified, include `department: {id: N}` on the expense posting
   - If VAT applies, include `vatType: {id: N}` on the expense posting

**Scoring-critical:** For ordinary supplier receipts/PDFs, use voucherType **"Leverandørfaktura"**. Do **NOT** use voucherType **"Ansattutlegg"** just because the receipt says "Bedriftskort" / company card. The payment wording describes how it was paid, not the artifact the grading flow is likely searching for.

NEVER credit account 2910 (requires employee ref). Use 2400 for supplier debt. Only use 1920 if there is truly no supplier to attach and 2400 is impractical.

**If supplier needs to be created** (from PDF invoice): POST /supplier with `name`, `organizationNumber`, `isSupplier: true`, and `postalAddress` only. Do NOT include `bankAccounts` -- the field requires a complex nested format and causes 422 "Verdien er ikke av korrekt type".

### TASK-23: Bankavstemming fra CSV (Bank Reconciliation from CSV)
Reconcile a bank statement (CSV) against open invoices. Match received payments to customer invoices and outgoing payments to supplier invoices. Handle partial payments, interest, fees, and tax transfers.

**Deterministic solver:** `BANK_RECONCILIATION` — parses CSV, batches GETs, combines vouchers.

**IMPORTANT fresh sandbox facts:**
- There are NO pre-existing supplier invoice vouchers or supplier ledger postings.
- PUT /invoice/:payment handles accounts receivable (1500) internally.
- Use manual vouchers (POST /ledger/voucher) for supplier invoices and payments.
- GETs are free -- look up anything you need to validate data before writing.

Optimal sequence:
1. Parse CSV to classify rows: customer payments, supplier payments, misc (tax/interest/fees)
2. Batch all GETs upfront (they're free): GET /ledger/voucherType, GET /invoice (all open), GET /supplier, GET /invoice/paymentType, GET /ledger/account for 1920, 2400, 4300, 1950, 8050, 7770
3. PUT /invoice/{id}/:payment for each matched customer payment (parallel)
4. POST /ledger/voucher for each supplier: **combine invoice + payment into ONE voucher** with 4 postings (debit 4300, credit 2400 with supplier ref, debit 2400 with supplier ref, credit 1920). This saves 1 write per supplier vs separate invoice + payment vouchers.
5. POST /ledger/voucher: **combine ALL misc entries** (tax, interest, fees) into ONE voucher. Use account 1950 (Skattetrekk bankkonto) for tax transfers, 8050 for interest income, 7770 for bank fees.

**Key accounting rules:**
- Tax transfers use account 1950 (Skattetrekk bankkonto), NOT 2600 (Forskuddstrekk)
- Interest income (Renteinntekter, Inn column): debit 1920, credit 8050
- Interest expense (Rentekostnader, Ut column): debit 8050, credit 1920
- Bank fees (Gebyr, Ut column): debit 7770, credit 1920

**Write count:** 5 customer payments + 3 supplier vouchers + 1 misc voucher = **9 writes** (vs 14 with separate vouchers)

### TASK-24: Feilsøking i hovedbok (Error Correction in Ledger)
Find erroneous vouchers/postings and post correction entries.

Optimal sequence:
1. Batch call: GET /ledger/voucherType + GET /ledger/voucher (with `dateFrom`, `dateTo`, `fields=id,number,date,description,voucherType(id,name),postings(id,account(id,number,name),amountGross,amountGrossCurrency)`) -- this fetches ALL vouchers with full posting detail in ONE call.
2. Analyze the response. When `count == fullResultSize`, ALL data is present -- do NOT paginate or re-fetch. Extract account IDs directly from the posting data.
3. GET /ledger/account ONLY for accounts NOT already present in the voucher/posting data (e.g. the correct target account 7100 if only 6300 was in the data, or VAT account 2710). Skip any account whose ID you already have from step 1.
4. POST /ledger/voucher for each correction entry. Post ALL corrections in a single iteration batch if possible.

**Write-efficiency rules (GETs are free, focus on minimizing writes):**
- Request comprehensive fields on GETs so you have all data for your writes. Use `fields=id,account(id,number,name),amountGross,amountGrossCurrency,voucher(id,number,date,description),currency(id)` for postings.
- After step 1, you should have all account IDs, amounts, and voucher references. Look up any additional accounts you need -- GETs don't count.
- For duplicate reversal, extract BOTH sides of the original voucher's postings and reverse them (swap debit/credit signs). Do not reconstruct amounts from the prompt text alone.
- Focus on getting every POST /ledger/voucher (write) correct on the first attempt. Use GETs to verify any data you're unsure about.

**Correction patterns:**
- Wrong account (e.g. 6300 used instead of 7100): Debit correct account, Credit wrong account (reverses the error)
- Duplicate voucher: Reverse the entire voucher (debit what was credited, credit what was debited). Extract the original posting amounts exactly.
- Missing VAT line: Debit VAT account (e.g. 2710), Credit counterpart. **CAUTION:** if crediting 2400 with vatType on a purchase account (4xxx), you MUST include `supplier: {id: N}` on the 2400 posting or it will fail with "Leverandør mangler". Use 1920 (bank) instead if no supplier exists.
- Wrong amount: Post the difference (debit/credit to adjust to correct amount)

### TASK-25: MVA-melding generering og kontroll (VAT Return Generation and Control)
Generate and verify VAT return (MVA-melding). This involves aggregating VAT postings and validating the return.

*Documentation pending -- no detailed implementation guide available yet.*

### TASK-26: Periodeavslutning / månedsavslutning (Month-end Closing)
Typical prompt asks to: post accrual reversal, record depreciation, verify trial balance, post salary accrual.

Optimal sequence:
1. GET /ledger/voucherType (need the "Memorialbillag" or equivalent type ID)
2. GET /ledger/account for each account mentioned (1720, 6300, 1200, 6010/6030, 5000, 2930, etc.)
3. If an account doesn't exist (e.g. 6030), create it immediately with POST /ledger/account
4. **COMBINE all journal entries into ONE voucher** with POST /ledger/voucher containing ALL postings (accrual reversal + depreciation + salary accrual). All debit-credit pairs go as postings in a single voucher -- they just need to sum to zero. This saves 2 writes vs posting 3 separate vouchers.
5. Do NOT verify trial balance -- the API enforces balanced vouchers. Just confirm 201 responses.
6. NEVER call GET /ledger/posting for month-end tasks -- it adds no value.

Key accounting rules:
- Accrual reversal (1720 -> expense): Debit expense (e.g. 6300 for rent), Credit 1720
- Depreciation: Debit depreciation account (6010/6015/6017/6030), Credit asset account (1200/1250)
  - Straight-line: annual = cost / useful_life_years, monthly = annual / 12
- Salary accrual: Debit 5000, Credit 2930 (skyldig lonn) -- ALWAYS use 2930 even if the prompt says 2900. Account 2900 is "Forskudd fra kunder" (customer advances), NOT salary. The grading system expects the correct Norwegian accounting standard.

### TASK-27: Valutafaktura med agio/disagio (Foreign Currency Invoice + Payment + Agio)
Invoice a customer in foreign currency (e.g. EUR), then register payment at a different exchange rate. Tripletex automatically posts the currency gain/loss (agio) -- no manual voucher needed.

Optimal sequence:
1. GET /currency (code=EUR or relevant currency -- need currency ID)
2. GET /customer (by organizationNumber, use `fields=id,name,version,currency(id,code)` -- get version+currency upfront to avoid re-fetch)
3. GET /ledger/vatType (find correct VAT type for the product)
4. GET /invoice/paymentType (need paymentTypeId for payment registration)
5. PUT /customer/{id} (set `currency: {id: EUR_ID}` so invoice is issued in foreign currency)
6. POST /invoice with inline order and `currency: {id: EUR_ID}` (embed new Order with `currency`, `orderLines` in the `orders` array -- saves 1 write vs separate POST /order + POST /invoice)
7. PUT /invoice/{id}/:payment (with `paymentDate`, `paymentTypeId`, `paidAmount` in NOK at new rate, `paidAmountCurrency` in foreign currency original amount)

**Agio calculation:** `paidAmount` = foreign amount × payment exchange rate (e.g. 10781 × 11.41 = 123,011.21 NOK). `paidAmountCurrency` = original foreign amount (e.g. 10781 EUR). Tripletex computes the difference vs the invoice rate and auto-posts to account 8060 (Valutagevinst/agio) or 8160 (Valutatap/disagio).

**Notes:**
- No manual voucher is needed for agio -- Tripletex auto-generates the payment + agio voucher. GETs are free, so look up whatever you need, but no extra writes are needed.
- Include `version,currency(id,code)` in the initial GET /customer to have all data upfront.
- Do NOT try to pay an existing NOK invoice with foreign currency amounts -- if the existing invoice is in NOK, you must create a NEW invoice in the correct foreign currency first.

**TASK-27b: Payment on Existing Foreign Currency Invoice**
When the task references an existing invoice and only asks to register payment at a different exchange rate:

Optimal sequence:
1. GET /customer (by organizationNumber) → customer ID
2. GET /invoice/paymentType → find "Betalt til bank"
3. GET /invoice (customerId=X, include `amountCurrency,amountCurrencyOutstanding,currency(id,code)`) → find the existing invoice
4. PUT /invoice/{id}/:payment (`paymentDate`, `paymentTypeId`, `paidAmount` = foreign amount × payment rate in NOK, `paidAmountCurrency` = original foreign amount)

**Deterministic solver:** `FOREIGN_CURRENCY_PAYMENT` — handles this automatically (~4 API calls).

### TASK-28: Resultatanalyse: kostnadssikring (Cost Analysis + Internal Projects)
Analyze ledger postings to find expense accounts with the biggest changes, then create internal projects and activities.

Optimal sequence:
1. GET /ledger/posting for each month (e.g. Jan + Feb) with `fields=id,account(id,number,name),amountGross` -- aggregate per expense account (4000-7999) to find top increases
2. GET /employee (need a project manager for internal projects -- `fields=id`, `count=1`)
3. POST /project/list to create ALL projects in one batch (1 write). Each project: `{"name": "<account name>", "startDate": "YYYY-MM-01", "projectManager": {"id": PM_ID}, "isInternal": true}`
4. POST /project/projectActivity for EACH project (1 write per project). Use the **account name** as the activity name. Body:
```json
{"project": {"id": PROJ_ID}, "activity": {"name": "<account name>", "activityType": "PROJECT_SPECIFIC_ACTIVITY"}}
```
Total writes: 1 (projects) + N (activities) = N+1 writes.

**IMPORTANT:** Do NOT use inline `projectActivities` on POST /project/list — the OpenAPI spec states PROJECT_SPECIFIC_ACTIVITY must be created via POST /project/projectActivity. Inline activities may be silently ignored.

**IMPORTANT:** Do NOT set `isProjectActivity`, `isGeneral`, or `isTask` on the activity — these are readOnly fields computed from `activityType`.

**Do NOT** call `compute_taxable_result` -- it returns only aggregate net totals, not per-account breakdowns. Go directly to GET /ledger/posting for per-account data.

**Common mistakes:**
- Omitting `startDate` on POST /project causes 422. Always include it.
- Omitting `activityType` on POST /project/projectActivity causes 422. Always use `"PROJECT_SPECIFIC_ACTIVITY"`.
- Using inline `projectActivities` instead of separate POST /project/projectActivity — activities won't be created.
- Inventing activity names like "Kostnadsreduksjon" when the prompt doesn't specify one — use the account name.

### TASK-29: Komplett prosjektsyklus med avvik (Complete Project Cycle with Deviations)
Full project lifecycle including handling deviations such as overdue invoices, reminder fees, and partial payments.

This extends TASK-16 (Time Tracking & Billing) with additional steps for handling project deviations:

**Reminder Fee + Invoice + Partial Payment flow:**

Optimal sequence:
1. GET /invoice (invoiceDateFrom, invoiceDateTo -- both REQUIRED) to find the overdue invoice and its customer
2. GET /ledger/voucherType (need "Purring" type ID for reminder voucher)
3. GET /ledger/account for 1500 + 3400 (or whatever accounts the prompt specifies)
4. GET /invoice/paymentType (need paymentTypeId for partial payment)
5. POST /ledger/voucher for the reminder charge (debit 1500, credit 3400). **MUST include `customer: {id: N}` on the 1500 posting** or you get 422 "Kunde mangler".
6. POST /product (create a "Purregebyr" product for the reminder fee, vatType 5 = no VAT)
7. POST /invoice with inline order (embed new Order with customer ref and orderLine for the reminder product in the `orders` array -- saves 1 write vs separate POST /order + POST /invoice)
8. PUT /invoice/{id}/:send (query: `sendType=EMAIL`)
9. PUT /invoice/{overdue_id}/:payment (query: `paymentDate`, `paymentTypeId`, `paidAmount`)

### TASK-30: Årsoppgjør: avskrivninger, periodiseringer og skatt (Year-end Closing)
Simplified year-end closing: depreciation, prepaid reversals, tax calculation.

**CRITICAL execution order -- post known amounts FIRST, calculate tax LAST:**
1. GET /ledger/voucherType (need "Memorialbillag" type ID)
2. GET /ledger/account for ALL accounts you might need: depreciation expense (6010), accumulated depreciation (1209), prepaid (1700), expense counterpart (6300), tax expense (8700), tax liability (2920), and any others mentioned in the prompt. GETs are free -- look up everything upfront. Create any missing accounts with POST /ledger/account.
3. **COMBINE all depreciation + prepaid reversal entries into ONE voucher** with POST /ledger/voucher containing ALL postings. Multiple debit-credit pairs go in one voucher -- they just need to sum to zero. This saves writes vs posting separate vouchers.
   - Straight-line depreciation: annual = cost / useful_life_years
   - Debit depreciation expense account (e.g. 6010), Credit accumulated depreciation account (e.g. 1209 or contra asset)
   - Prepaid reversal: Debit expense account (e.g. 6300 for rent), Credit prepaid account (e.g. 1700)
4. Use `compute_taxable_result` tool to get the net result INCLUDING the entries just posted
5. Calculate tax = 22% (or rate from prompt) × taxable result (absolute value of net_result if negative/profitable)
6. POST /ledger/voucher for tax: Debit tax expense (e.g. 8700), Credit tax liability (e.g. 2920). Tax must be a separate voucher since you need the result from step 4 first.

**Tax on a loss -- CRITICAL:**
- If `compute_taxable_result` returns a negative `net_result` (which means a loss in Tripletex's sign convention where negative = profit), calculate tax = 22% × abs(net_result).
- If `net_result` is positive (meaning a loss / expenses exceed revenue), the company has NO taxable profit. Do NOT post a skattekostnad voucher -- skip the tax entry entirely. There is no income tax to pay on a loss in a simplified year-end.

**Do NOT:**
- Call GET /resultReport/result -- this endpoint does NOT exist (returns 404)
- Try to calculate tax BEFORE posting depreciation and reversal entries (they affect the taxable result)
- Post a tax voucher when the taxable result shows a loss (positive net_result) -- this is wrong and will cost points
- Use `compute_taxable_result` only when you need the net result for tax. For per-account data, use GET /ledger/posting (GETs are free).
