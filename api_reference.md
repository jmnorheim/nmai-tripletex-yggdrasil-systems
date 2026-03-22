# Tripletex API Reference (Hackathon)

## Auth & Conventions
- Basic Auth: username=`0`, password=`session_token`
- Base URL: from `tripletex_credentials.base_url` (includes `/v2`)
- Single response: `{"value": {...}}`
- List response: `{"values": [...], "fullResultSize": N}`
- Use `?fields=*` for all fields, `?fields=id,name,...` for specific
- Dates: `YYYY-MM-DD`, DateTimes: `YYYY-MM-DDThh:mm:ss`
- References use `{"id": N}` format (e.g., `"customer": {"id": 123}`)
- PUT uses optional fields (partial update), include `id` and `version` in body

---

## Employee
**GET /employee** -- Search: `firstName`, `lastName`, `email`, `employeeNumber`, `hasSystemAccess`
**POST /employee** -- Create employee
**GET /employee/{id}** -- Get by ID
**PUT /employee/{id}** -- Update employee
```json
{
  "firstName": "Ola", "lastName": "Nordmann",
  "email": "ola@example.com",
  "phoneNumberMobile": "+4712345678",
  "dateOfBirth": "1990-01-15",
  "nationalIdentityNumber": "12345678901",
  "address": {"addressLine1": "Gate 1", "postalCode": "0150", "city": "Oslo"},
  "department": {"id": 1},
  "employments": [{"startDate": "2026-01-01", "division": {"id": 1}, "employmentDetails": [{"date": "2026-01-01", "employmentType": "ORDINARY", "maritimeEmployment": {"shipRegister": "NIS", "shipType": "OTHER", "tradeArea": "DOMESTIC"}, "remunerationType": "MONTHLY_WAGE", "workingHoursScheme": "NOT_SHIFT", "occupationCode": {"id": 123}}]}]
}
```
Note: `employments` with nested `employmentDetails` is often required. Look up `occupationCode` via GET /employee/employment/occupationCode.

**PUT /employee/entitlement/:grantEntitlementsByTemplate** -- Set employee role
Query params: `employeeId` (required), `template` (required)
Templates: `NONE_PRIVILEGES`, `ALL_PRIVILEGES`, `INVOICING_MANAGER`, `PERSONELL_MANAGER`, `ACCOUNTANT`, `AUDITOR`, `DEPARTMENT_LEADER`

---

## Customer
**GET /customer** -- Search: `customerName`, `email`, `organizationNumber`, `customerAccountNumber`, `isInactive`
**POST /customer** -- Create customer
**GET /customer/{id}** -- Get by ID
**PUT /customer/{id}** -- Update customer
```json
{
  "name": "Acme AS",
  "email": "post@acme.no",
  "organizationNumber": "999888777",
  "phoneNumber": "22334455",
  "phoneNumberMobile": "99887766",
  "isCustomer": true,
  "invoiceEmail": "faktura@acme.no",
  "invoiceSendMethod": "EMAIL",
  "postalAddress": {"addressLine1": "Gata 1", "postalCode": "0150", "city": "Oslo"},
  "physicalAddress": {"addressLine1": "Gata 1", "postalCode": "0150", "city": "Oslo"},
  "language": "NO",
  "invoicesDueIn": 30,
  "invoicesDueInType": "DAYS",
  "currency": {"id": 1}
}
```

---

## Contact
**GET /contact** -- Search: `firstName`, `lastName`, `email`, `customerId`
**POST /contact** -- Create contact
**GET /contact/{id}** -- Get by ID
**PUT /contact/{id}** -- Update contact
```json
{
  "firstName": "Per", "lastName": "Hansen",
  "email": "per@acme.no",
  "phoneNumberMobile": "99112233",
  "customer": {"id": 123}
}
```

---

## Product
**GET /product** -- Search: `name`, `number`, `productNumber`, `isInactive`
**POST /product** -- Create product
**GET /product/{id}** -- Get by ID
**PUT /product/{id}** -- Update product
```json
{
  "name": "Konsulenttime",
  "number": "1001",
  "priceExcludingVatCurrency": 1200.00,
  "vatType": {"id": 3},
  "productUnit": {"id": 1},
  "account": {"id": 3000},
  "isStockItem": false
}
```
**GET /product/unit** -- List product units (stk, time, kg, etc.)

