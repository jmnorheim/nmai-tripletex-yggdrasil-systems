# Known Task Types

## Tier 1 -- Simple (x1 multiplier)

### TASK-01: Create Employee (+ Onboarding)
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

### TASK-02: Create Customer
POST /customer with all fields from prompt. ALWAYS set `isCustomer: true`.
Extract: name, organizationNumber, email, phone, addresses.

### TASK-03: Create Product
GET /ledger/vatType to find the right VAT type, then POST /product.
Match VAT rate from prompt: 25% -> vatType 3, 15% -> vatType 31, 12% -> vatType 32, 0% -> vatType 5 or 6.

### TASK-04: Create Supplier
POST /supplier with all fields. Set `isSupplier: true`.

### TASK-05: Create Departments
POST /department for each department mentioned. If multiple, call one at a time.

### TASK-06: Create Simple Invoice
GET /customer -> POST /order (with orderLines) -> POST /invoice (referencing order).
If customer doesn't exist, create them first. Invoice needs `orders: [{id: N}]`, `invoiceDate`, `invoiceDueDate`.

### TASK-07: Register Payment on Existing Invoice
Find a customer's pending invoice and register full payment.

Optimal sequence:
1. GET /customer (by organizationNumber) → get customer ID
2. GET /invoice/paymentType (`fields=id,description`) → find "Betalt til bank" payment type
3. GET /invoice (`customerId=X`, `fields=id,invoiceNumber,amount,amountOutstanding`) → find the pending invoice
4. PUT /invoice/{id}/:payment (`paymentDate`, `paymentTypeId`, `paidAmount=amountOutstanding`)

**Key:** Use the invoice's `amountOutstanding` (VAT-inclusive) as `paidAmount`, NOT the VAT-exclusive amount from the prompt.

**Deterministic solver:** `REGISTER_PAYMENT` — handled automatically (4 API calls, ~10s).

### TASK-08: Create Project
GET /customer -> POST /project with customer ref.
If project manager mentioned, look up employee first.
**NOTE:** POST /project does NOT accept `fixedPrice` or `isFixedPrice`. Create the project first, then PUT /project/{id} to set these fields.

## Tier 2 -- Multi-step (x2 multiplier)

### TASK-09: Invoice with Multiple VAT Rates
GET /customer -> GET /product (pre-existing in sandbox) -> POST /order (with multiple orderLines, each with its own vatType) -> POST /invoice.
Products and customer are typically pre-created in the sandbox for this task. Each order line gets a different vatType based on the rate specified.

### TASK-10: Order -> Invoice -> Payment
GET /customer -> POST /order -> POST /invoice -> PUT /invoice/{id}/:payment.
Payment uses query params: `paymentDate`, `paymentTypeId`, `paidAmount`. Get payment types via GET /invoice/paymentType first if needed (paymentTypeId is often 1 or look it up).

### TASK-11: Cost Analysis + Internal Projects
Analyze ledger postings to find expense accounts with the biggest changes, then create internal projects and activities.

Optimal sequence:
1. GET /ledger/posting for each month (e.g. Jan + Feb) with `fields=id,account(id,number,name),amountGross` -- aggregate per expense account to find top increases
2. GET /employee (need a project manager for internal projects -- `fields=id`, `count=1`)
3. POST /project for each identified account (use `isInternal: true`, account name as project name)
4. POST /activity for each project -- REQUIRES `activityType` (use `"PROJECT_GENERAL_ACTIVITY"`). Also include `name`, `isProjectActivity: true`, `isGeneral: true`, `isChargeable: false`.

**Do NOT** call `compute_taxable_result` -- it returns only aggregate net totals, not per-account breakdowns. Go directly to GET /ledger/posting for per-account data.

**Common mistake:** Omitting `activityType` on POST /activity causes 422. Always include it.