---

## Order
**GET /order** -- Search: `customerId`, `number`, `orderDateFrom`, `orderDateTo`, `isClosed`
**POST /order** -- Create order (include orderLines inline)
**GET /order/{id}** -- Get by ID
**PUT /order/{id}** -- Update order
```json
{
  "customer": {"id": 123},
  "orderDate": "2026-03-21",
  "deliveryDate": "2026-03-21",
  "orderLines": [
    {
      "product": {"id": 456},
      "description": "Konsulenttime",
      "count": 10,
      "unitPriceExcludingVatCurrency": 1200.00,
      "vatType": {"id": 3}
    }
  ]
}
```
**POST /order/orderline** -- Add order line to existing order
**PUT /order/orderline/{id}** -- Update order line
**DELETE /order/orderline/{id}** -- Delete order line

---

## Invoice
**GET /invoice** -- Search: `invoiceDateFrom`* (required), `invoiceDateTo`*, `customerId`, `invoiceNumber`, `kid`
**POST /invoice** -- Create invoice (from existing orders)
```json
{
  "invoiceDate": "2026-03-21",
  "invoiceDueDate": "2026-04-20",
  "customer": {"id": 123},
  "orders": [{"id": 789}]
}
```
**GET /invoice/{id}** -- Get by ID
**PUT /invoice/{id}/:payment** -- Register payment on invoice
Query params: `paymentDate`, `paymentTypeId`, `paidAmount`, `paidAmountCurrency` (for foreign currency)
**PUT /invoice/{id}/:createCreditNote** -- Create credit note
Query params: `date` (required), `comment`, `sendToCustomer`
**PUT /invoice/{id}/:send** -- Send invoice

---

## Travel Expense
**GET /travelExpense** -- Search: `employeeId`, `departmentId`, `projectId`, `state`
**POST /travelExpense** -- Create travel expense
**GET /travelExpense/{id}** -- Get by ID
**PUT /travelExpense/{id}** -- Update
**DELETE /travelExpense/{id}** -- Delete
```json
{
  "employee": {"id": 1},
  "title": "Reise til Oslo",
  "travelDetails": {
    "departureDate": "2026-03-20", "returnDate": "2026-03-21",
    "departureFrom": "Bergen", "destination": "Oslo",
    "departureTime": "08:00", "returnTime": "17:00",
    "purpose": "Kundemote",
    "isForeignTravel": false, "isDayTrip": false
  },
  "isChargeable": false,
  "isFixedInvoicedAmount": false,
  "isIncludeAttachedReceiptsWhenReinvoicing": false
}
```
**PUT /travelExpense/:approve** -- Approve (query: `id`)
**PUT /travelExpense/:deliver** -- Deliver (query: `id`)
**PUT /travelExpense/:createVouchers** -- Create vouchers (query: `id`, `date` -- both REQUIRED)
**POST /travelExpense/cost** -- Add cost to travel expense
```json
{
  "travelExpense": {"id": 1},
  "date": "2026-03-20",
  "costCategory": {"id": 123},
  "paymentType": {"id": 456},
  "amountCurrencyIncVat": 6000.00
}
```
Fields that do NOT exist on cost: `description`, `rate`, `count`, `currency`. Use `amountCurrencyIncVat` for the amount.

**POST /travelExpense/mileageAllowance** -- Add mileage allowance
**POST /travelExpense/perDiemCompensation** -- Add per diem compensation
```json
{
  "travelExpense": {"id": 1},
  "rateCategory": {"id": 690},
  "overnightAccommodation": "HOTEL",
  "location": "Tromsø",
  "count": 5,
  "rate": 800.0,
  "isDeductionForBreakfast": false,
  "isDeductionForLunch": false,
  "isDeductionForDinner": false
}
```
**CRITICAL per diem rules:**
- For DOMESTIC trips: do NOT include `countryCode` -- it causes "Country not enabled for travel expense". Leave it out entirely.
- For FOREIGN trips: include `countryCode` (ISO alpha-2, e.g. "SE") and optionally `travelExpenseZoneId` from GET /travelExpense/zone.
- `overnightAccommodation` enum: `NONE`, `HOTEL`, `BOARDING_HOUSE_WITHOUT_COOKING`, `BOARDING_HOUSE_WITH_COOKING`
- `rateCategory` must be valid for the travel date range. Use GET /travelExpense/rateCategory with filters `type=PER_DIEM`, `isValidDomestic=true/false`, `dateFrom=YYYY-01-01`, `dateTo=YYYY-12-31` to find valid categories.
- `rate` can override the system rate (from GET /travelExpense/rate). Use the rate from the prompt if specified.
- `location` is REQUIRED (cannot be null).
- Fields that do NOT exist: `currency`, `country`, `description`.
**POST /travelExpense/accommodationAllowance** -- Add accommodation allowance
**GET /travelExpense/rate** -- Get rate types for compensations
**GET /travelExpense/rateCategory** -- Get rate categories. Filter params: `type` (PER_DIEM, ACCOMMODATION_ALLOWANCE, MILEAGE_ALLOWANCE), `isValidDomestic`, `isValidDayTrip`, `isValidAccommodation`, `isRequiresOvernightAccommodation`, `dateFrom`, `dateTo`, `name`. Use filters to narrow results (459 total categories without filters). Fields: `id`, `name`, `fromDate`, `toDate`, `type`, `isValidDomestic`, `isRequiresOvernightAccommodation`.
**GET /travelExpense/costCategory** -- Get cost categories (fields: `id`, `description`, `displayName`, `account` -- NOT `name`)
**GET /travelExpense/paymentType** -- Get payment types (fields: `id`, `description`, `account`)

---

## Project
**GET /project** -- Search: `name`, `number`, `customerId`, `projectManagerId`, `isClosed`
**POST /project** -- Create project (**cannot include `fixedPrice` or `isFixedPrice` -- use PUT after creation**)
**GET /project/{id}** -- Get by ID
**PUT /project/{id}** -- Update project (use this to set `isFixedPrice: true` and `fixedprice: N`)
**DELETE /project/{id}** -- Delete project
```json
{
  "name": "Prosjekt Alpha",
  "number": "P-001",
  "projectManager": {"id": 1},
  "customer": {"id": 123},
  "startDate": "2026-03-01",
  "endDate": "2026-12-31",
  "isInternal": false,
  "projectCategory": {"id": 1}
}
```
**GET /project/category** -- List project categories
**POST /project/participant** -- Add participant: `{"project": {"id": N}, "employee": {"id": N}}`
**POST /project/participant/list** -- Add multiple participants at once (body: array of participant objects)

---

## Department
**GET /department** -- Search: `name`, `departmentNumber`, `isInactive`
**POST /department** -- Create department
**GET /department/{id}** -- Get by ID
**PUT /department/{id}** -- Update department
```json
{
  "name": "Salg",
  "departmentNumber": "100",
  "departmentManager": {"id": 1}
}
```

---

## Ledger & Vouchers
**GET /ledger/account** -- Search by exact `number` only. Does NOT support range filters (`numberFrom`/`numberTo` will return all 529 accounts -- never use them).
**POST /ledger/account** -- Create account
**GET /ledger/posting** -- Search: `dateFrom`*, `dateTo`*, `accountId`, `customerId`, `supplierId`
**GET /ledger/posting/openPost** -- Open posts: `date`*, `accountId`, `customerId`
**GET /ledger/voucher** -- Search: `dateFrom`*, `dateTo`* (dateTo is EXCLUSIVE), `number`, `typeId`
**POST /ledger/voucher** -- Create voucher with postings
**PUT /ledger/voucher/{id}** -- Update voucher
**DELETE /ledger/voucher/{id}** -- Delete voucher
**PUT /ledger/voucher/{id}/:reverse** -- Reverse a voucher. Query param: `date` (REQUIRED -- will 422 without it).
**GET /ledger/voucherType** -- List voucher types (IDs vary per sandbox -- always look up first)
**GET /ledger/paymentTypeOut** -- Payment types for outgoing payments
**GET /ledger/accountingDimensionName** -- List free accounting dimension names (query: `activeOnly`)
**POST /ledger/accountingDimensionName** -- Create dimension: `{"dimensionName": "MyDim"}`. Returns `{id, dimensionIndex}` (index auto-assigned 1-3).
**POST /ledger/accountingDimensionValue** -- Create dimension value: `{"displayName": "Value1", "dimensionIndex": 1}` (use dimensionIndex from the dimension name).
**NOTE:** The endpoint is NOT `/freeAccountingDimension` (that returns 404). To attach a dimension value to a voucher posting, use `freeAccountingDimension1: {"id": VALUE_ID}` (or `freeAccountingDimension2`/`3` matching the dimensionIndex).