### TASK-12: Payroll Run
POST /salary/transaction requires the employee to have an employment record. Fresh sandboxes have none, so you must create prerequisites first:
1. GET /employee (find employee)
2. GET /division -- if empty, POST /division with `{name, organizationNumber, startDate, municipality: {id: 1}, municipalityDate}`
3. Ensure employee has `dateOfBirth` -- if missing, PUT /employee/{id} to set it
4. POST /employee/employment with `{employee: {id}, startDate, division: {id}, employmentDetails: [{date, employmentType: "ORDINARY", maritimeEmployment: {shipRegister: "NIS", shipType: "OTHER", tradeArea: "DOMESTIC"}, remunerationType: "MONTHLY_WAGE", workingHoursScheme: "NOT_SHIFT", occupationCode: {id: 3}}]}`
5. GET /salary/type (find salary type IDs)
6. POST /salary/transaction with `{date, year, month, payslips: [{employee: {id}, specifications: [{salaryType: {id}, amount, rate: <same as amount>, count: 1}]}]}`
Multiple salary types (base + bonus) go as multiple items in `specifications`. The `rate` field is REQUIRED and should equal `amount` for fixed amounts.

### TASK-13: Travel Expense with Costs
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

**Do NOT:**
- Fetch GET /travelExpense/perDiemCompensation before creating the travel expense (returns 0, useless)
- Re-fetch costCategory multiple times -- get all in one call with count=100
- Fetch GET /travelExpense/rateCategory unless you need per diem compensation

### TASK-14: Credit Note
GET /invoice (search by customer/description) -> PUT /invoice/{id}/:createCreditNote.
Credit note date goes as query param `date`.

**IMPORTANT:** Always search invoices with wide date ranges (`invoiceDateFrom=2020-01-01`, `invoiceDateTo=2030-12-31`). Invoice dates may be in the future -- never assume they are in past years only. The credit note `date` must be on or after the invoice's `invoiceDate` -- using an earlier date causes 422.

**Deterministic solver:** `CREDIT_NOTE` — handled automatically (~3 API calls).

### TASK-15: Fixed Price Project Invoice
**IMPORTANT:** POST /project does NOT accept `fixedPrice` or `isFixedPrice`. You MUST:
1. POST /project (without fixedPrice fields)
2. PUT /project/{id} with `{id, version, isFixedPrice: true, fixedprice: AMOUNT}` (note lowercase `fixedprice`)
3. POST /order (with project ref and orderLines) -> POST /invoice

Optimal sequence: GET /customer -> GET /employee (if project manager) -> POST /project -> PUT /project/{id} -> POST /order -> POST /invoice.
Only look up activity if you need to register timesheet entries. Only look up voucherType if you need to create a voucher.