**CRITICAL voucher posting rules:**
- Each posting MUST include `row` starting from 1 (row 0 is reserved for system-generated VAT postings -- NEVER use row 0)
- Each posting MUST include BOTH `amountGross` AND `amountGrossCurrency` set to the same value (for NOK)
- Each posting MUST include `currency: {"id": 1}` and `date` matching the voucher date
- If an account has a VAT type (e.g. revenue account 3000 -> vatType 3), include `vatType: {"id": N}` on that posting
- Postings must balance: the sum of all `amountGross` values must equal zero
- VoucherType IDs vary between sandboxes -- always GET /ledger/voucherType first
- Postings to account 2400 (leverandorgjeld) with vatType on purchase accounts (4xxx) REQUIRE `supplier: {id: N}` on the posting -- omitting it causes 422 "Leverandør mangler"

```json
{
  "date": "2026-03-31",
  "description": "Manuell postering",
  "voucherType": {"id": 123},
  "postings": [
    {"row": 1, "date": "2026-03-31", "account": {"id": 5000}, "amountGross": 50000.00, "amountGrossCurrency": 50000.00, "currency": {"id": 1}},
    {"row": 2, "date": "2026-03-31", "account": {"id": 2900}, "amountGross": -50000.00, "amountGrossCurrency": -50000.00, "currency": {"id": 1}}
  ]
}
```

---

## Supplier
**GET /supplier** -- Search: `supplierNumber`, `email`, `organizationNumber`
**POST /supplier** -- Create supplier
**GET /supplier/{id}** -- Get by ID
**PUT /supplier/{id}** -- Update supplier

---

## Bank
**GET /bank** -- List banks
**GET /bank/reconciliation** -- Search reconciliations
**POST /bank/reconciliation** -- Create bank reconciliation
**GET /bank/reconciliation/paymentType** -- Payment types

---

## Company & Modules
**GET /company/{id}** -- Get company info (get companyId from GET /token/session/>whoAmI first)
**GET /company** -- Does NOT exist (no list endpoint). Will return 405.
**GET /company/salesmodules** -- List active modules
**POST /company/salesmodules** -- Activate module
Module names: `MAMUT`, `AGRO_LICENCE`, `AGRO_CLIENT`, `AGRO_INVOICE`, `AGRO_WAGE`, etc.

---

## Utility Endpoints
**GET /currency** -- List currencies (search: `code`)
**GET /country** -- List countries (search: `code`). Country model fields: `id`, `name`, `displayName`, `isoAlpha2Code`, `isoAlpha3Code`, `isoNumericCode` -- NOT `code`. Use `fields=id,name,isoAlpha2Code` or `fields=*`.
**GET /activity** -- List activities
**POST /activity** -- Create activity. REQUIRED fields: `name`, `activityType` (one of `"PROJECT_GENERAL_ACTIVITY"` or `"GENERAL_ACTIVITY"`). Also include `isProjectActivity: true/false`, `isGeneral: true/false`, `isChargeable: true/false` as appropriate.
**POST /activity/list** -- Create multiple activities at once (body: array of activity objects)
**GET /division** -- List divisions
**POST /division** -- Create division
**GET /deliveryAddress** -- List delivery addresses
**PUT /deliveryAddress/{id}** -- Update delivery address
**GET /salary/type** -- List salary types
**POST /salary/transaction** -- Create salary transaction
**GET /token/session/>whoAmI** -- Get current session info including companyId

---

## Timesheet
**GET /timesheet/entry** -- Search: `dateFrom`, `dateTo`, `employeeId`, `projectId`, `activityId`
**POST /timesheet/entry** -- Create timesheet entry (one per employee/date/activity/project)
**GET /timesheet/entry/{id}** -- Get by ID
**PUT /timesheet/entry/{id}** -- Update entry
**DELETE /timesheet/entry/{id}** -- Delete entry
**POST /timesheet/entry/list** -- Create multiple entries at once
**PUT /timesheet/entry/list** -- Update multiple entries at once
```json
{
  "employee": {"id": 1},
  "project": {"id": 123},
  "activity": {"id": 456},
  "date": "2026-03-21",
  "hours": 7.5,
  "comment": "Development work"
}
```
**GET /timesheet/entry/>recentActivities** -- Recent activities for employee (params: `projectId` required, `employeeId`)
**GET /timesheet/entry/>recentProjects** -- Recent projects for employee
**GET /timesheet/entry/>totalHours** -- Total hours (params: `employeeId`, `startDate`, `endDate`)

---

## Norwegian Chart of Accounts (NS 4102)

Account numbers follow the standard Norwegian chart. Key accounts by group:

### 1xxx -- Assets (Eiendeler)
- 1200 Maskiner og anlegg [vatType 1]
- 1250 Inventar [vatType 1]
- 1280 Kontormaskiner [vatType 1]
- 1500 Kundefordringer (accounts receivable)
- 1570 Reiseforskudd (travel advance)
- 1700 Forskuddsbetalt leiekostnad (prepaid rent)
- 1720 Andre depositum (other deposits)
- **1920 Bankinnskudd (main bank account -- use as credit for paid expenses)**
- 1950 Bankinnskudd for skattetrekk

### 2xxx -- Equity & Liabilities (Egenkapital og gjeld)
- 2000 Aksjekapital (share capital)
- **2400 Leverandorgjeld (accounts payable -- use as credit for unpaid supplier invoices AND expense receipts)** -- **WARNING: postings to 2400 REQUIRE `supplier: {id: N}` when used with purchase/expense accounts that have vatType (e.g. 4300 + vatType 1). Will fail with "Leverandør mangler" without it. For error corrections or journal entries where no real supplier exists, use 1920 (bank) as credit instead, or look up/create a generic supplier first.**
- 2600 Forskuddstrekk (tax withholding)
- 2700 Utgaende merverdiavgift, hoy sats (output VAT 25%)
- 2710 Inngaende merverdiavgift, hoy sats (input VAT 25%)
- 2770 Skyldig arbeidsgiveravgift
- 2780 Paloppt arbeidsgiveravgift pa paloppt lonn
- 2900 Forskudd fra kunder (customer advances)
- 2910 Gjeld til ansatte og eiere -- **WARNING: postings to this account REQUIRE `employee: {id: N}` on the posting. Will fail with "Ansatt mangler" without it. Avoid unless specifically needed.**
- 2930 Skyldig lonn (accrued salaries -- use for salary accrual postings)
- 2940 Skyldig feriepenger (accrued holiday pay)
- 2960 Annen paloppt kostnad (other accrued expenses)
- 2990 Annen kortsiktig gjeld (other current liabilities)

### 3xxx -- Revenue (Driftsinntekter)
- 3000 Salgsinntekt, avgiftspliktig [vatType 3 -- 25%]
- 3100 Salgsinntekt, avgiftsfri [vatType 5]
- 3900 Annen driftsrelatert inntekt

### 4xxx -- Cost of Goods (Varekostnad)
- 4000 Innkjop av ravarer [vatType 1]
- 4300 Innkjop av varer for videresalg [vatType 1]