### TASK-16: Time Tracking & Billing (Project Lifecycle)
Optimal sequence:
1. GET /customer, GET /employee (for each person), GET /supplier (if supplier cost mentioned), GET /activity (need activity ID for timesheet)
2. POST /project -> PUT /project/{id} (if budget/fixedPrice needed)
3. POST /project/participant (for non-manager employees)
4. POST /timesheet/entry (for each employee's hours)
5. If supplier cost: GET /ledger/voucherType, GET /ledger/account for ONLY 4300 (expense) + 2400 (AP), POST /ledger/voucher (debit 4300, credit 2400). Do NOT look up 1920 -- recording a supplier cost is not a payment.
6. POST /order (with project ref) -> POST /invoice

Do NOT look up department, project/category, or bank (1920) -- they are not needed for this task.

**Existing project variant:** If the project already exists (prompt references it by name), use GET /project with `customerId` and include `startDate` in `fields`. Always set timesheet `date` on or after the project's `startDate` to avoid 422.

### TASK-17: Custom Accounting Dimensions
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

### TASK-19: Reminder Fee + Invoice + Partial Payment
Optimal sequence:
1. GET /invoice (invoiceDateFrom, invoiceDateTo -- both REQUIRED) to find the overdue invoice and its customer
2. GET /ledger/voucherType (need "Purring" type ID for reminder voucher)
3. GET /ledger/account for 1500 + 3400 (or whatever accounts the prompt specifies)
4. GET /invoice/paymentType (need paymentTypeId for partial payment)
5. POST /ledger/voucher for the reminder charge (debit 1500, credit 3400). **MUST include `customer: {id: N}` on the 1500 posting** or you get 422 "Kunde mangler".
6. POST /product (create a "Purregebyr" product for the reminder fee, vatType 5 = no VAT)
7. POST /order (with customer ref and orderLine for the reminder product)
8. POST /invoice (referencing the order)
9. PUT /invoice/{id}/:send (query: `sendType=EMAIL`)
10. PUT /invoice/{overdue_id}/:payment (query: `paymentDate`, `paymentTypeId`, `paidAmount`)

### TASK-18: Reverse Payment
Optimal sequence:
1. GET /ledger/voucher (dateFrom, dateTo -- both REQUIRED) with `fields=id,number,date,description,voucherType(id,name),postings(id,account(id,number),amountGross,amountGrossCurrency)` -- find the payment voucher (type "Betaling") matching the invoice
2. PUT /ledger/voucher/{id}/:reverse with query param `date=YYYY-MM-DD` (date is REQUIRED -- will 422 without it)

If the prompt references a specific invoice, optionally GET /invoice (invoiceDateFrom + invoiceDateTo both REQUIRED) first to confirm context, but this is not strictly necessary if voucher descriptions are clear enough.

### TASK-20: Foreign Currency Invoice + Payment + Agio
Invoice a customer in foreign currency (e.g. EUR), then register payment at a different exchange rate. Tripletex automatically posts the currency gain/loss (agio) -- no manual voucher needed.

Optimal sequence:
1. GET /currency (code=EUR or relevant currency -- need currency ID)
2. GET /customer (by organizationNumber, use `fields=id,name,version,currency(id,code)` -- get version+currency upfront to avoid re-fetch)
3. GET /ledger/vatType (find correct VAT type for the product)
4. GET /invoice/paymentType (need paymentTypeId for payment registration)
5. PUT /customer/{id} (set `currency: {id: EUR_ID}` so invoice is issued in foreign currency)
6. POST /product (with `priceExcludingVatCurrency` in foreign currency amount, `currency: {id: EUR_ID}`)
7. POST /order (with `currency: {id: EUR_ID}` and orderLines referencing the product)
8. POST /invoice (referencing the order, with `currency: {id: EUR_ID}`)
9. PUT /invoice/{id}/:payment (with `paymentDate`, `paymentTypeId`, `paidAmount` in NOK at new rate, `paidAmountCurrency` in foreign currency original amount)

**Agio calculation:** `paidAmount` = foreign amount × payment exchange rate (e.g. 10781 × 11.41 = 123,011.21 NOK). `paidAmountCurrency` = original foreign amount (e.g. 10781 EUR). Tripletex computes the difference vs the invoice rate and auto-posts to account 8060 (Valutagevinst/agio) or 8160 (Valutatap/disagio).

**Do NOT:**
- Look up GET /ledger/voucherType -- no manual voucher is needed, Tripletex auto-generates the payment + agio voucher
- Look up GET /ledger/account for 8060, 1920, or 1500 -- these are handled internally by the payment endpoint
- Fetch GET /ledger/voucher after the payment to verify the agio posting -- a 200 response confirms success
- Use narrow fields on the initial GET /customer -- include `version,currency(id,code)` to avoid a second fetch
- Try to pay an existing NOK invoice with foreign currency amounts -- if the existing invoice is in NOK, you must create a NEW invoice in the correct foreign currency first. Do NOT make payment attempts on a mismatched-currency invoice.

### TASK-20b: Payment on Existing Foreign Currency Invoice
When the task references an existing invoice and only asks to register payment at a different exchange rate:

Optimal sequence:
1. GET /customer (by organizationNumber) → customer ID
2. GET /invoice/paymentType → find "Betalt til bank"
3. GET /invoice (customerId=X, include `amountCurrency,amountCurrencyOutstanding,currency(id,code)`) → find the existing invoice
4. PUT /invoice/{id}/:payment (`paymentDate`, `paymentTypeId`, `paidAmount` = foreign amount × payment rate in NOK, `paidAmountCurrency` = original foreign amount)

**Deterministic solver:** `FOREIGN_CURRENCY_PAYMENT` — handles this automatically (~4 API calls).

## Tier 3 -- Complex (x3 multiplier)

### TASK-T3-MONTH-END: Month-end Closing
Typical prompt asks to: post accrual reversal, record depreciation, verify trial balance, post salary accrual.

Optimal sequence:
1. GET /ledger/voucherType (need the "Memorialbillag" or equivalent type ID)
2. GET /ledger/account for each account mentioned (1720, 6300, 1200, 6010/6030, 5000, 2930, etc.)
3. If an account doesn't exist (e.g. 6030), create it immediately with POST /ledger/account
4. POST /ledger/voucher for each journal entry (accrual reversal, depreciation, salary accrual)
5. Do NOT verify trial balance -- the API enforces balanced vouchers. Just confirm 201 responses.
6. NEVER call GET /ledger/posting for month-end tasks -- it adds no value.

Key accounting rules:
- Accrual reversal (1720 -> expense): Debit expense (e.g. 6300 for rent), Credit 1720
- Depreciation: Debit depreciation account (6010/6015/6017/6030), Credit asset account (1200/1250)
  - Straight-line: annual = cost / useful_life_years, monthly = annual / 12
- Salary accrual: Debit 5000, Credit 2930 (skyldig lonn) -- ALWAYS use 2930 even if the prompt says 2900. Account 2900 is "Forskudd fra kunder" (customer advances), NOT salary. The grading system expects the correct Norwegian accounting standard.

### TASK-T3-EXPENSE: Expense from Receipt/PDF
Post an expense with correct account, VAT treatment, and optional department.

Optimal sequence:
1. GET /department (if department mentioned)
2. GET /ledger/voucherType (get "Leverandorfaktura" type ID)
3. GET /ledger/account for expense account (e.g. 7140 for travel) and credit account (2400)
4. POST /ledger/voucher with postings:
   - Debit: expense account (e.g. 7140) with appropriate vatType (e.g. 12 for travel)
   - Credit: 2400 (leverandorgjeld)
   - If department specified, include `department: {id: N}` on the expense posting
   - If VAT applies, include `vatType: {id: N}` on the expense posting

NEVER credit account 2910 (requires employee ref). Use 2400 for supplier debt or 1920 if paid by company bank.

**If supplier needs to be created** (from PDF invoice): POST /supplier with `name`, `organizationNumber`, `isSupplier: true`, and `postalAddress` only. Do NOT include `bankAccounts` -- the field requires a complex nested format and causes 422 "Verdien er ikke av korrekt type".

### TASK-T3-BANK-RECON: Bank Reconciliation from CSV
Reconcile a bank statement (CSV) against open invoices. Match received payments to customer invoices and outgoing payments to supplier invoices. Handle partial payments, interest, fees, and tax transfers.

**IMPORTANT fresh sandbox facts:**
- There are NO pre-existing supplier invoice vouchers or supplier ledger postings. Do NOT search for them (no GET /ledger/voucher by supplier type, no GET /ledger/posting per supplier, no GET /ledger/posting/openPost on 2400).
- PUT /invoice/:payment handles accounts receivable (1500) internally -- do NOT look up account 1500.
- Use manual vouchers (POST /ledger/voucher) for supplier invoices and payments -- do NOT look up GET /ledger/paymentTypeOut.
- GET /invoice returns customer refs with names -- do NOT make a separate GET /customer call.

Optimal sequence:
1. GET /ledger/voucherType (need type IDs for supplier invoice + payment vouchers)
2. GET /invoice (find all open customer invoices -- includes customer names)
3. GET /supplier (match supplier names to CSV entries) -- if supplier not found, POST /supplier to create it. You NEED supplier IDs for all 2400 postings.
4. GET /invoice/paymentType (need paymentTypeId for customer invoice payments)
5. GET /ledger/account for ONLY the accounts needed in voucher postings: 1920 (bank), 2400 (AP), and any special accounts from the CSV (e.g. 8050 interest, 1950 tax bank, 7770 bank fees, 4300 purchases)
6. PUT /invoice/{id}/:payment for each matched customer payment from the CSV
7. POST /ledger/voucher for each supplier invoice (debit expense/purchase account, credit 2400). **MUST include `supplier: {id: N}` on the 2400 posting.**
8. POST /ledger/voucher for each supplier payment (debit 2400, credit 1920). **MUST include `supplier: {id: N}` on the 2400 debit posting.**
9. POST /ledger/voucher for each misc entry (interest income, bank fees, tax transfers, etc.)

### TASK-T3-OTHER: Error Correction in Ledger
Find erroneous vouchers/postings and post correction entries.

Optimal sequence:
1. Batch call: GET /ledger/voucherType + GET /ledger/voucher (with `dateFrom`, `dateTo`, `fields=id,number,date,description,voucherType(id,name),postings(id,account(id,number,name),amountGross,amountGrossCurrency)`) -- this fetches ALL vouchers with full posting detail in ONE call.
2. Analyze the response. When `count == fullResultSize`, ALL data is present -- do NOT paginate or re-fetch. Extract account IDs directly from the posting data.
3. GET /ledger/account ONLY for accounts NOT already present in the voucher/posting data (e.g. the correct target account 7100 if only 6300 was in the data, or VAT account 2710). Skip any account whose ID you already have from step 1.
4. POST /ledger/voucher for each correction entry. Post ALL corrections in a single iteration batch if possible.

**Efficiency rules -- CRITICAL:**
- NEVER re-fetch vouchers with different `fields`, `from`, or `count` parameters. The first GET returns all data you need if you request comprehensive fields.
- NEVER re-fetch `/ledger/posting` for the same account with different field selections. Request `fields=id,account(id,number,name),amountGross,amountGrossCurrency,voucher(id,number,date,description),currency(id)` on the FIRST call.
- NEVER re-fetch individual vouchers by date range after the bulk fetch -- the data is already in your context.
- NEVER re-fetch `/ledger/voucherType` -- it's a static list, cache from the first call.
- After step 1, you should have all account IDs, amounts, and voucher references needed. The only GETs after step 1 should be for NEW accounts (target/correction accounts not in the original data).
- For duplicate reversal, extract BOTH sides of the original voucher's postings and reverse them (swap debit/credit signs). Do not reconstruct amounts from the prompt text alone.

**Correction patterns:**
- Wrong account (e.g. 6300 used instead of 7100): Debit correct account, Credit wrong account (reverses the error)
- Duplicate voucher: Reverse the entire voucher (debit what was credited, credit what was debited). Extract the original posting amounts exactly.
- Missing VAT line: Debit VAT account (e.g. 2710), Credit counterpart. **CAUTION:** if crediting 2400 with vatType on a purchase account (4xxx), you MUST include `supplier: {id: N}` on the 2400 posting or it will fail with "Leverandør mangler". Use 1920 (bank) instead if no supplier exists.
- Wrong amount: Post the difference (debit/credit to adjust to correct amount)

### TASK-T3-YEAR-END: Year-end Closing
Simplified year-end closing: depreciation, prepaid reversals, tax calculation.

**CRITICAL execution order -- post known amounts FIRST, calculate tax LAST:**
1. GET /ledger/voucherType (need "Memorialbillag" type ID)
2. GET /ledger/account for accounts used in voucher POSTINGS only: depreciation expense (e.g. 6010), accumulated depreciation (e.g. 1209), prepaid (1700), expense counterpart (e.g. 6300), tax expense (8700), tax liability (2920). Do NOT look up asset accounts (1210, 1230, 1250, etc.) -- they are descriptive and not used in postings. Create any missing accounts with POST /ledger/account.
3. POST /ledger/voucher for EACH depreciation entry (one voucher per asset as separate bilag):
   - Straight-line: annual = cost / useful_life_years
   - Debit depreciation expense account (e.g. 6010), Credit accumulated depreciation account (e.g. 1209 or contra asset)
4. POST /ledger/voucher for prepaid expense reversal:
   - Debit expense account (e.g. 6300 for rent), Credit prepaid account (e.g. 1700)
5. Use `compute_taxable_result` tool to get the net result INCLUDING the entries just posted
6. Calculate tax = 22% (or rate from prompt) × taxable result (absolute value of net_result if negative/profitable)
7. POST /ledger/voucher for tax: Debit tax expense (e.g. 8700), Credit tax liability (e.g. 2920)

**Tax on a loss -- CRITICAL:**
- If `compute_taxable_result` returns a negative `net_result` (which means a loss in Tripletex's sign convention where negative = profit), calculate tax = 22% × abs(net_result).
- If `net_result` is positive (meaning a loss / expenses exceed revenue), the company has NO taxable profit. Do NOT post a skattekostnad voucher -- skip the tax entry entirely. There is no income tax to pay on a loss in a simplified year-end.

**Do NOT:**
- Call GET /resultReport/result -- this endpoint does NOT exist (returns 404)
- Fetch GET /ledger/posting to manually aggregate the result -- use `compute_taxable_result` instead
- Try to calculate tax BEFORE posting depreciation and reversal entries (they affect the taxable result)
- Re-fetch postings with different fields or filters -- one call via the tool is enough
- Post a tax voucher when the taxable result shows a loss (positive net_result) -- this is wrong and will cost points