### 5xxx -- Personnel (Lonnskostnader)
- 5000 Lonn til ansatte (salaries)
- 5020 Feriepenger (holiday pay)
- 5090 Paloppt, ikke utbetalt lonn
- 5400 Arbeidsgiveravgift (employer's NI)
- 5800 Refusjon av sykepenger (sick pay refund)

### 6xxx -- Operating Costs (Andre driftskostnader)
- 6000 Avskrivning bygninger (depreciation -- buildings)
- 6010 Avskrivning transportmidler (depreciation -- vehicles)
- 6015 Avskrivning maskiner (depreciation -- machinery)
- 6017 Avskrivning inventar (depreciation -- fixtures)
- 6030 Avskrivning andre driftsmidler (depreciation -- other operating assets; may not exist in all sandboxes -- create if missing)
- 6300 Leie lokale (rent) -- typical expense counterpart for accrual reversal from 1720/1700
- 6540 Inventar [vatType 1]
- 6700 Regnskapshonorar (accounting fees)
- 6800 Kontorrekvisita [vatType 1] (office supplies)
- 6900 Telefon [vatType 1]

### 7xxx -- Other Costs (Andre kostnader)
- 7000 Drivstoff [vatType 1] (fuel)
- 7100 Bilgodtgjorelse oppgavepliktig (mileage allowance)
- 7130 Reisekostnad, oppgavepliktig (travel -- reportable)
- **7140 Reisekostnad, ikke oppgavepliktig [vatType 12 -- 12%] (travel -- non-reportable, e.g. train/bus tickets)**
- 7350 Representasjon (entertainment)
- 7500 Forsikringspremie (insurance)
- 7770 Bank og kortgebyrer (bank fees)
- 7790 Annen kostnad, fradragsberettiget [vatType 1]

### 8xxx -- Financial Items (Finansposter)
- 8050 Annen renteinntekt (interest income)
- 8150 Annen rentekostnad (interest expense)
- 8170 Annen finanskostnad (other financial expense)
- 8300 Betalbar skatt (income tax)

### Key VAT Types
- **ID 0**: No VAT treatment
- **ID 1**: Input VAT deduction, high rate (25%) -- used on most expense/purchase accounts
- **ID 3**: Output VAT, high rate (25%) -- used on sales/revenue accounts
- **ID 5**: No output VAT (within VAT law) -- VAT-exempt sales
- **ID 6**: No output VAT (outside VAT law)
- **ID 11**: Input VAT deduction, medium rate (15%)
- **ID 12**: Input VAT deduction, low rate (12%) -- used on transport/travel

---

## Common Posting Patterns

1. **Create entity**: POST with JSON body -> response `{"value": {"id": N, ...}}`
2. **Link entities**: Use `{"id": N}` references (e.g., `"customer": {"id": 5}`)
3. **Invoice flow**: Create Customer -> Create Product -> Create Order (with orderLines) -> Create Invoice (referencing order)
4. **Payment flow**: Create Invoice -> PUT /invoice/{id}/:payment with query params
5. **Credit note**: PUT /invoice/{id}/:createCreditNote with date query param
6. **Set roles**: PUT /employee/entitlement/:grantEntitlementsByTemplate with employeeId + template
7. **Travel expense**: Create TravelExpense -> optionally add costs/mileage/perDiem/accommodation as sub-resources
8. **Voucher/posting**: Create voucher with balanced debit/credit postings. MUST use `row` starting from 1 (never 0), and MUST set both `amountGross` and `amountGrossCurrency` to the same value. Look up voucherType IDs via GET /ledger/voucherType first.
9. **Expense paid by company**: Debit expense account (6xxx/7xxx), Credit 1920 (bank)
10. **Expense owed but not yet paid**: Debit expense account, Credit 2400 (leverandorgjeld)
11. **Employee expense / receipt**: Debit expense account (e.g. 7140), Credit 2400 (leverandorgjeld). NEVER use 2910.
12. **Salary accrual**: Debit 5000 (lonn), Credit 2930 (skyldig lonn)
13. **Depreciation**: Debit 6010-6030 (avskrivning), Credit 1200-1290 (contra asset)
14. **Accrual reversal (1720/1700 to expense)**: Debit expense (6300 for rent), Credit 1720/1700
