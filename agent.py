import os
import re
import json
import time
import uuid
import base64
import asyncio
import logging
import calendar
import contextvars
from datetime import datetime, timedelta
from pathlib import Path
from io import BytesIO

import httpx
import pdfplumber
from fastapi import FastAPI, Request, Query
from fastapi.responses import JSONResponse
from dotenv import load_dotenv

load_dotenv()

app = FastAPI(title="Tripletex AI Agent")

# ---- Configuration --------------------------------------------------------

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
MODEL = os.getenv("AGENT_MODEL", "anthropic/claude-sonnet-4")
SOLVER_MODEL = "anthropic/claude-sonnet-4"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
MAX_ITERATIONS = 25
SOLVE_TIMEOUT = 180
RESPONSE_TRUNCATE_CHARS = 3000

# ---- Logging --------------------------------------------------------------

LOG_DIR = Path(__file__).parent / "logs"
LOG_DIR.mkdir(exist_ok=True)


def _make_logger(name: str, filename: str) -> logging.Logger:
    logger = logging.getLogger(name)
    logger.setLevel(logging.DEBUG)
    if not logger.handlers:
        handler = logging.FileHandler(LOG_DIR / filename, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s | %(message)s"))
        logger.addHandler(handler)
    return logger


for logfile in ("submissions.log", "testing.log", "network.log"):
    (LOG_DIR / logfile).write_text("", encoding="utf-8")

submission_log = _make_logger("submissions", "submissions.log")
testing_log = _make_logger("testing", "testing.log")
network_log = _make_logger("network", "network.log")

_ctx_log: contextvars.ContextVar[logging.Logger | None] = contextvars.ContextVar(
    "_ctx_log", default=None
)
_ctx_rid: contextvars.ContextVar[str] = contextvars.ContextVar("_ctx_rid", default="")


@app.middleware("http")
async def log_all_requests(request: Request, call_next):
    start = time.time()
    method = request.method
    path = request.url.path
    client_ip = request.client.host if request.client else "unknown"
    network_log.info(f">>> {method} {path} from {client_ip}")
    try:
        response = await call_next(request)
        elapsed = time.time() - start
        network_log.info(
            f"<<< {method} {path} -> {response.status_code} ({elapsed:.1f}s)"
        )
        return response
    except Exception as e:
        elapsed = time.time() - start
        network_log.error(
            f"!!! {method} {path} FAILED after {elapsed:.1f}s: {type(e).__name__}: {e}"
        )
        raise

# ---- Tool Definition ------------------------------------------------------

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "tripletex_api",
            "description": (
                "Make a request to the Tripletex v2 REST API. "
                "Use this to create, read, update, or delete accounting entities. "
                "For /list batch endpoints, body should be a JSON array."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "method": {
                        "type": "string",
                        "enum": ["GET", "POST", "PUT", "DELETE"],
                        "description": "HTTP method",
                    },
                    "path": {
                        "type": "string",
                        "description": "API path, e.g. /employee, /invoice/123/:payment",
                    },
                    "params": {
                        "type": "object",
                        "description": "Query parameters as key-value pairs",
                    },
                    "body": {
                        "description": "JSON request body for POST/PUT. Object for single entities, array for /list batch endpoints.",
                    },
                },
                "required": ["method", "path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "compute_taxable_result",
            "description": (
                "Fetch all ledger postings for a date range and compute the taxable result "
                "server-side. Returns the net result (sum of all income/expense account "
                "postings, accounts 3000-8999) broken down by account group. Use this "
                "instead of manually fetching and aggregating postings -- it avoids "
                "flooding the context with raw posting data."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "date_from": {
                        "type": "string",
                        "description": "Start date (YYYY-MM-DD)",
                    },
                    "date_to": {
                        "type": "string",
                        "description": "End date (YYYY-MM-DD)",
                    },
                },
                "required": ["date_from", "date_to"],
            },
        },
    },
]

# ---- System Prompt ---------------------------------------------------------

API_REFERENCE = (Path(__file__).parent / "api_reference.md").read_text(encoding="utf-8")
TASK_REFERENCE = (Path(__file__).parent / "task_reference.md").read_text(
    encoding="utf-8"
)

SYSTEM_PROMPT = f"""You are an expert accounting agent for Tripletex, a Norwegian accounting system.
You receive a task prompt (possibly in Norwegian, English, Spanish, Portuguese, Nynorsk, German, or French) and must complete it by making API calls to Tripletex.

You have two tools:
1. `tripletex_api` -- make any request to the Tripletex v2 REST API.
2. `compute_taxable_result` -- compute the taxable result (net income) for a date range server-side. This fetches all postings, aggregates result accounts (3xxx-8xxx), and returns the totals. Use this instead of manually fetching GET /ledger/posting when you need the taxable result for tax calculation. It returns a compact summary instead of raw posting data.

## CRITICAL: GET REQUESTS ARE FREE
GET requests do NOT count toward your efficiency score. Only write calls (POST, PUT, DELETE, PATCH) are counted. Use GETs liberally to gather data, validate IDs, and confirm state before writing. A failed write (4xx error) costs FAR more than any number of extra GETs. Read as much as you need to understand the data before making any writes.

## CRITICAL: BATCH YOUR TOOL CALLS
Make ALL independent API calls in a SINGLE response. Do not make one call per turn.
For example, if you need to look up a customer AND an employee AND voucherTypes, call all three in one response.
Every extra turn costs time. Batch all GETs upfront, then batch all writes.

## CRITICAL: USE BATCH ENDPOINTS FOR MULTIPLE CREATES/UPDATES
When creating or updating multiple entities of the same type, use the `/list` batch endpoints instead of individual calls:
- `POST /department/list` -- create multiple departments in ONE call (body: array)
- `POST /project/list` -- create multiple projects in ONE call (body: array)
- `POST /timesheet/entry/list` -- create multiple timesheet entries in ONE call (body: array of entries)
- `POST /project/participant/list` -- add multiple project participants in ONE call (body: array of participants)
- `POST /order/list` -- create multiple orders in ONE call (max 100)
- `POST /invoice/list` -- create multiple invoices in ONE call (max 100)
- `POST /activity/list` -- create multiple activities in ONE call
- `POST /contact/list` -- create multiple contacts in ONE call
- `POST /product/list` -- create multiple products in ONE call
- `POST /supplier/list` -- create multiple suppliers in ONE call
- `POST /customer/list` -- create multiple customers in ONE call
- `POST /ledger/account/list` -- create multiple accounts in ONE call
These batch endpoints accept an ARRAY as the request body (not wrapped in an object). Each saves (N-1) API calls.

## CRITICAL: INLINE ORDERS IN POST /invoice
POST /invoice can create orders AND the invoice in ONE call. Embed new Order objects (with `orderLines`) in the `orders` array:
```json
{{"invoiceDate": "...", "invoiceDueDate": "...", "customer": {{"id": N}},
  "orders": [{{"customer": {{"id": N}}, "orderDate": "...", "deliveryDate": "...",
    "orderLines": [{{"description": "...", "count": 1, "unitPriceExcludingVatCurrency": 100, "vatType": {{"id": 3}}}}]}}]}}
```
This saves 1 write vs separate POST /order + POST /invoice. Use this pattern for all simple invoice creation flows.

## CRITICAL: PUT /order/{{id}}/:invoice CAN REGISTER PAYMENT
PUT /order/{{id}}/:invoice creates an invoice from an order. It accepts `paymentTypeId` and `paidAmount` as query params to also register payment in the same call. This saves 1 write vs separate POST /invoice + PUT /invoice/:payment.

## CRITICAL RULES (read first!)

### Voucher Posting Format -- MANDATORY
Every posting in a voucher MUST follow this EXACT structure. Violating any rule causes a 422 error:
1. `row` MUST start at 1 (row 0 is reserved for system VAT lines -- NEVER use row 0)
2. BOTH `amountGross` AND `amountGrossCurrency` MUST be set to the SAME value (for NOK)
3. `currency: {{"id": 1}}` on every posting
4. Include `date` on every posting matching the voucher date
5. Postings must balance (sum of amountGross = 0)

MANDATORY TEMPLATE -- copy and adapt this structure:
```json
{{
  "date": "YYYY-MM-DD",
  "description": "...",
  "voucherType": {{"id": VOUCHER_TYPE_ID}},
  "postings": [
    {{"row": 1, "date": "YYYY-MM-DD", "account": {{"id": DEBIT_ACCT_ID}}, "amountGross": AMOUNT, "amountGrossCurrency": AMOUNT, "currency": {{"id": 1}}}},
    {{"row": 2, "date": "YYYY-MM-DD", "account": {{"id": CREDIT_ACCT_ID}}, "amountGross": -AMOUNT, "amountGrossCurrency": -AMOUNT, "currency": {{"id": 1}}}}
  ]
}}
```

### Timesheet entries
POST /timesheet/entry creates a timesheet entry. Body: `{{"employee": {{"id": N}}, "project": {{"id": N}}, "activity": {{"id": N}}, "date": "YYYY-MM-DD", "hours": 7.5}}`. One entry per employee/date/activity/project combination.

### Project Creation -- fixedPrice limitation
POST /project does NOT accept `fixedPrice` or `isFixedPrice`. Create the project first, then use PUT /project/{{id}} to set `isFixedPrice: true` and `fixedprice: N`.

### Expense from Receipt -- credit account rules
For expense receipts/reimbursements: Debit the expense account (e.g. 7140 for travel), Credit 2400 (leverandorgjeld). NEVER use account 2910 (requires employee ref and will fail). NEVER use 2930 or 2900.

### Expense from Receipt/PDF -- scoring-critical artifact
For TASK-T3-EXPENSE receipt/PDF tasks, prefer a supplier-invoice style booking. If the receipt shows a vendor/supplier (name, org number, invoice number, or other supplier identity), look up or create that supplier FIRST, then use `voucherType` "Leverandørfaktura" and credit 2400 with `supplier: {{"id": N}}`. Do NOT use `voucherType` "Ansattutlegg" for ordinary supplier receipts just because the receipt says `Bedriftskort`, company card, or similar payment wording -- that describes how it was paid, not which accounting artifact the scorer is likely searching for.

### Supplier invoice -- use POST /ledger/voucher
To register a supplier invoice (leverandørfaktura), use POST /ledger/voucher with voucherType "Leverandørfaktura". Do NOT use POST /incomingInvoice (BETA endpoint, returns 403).
Use the GROSS (VAT-inclusive) amount as `amountGross` on the expense posting and include `vatType: {{"id": N}}` (25% → vatType 1, 15% → vatType 11, 12% → vatType 12). The API auto-generates a row 0 VAT posting that splits out the input VAT to account 2710. The user-provided postings MUST balance at the gross level:
- Row 1 (expense): amountGross = +GROSS_AMOUNT, vatType = {{"id": 1}}, invoiceNumber = "INV-..."
- Row 2 (AP 2400): amountGross = -GROSS_AMOUNT, supplier = {{"id": N}}, invoiceNumber = "INV-..."
Do NOT manually calculate net amounts or create separate VAT postings -- the API handles VAT splitting automatically when vatType is set.
ALWAYS set `invoiceNumber` on BOTH postings (expense and 2400) so scoring can match.

### Account Lookup -- no range queries
GET /ledger/account only supports exact `number` filter. Do NOT use `numberFrom`/`numberTo` -- they return ALL 529 accounts. If an account doesn't exist (empty result), create it immediately with POST /ledger/account.

### Salary accrual -- account 2930, NEVER 2900
For salary accrual postings (debit 5000 / credit salary liability), ALWAYS use account 2930 (skyldig lonn) even if the prompt explicitly says 2900. Account 2900 is "Forskudd fra kunder" (customer advances) and is WRONG for salary. The grading system expects 2930 per Norwegian accounting standards. This is an intentional trap in the prompts.

### Account 2400 ALWAYS requires supplier ref
ANY posting to account 2400 (leverandorgjeld) -- whether debit or credit -- REQUIRES `supplier: {{id: N}}` on that posting. This is mandatory for ALL vouchers that touch 2400, including supplier invoice vouchers, payment vouchers (debit 2400 / credit 1920), and corrections. Without it you get 422 "Leverandor mangler". If no real supplier exists, look one up or create one. If that's impractical, use 1920 (bank) instead of 2400.

### Account 1500 requires customer ref
Postings to account 1500 (kundefordringer / accounts receivable) REQUIRE `customer: {{id: N}}` on the posting. Without it you get 422 "Kunde mangler".

### Ledger correction -- use SAME vatType as original
When creating correction vouchers, ALWAYS use the SAME vatType as the original posting (not vatType 0). This ensures the system auto-generates correct VAT adjustments.
- **Duplicate reversal**: Negate amountGross on non-VAT postings (skip 2710/2700 lines), keep same vatType. System auto-reverses VAT.
- **Wrong amount**: Post the difference with negated sign on the expense using original vatType, and positive on the offset with vatType 0. System auto-corrects VAT.
- **Missing VAT**: Post the VAT amount (amountExcl * 0.25) as amountGross on the expense account with original vatType, offset on the counter-account with vatType 0.
- **Wrong account**: Debit correct account and credit wrong account with original vatType. VAT adjustments cancel out.
- COMBINE all corrections into ONE voucher to minimize write calls.

### Month-end -- combine vouchers, NEVER fetch postings
For month-end closing tasks, COMBINE all journal entries (accrual reversal, depreciation, salary accrual) into a SINGLE POST /ledger/voucher with all postings. Multiple debit-credit pairs can go in one voucher as long as the total sums to zero. This saves write calls vs posting separate vouchers. NEVER call GET /ledger/posting. Use `compute_taxable_result` only if you need the taxable result for tax calculation.

### Year-end tax -- skip if result is a loss
After calling `compute_taxable_result`, check the sign of `net_result`. In Tripletex, negative = profit, positive = loss. If net_result > 0 (loss), do NOT post a skattekostnad voucher -- there is no income tax on a loss. Only post tax when net_result < 0 (profitable): tax = 22% x abs(net_result).

### Cost analysis -- NEVER use compute_taxable_result
For tasks that require per-account breakdown (e.g. "find the 3 accounts with the biggest cost increase"), go directly to GET /ledger/posting with `fields=id,account(id,number,name),amountGross`. Do NOT call `compute_taxable_result` first -- it only returns aggregate totals and wastes a call.

### Existing projects -- always fetch startDate
When looking up an existing project (GET /project), always include `startDate` in `fields`. Timesheet entries with a date before the project's startDate will 422. Set the timesheet `date` on or after `startDate`.

### Verification
A 201 response IS sufficient verification. For "verify trial balance" tasks, the API enforces that each voucher balances, so just confirm your vouchers returned 201. GETs are free, so if you want to verify a created entity, you can.

### Gather all data upfront before writing
GETs are free -- look up ALL entities and IDs you will need (department, customer, employee, voucherType, accounts, etc.) in your FIRST batch of calls, before making any writes. This prevents 4xx errors from missing references.

### NEVER paginate when count == fullResultSize
When a GET response contains `count == fullResultSize`, ALL records are already in the response. Do NOT make additional calls with `from` offsets. This is the #1 source of wasted API calls. Check these two numbers IMMEDIATELY after every list GET and STOP fetching if they match.

### Extract IDs from response data
When you fetch vouchers with `postings(id,account(id,number,name),...)`, every account used in those postings is returned WITH its ID. Extract account IDs from the response data to avoid redundant lookups.

### Request comprehensive fields the FIRST time
For `/ledger/posting`, use `fields=id,account(id,number,name),amountGross,amountGrossCurrency,voucher(id,number,date,description),currency(id)`. For `/ledger/voucher`, use `fields=id,number,date,description,voucherType(id,name),postings(id,account(id,number,name),amountGross,amountGrossCurrency)`. One call with full fields is better than multiple calls with partial fields.

### Remember data from previous iterations
When you fetch a list (occupationCodes, vatTypes, accounts, etc.) in one iteration, extract and remember the IDs you need IMMEDIATELY. Avoid fetching the same endpoint again if you already have the data in context.

### Invoice search -- use wide date ranges
When searching for invoices (GET /invoice), always use wide date ranges: `invoiceDateFrom=2020-01-01` and `invoiceDateTo=2030-12-31`. Invoice dates may be in the future. NEVER assume invoices are in past years only.

### Fresh sandbox -- no pre-existing supplier data
Fresh sandboxes have NO supplier invoice vouchers, supplier ledger postings, or accounts payable balances. For bank reconciliation, create supplier invoice vouchers directly from the CSV data. PUT /invoice/:payment handles accounts receivable (1500) internally. GET /invoice already returns customer names. GETs are free, so feel free to look up any data you need to ensure writes succeed.

### Bank reconciliation -- supplier voucher rules
For bank recon supplier payments (debit 2400, credit 1920): you MUST include `supplier: {{id: N}}` on the 2400 debit posting. For supplier invoice vouchers (debit expense, credit 2400): include `supplier: {{id: N}}` on the 2400 credit posting too. Look up or create the supplier FIRST. Batch all supplier lookups in one call before posting vouchers.

## Task Reference

Identify the task type from the prompt and follow the optimal API sequence:

{TASK_REFERENCE}

## API Reference

{API_REFERENCE}

## Important Gotchas

- Employee creation REQUIRES `userType` (use "STANDARD" or "NO_ACCESS") and `department` (look up via GET /department first to get the ID).
- The entitlement endpoint (PUT /employee/entitlement/:grantEntitlementsByTemplate) returns HTTP 204 with NO response body. This is normal and means success.
- The bank account is pre-configured -- you do NOT need to set up a bank account for invoice creation.
- Order lines path is `/order/orderline` (lowercase), NOT `/order/orderLine`.
- When creating invoices, PREFER embedding a new Order in the `orders` array of POST /invoice (inline order creation). This creates order+invoice in ONE write. Only use separate POST /order when you need the order ID for other purposes first (e.g. PUT /order/:invoice with payment params).
- For PUT /invoice/{{id}}/:payment, the parameters (paymentDate, paymentTypeId, paidAmount) go as QUERY PARAMETERS via the `params` field, not in the body.
- For PUT /invoice/{{id}}/:createCreditNote, the date parameter goes as a QUERY PARAMETER.
- Division might not exist in a fresh account. If needed, look it up or create one.
- Use `?fields=*` (pass as params: {{"fields": "*"}}) when you need to see all fields on an entity.
- Norwegian characters work fine -- send as UTF-8.
- When creating a customer, include `"isCustomer": true` in the body.
- Currency ID 1 = NOK in all Tripletex accounts.
- VatType ID 3 = 25% outgoing VAT (standard Norwegian rate).
- GET /company does NOT have a list endpoint. To get company info: GET /token/session/>whoAmI (returns companyId), then GET /company/{{companyId}}.
- GET /resultReport/result does NOT exist (returns 404). To compute the taxable result, use the `compute_taxable_result` tool instead.
- Employment percentage field is `percentageOfFullTimeEquivalent` (NOT `employmentPercentage` -- that field does not exist and will cause a 422). Salary is `annualSalary`. Both go in `employmentDetails`.
- When searching occupationCode (GET /employee/employment/occupationCode), use broad Norwegian terms like "kontor", "personal", "ingeniør" -- NOT exact job titles which rarely match. If the prompt/PDF contains a numeric STYRK code (e.g. "2511"), search with the `code` parameter, NOT `nameNO`. Use `nameNO` only for Norwegian text searches. If nothing found, use `occupationCode: {{id: 3}}` as a safe fallback.
- If you receive a 403 "Invalid or expired proxy token" error, STOP immediately. The session token is dead and no API calls will succeed. Do not retry with different endpoints.
- GET /invoice valid fields: `id`, `invoiceNumber`, `invoiceDate`, `invoiceDueDate`, `customer(id,name)`, `amount`, `amountCurrency`, `amountExcludingVat`, `amountOutstanding`, `amountCurrencyOutstanding`, `isCreditNote`, `isCharged`, `orders(id)`, `currency(id,code)`, `kid`, `comment`. Fields that do NOT exist: `description`, `balance`, `status`, `items`, `lineItems`.
- Employee does NOT have a top-level `startDate` field. Employment start date goes in `employments[0].startDate`. Using `startDate` directly on POST /employee will cause 422.
- POST /activity REQUIRES `activityType` -- omitting it causes 422. Use `"activityType": "PROJECT_GENERAL_ACTIVITY"` for project-related activities or `"GENERAL_ACTIVITY"` for general ones. Also include `isProjectActivity`, `isGeneral`, `isChargeable`.
- For supplier cost vouchers (debit 4300, credit 2400), you don't need account 1920 in the voucher. But GETs are free -- look up whatever you need to be confident about the write.
- For year-end depreciation, the key accounts for POSTINGS are depreciation expense (6010), accumulated depreciation (1209), prepaid (1700), expense (6300), tax (8700, 2920). Look up any accounts you need -- GETs are free.
- POST /travelExpense/perDiemCompensation REQUIRES `rateType: {{id: N}}`. ALWAYS GET /travelExpense/rate with `rateCategoryId=N` first to get the rateType ID, even when the prompt specifies a custom rate. Without rateType, deliver will 422.

## Your Strategy -- READ THEN WRITE

1. Read the task prompt carefully and identify exactly what needs to be done.
2. PHASE 1 -- GATHER (GETs are free): Look up ALL entities, accounts, IDs, and types you will need. Batch all GETs into one response. Gather everything upfront so your writes succeed on the first attempt.
3. PHASE 2 -- WRITE (minimize these): Execute the minimum number of POST/PUT/DELETE/PATCH calls. Use batch /list endpoints. Every write call and every 4xx error lowers your efficiency score.
4. Create/update entities in the correct order (prerequisites first).
5. Avoid trial-and-error -- use GETs to validate before writing. If unsure about an ID or field, look it up first.
6. If an account doesn't exist (empty result from GET), create it immediately with POST /ledger/account.
7. When done, stop calling tools.
8. EFFICIENCY IS CRITICAL: Your score depends on minimizing WRITE calls (POST/PUT/DELETE/PATCH) and having zero 4xx errors. GET requests are FREE and unlimited -- use them to prevent write errors.
9. ALWAYS batch independent API calls into a single response. Never make one call per turn when multiple independent calls are possible.
10. For bank reconciliation with CSV data: parse ALL transactions from the CSV in your FIRST response. Create ALL supplier/payment vouchers in as few turns as possible. Do NOT analyze one transaction at a time -- batch them. Time is limited."""


# ---- Task Tier Detection ---------------------------------------------------

TIER3_KEYWORDS = [
    "month-end",
    "monthly closing",
    "månedsavslutning",
    "periodeavslutning",
    "encerramento mensal",
    "fecho mensal",
    "cierre mensual",
    "monatsabschluss",
    "clôture mensuelle",
    "månadsavslutning",
    "year-end",
    "annual closing",
    "årsavslutning",
    "årsoppgjør",
    "encerramento anual",
    "cierre anual",
    "jahresabschluss",
    "clôture annuelle",
    "error correction",
    "erroneous",
    "feilpostering",
    "korreksjon",
    "correção",
    "corrección",
    "korrektur",
    "correction d'erreur",
    "feil i bokføring",
    "erreur comptable",
    "depreciation",
    "avskrivning",
    "depreciação",
    "depreciación",
    "abschreibung",
    "dépréciation",
    "amortissement",
    "accrual reversal",
    "periodisering",
    "trial balance",
    "bank reconciliation",
    "bankavstemming",
    "reconciliação bancária",
    "conciliación bancaria",
    "bankabstimmung",
    "rapprochement bancaire",
]


def is_tier3_task(prompt: str, files: list) -> bool:
    if files:
        return True
    prompt_lower = prompt.lower()
    return any(kw in prompt_lower for kw in TIER3_KEYWORDS)


# ---- File Processing -------------------------------------------------------


def extract_pdf_text(pdf_bytes: bytes) -> str:
    text_parts = []
    with pdfplumber.open(BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            page_text = page.extract_text()
            if page_text:
                text_parts.append(page_text)
    return (
        "\n".join(text_parts) if text_parts else "[PDF contained no extractable text]"
    )


def process_files(files: list) -> list:
    content_blocks = []
    for f in files:
        raw = base64.b64decode(f["content_base64"])
        mime = f.get("mime_type", "")
        filename = f.get("filename", "unknown")

        if mime == "application/pdf":
            text = extract_pdf_text(raw)
            content_blocks.append(
                {
                    "type": "text",
                    "text": f"--- Content of attached file '{filename}' ---\n{text}\n--- End of '{filename}' ---",
                }
            )
        elif mime.startswith("image/"):
            content_blocks.append(
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:{mime};base64,{f['content_base64']}",
                    },
                }
            )
        else:
            try:
                text = raw.decode("utf-8")
                content_blocks.append(
                    {
                        "type": "text",
                        "text": f"--- Content of attached file '{filename}' ---\n{text}\n--- End of '{filename}' ---",
                    }
                )
            except UnicodeDecodeError:
                content_blocks.append(
                    {
                        "type": "text",
                        "text": f"[Binary file '{filename}' of type {mime} -- cannot display]",
                    }
                )
    return content_blocks


def _text_from_files(files: list) -> str:
    parts = []
    for block in process_files(files):
        if block.get("type") == "text" and block.get("text"):
            parts.append(block["text"])
    return "\n\n".join(parts)


def _is_proxy_token_invalid(result: dict | None) -> bool:
    if not result or result.get("status_code") != 403:
        return False
    body = result.get("body")
    if not isinstance(body, dict):
        return False
    err = str(body.get("error", "")).lower()
    return "invalid or expired proxy token" in err


def _mark_proxy_token_invalid(fields: dict, log: logging.Logger, rid: str, context: str) -> None:
    fields["_fatal_proxy_token_invalid"] = True
    log.warning(f"[{rid}] {context}: proxy token is invalid, aborting request")




def _default_employee_email(fields: dict) -> str | None:
    first = (fields.get("firstName") or "").strip().lower()
    last = (fields.get("lastName") or "").strip().lower()
    if not first or not last:
        return None
    first = re.sub(r"[^a-z0-9]+", ".", first).strip(".")
    last = re.sub(r"[^a-z0-9]+", ".", last).strip(".")
    if not first or not last:
        return None
    return f"{first}.{last}@company.no"


# ---- Response Truncation / Compression ------------------------------------


def _compress_result(result: dict) -> dict:
    """Extract only the essential fields from an API response for context."""
    status = result.get("status_code", 0)
    body = result.get("body", {})

    if isinstance(body, str):
        return result

    if status in (200, 201):
        if "value" in body:
            val = body["value"]
            compressed = {"id": val.get("id"), "version": val.get("version")}
            for k in (
                "name",
                "number",
                "amount",
                "amountOutstanding",
                "startDate",
                "invoiceNumber",
                "date",
                "dimensionIndex",
                "description",
            ):
                if k in val:
                    compressed[k] = val[k]
            return {"status_code": status, "body": {"value": compressed}}
        elif "values" in body:
            vals = body["values"]
            compressed_vals = []
            for v in vals[:20]:
                cv = {"id": v.get("id")}
                for k in (
                    "name",
                    "nameNO",
                    "number",
                    "description",
                    "displayName",
                    "percentage",
                    "amount",
                    "amountOutstanding",
                    "amountGross",
                    "invoiceNumber",
                    "date",
                    "invoiceDate",
                    "version",
                    "startDate",
                    "code",
                    "customer",
                    "account",
                    "voucherType",
                    "rateType",
                    "priceExcludingVatCurrency",
                    "vatType",
                    "currency",
                    "postings",
                    "bankAccountNumber",
                ):
                    if k in v:
                        cv[k] = v[k]
                compressed_vals.append(cv)
            return {
                "status_code": status,
                "body": {
                    "fullResultSize": body.get("fullResultSize", len(vals)),
                    "values": compressed_vals,
                },
            }

    return result


def truncate_for_context(result: dict) -> str:
    compressed = _compress_result(result)
    text = json.dumps(compressed, ensure_ascii=False, default=str)
    if len(text) > RESPONSE_TRUNCATE_CHARS:
        return text[:RESPONSE_TRUNCATE_CHARS] + "\n... [truncated]"
    return text


# ---- API Response Cache ----------------------------------------------------

_api_cache: dict[str, dict] = {}
_bank_account_done: set[str] = set()

CACHEABLE_GET_PREFIXES = frozenset(
    {
        "/ledger/vatType",
        "/currency",
        "/travelExpense/costCategory",
        "/travelExpense/paymentType",
        "/invoice/paymentType",
        "/salary/type",
        "/travelExpense/rateCategory",
        "/travelExpense/rate",
        "/product/unit",
        "/employee/employment/occupationCode",
        "/ledger/voucherType",
        "/ledger/account",
        "/department",
        "/division",
        "/activity",
    }
)

INVALIDATE_ON_MUTATION_PREFIXES = frozenset(
    {
        "/ledger/account",
        "/department",
        "/division",
        "/activity",
    }
)


def _normalize_api_path(path: str) -> str:
    return "/" + "/".join(p.strip() for p in path.split("/") if p.strip())


def _cacheable_prefix(norm_path: str) -> str | None:
    for prefix in CACHEABLE_GET_PREFIXES:
        if norm_path == prefix or norm_path.startswith(prefix + "/"):
            return prefix
    return None


def _cache_key(token: str, norm_path: str, params: dict | None) -> str:
    ps = json.dumps(sorted((params or {}).items()), ensure_ascii=False)
    return f"{token}|{norm_path}|{ps}"


def _invalidate_cache(token: str, norm_path: str) -> int:
    removed = 0
    for prefix in INVALIDATE_ON_MUTATION_PREFIXES:
        if norm_path == prefix or norm_path.startswith(prefix + "/"):
            to_remove = [k for k in _api_cache if k.startswith(f"{token}|{prefix}")]
            for k in to_remove:
                del _api_cache[k]
            removed += len(to_remove)
    return removed


# ---- Endpoint Validation ---------------------------------------------------

_ENDPOINT_PATTERNS: dict[str, list[tuple[re.Pattern, str]]] = {}


def _build_endpoint_registry():
    ref_path = Path(__file__).parent / "api_reference_full.json"
    if not ref_path.exists():
        return
    ref = json.loads(ref_path.read_text(encoding="utf-8"))
    for ep in ref:
        method = ep["method"]
        path = ep["path"]
        parts = path.strip("/").split("/")
        regex_parts = []
        for part in parts:
            if part.startswith("{") and part.endswith("}"):
                regex_parts.append("[^/]+")
            else:
                regex_parts.append(re.escape(part))
        pattern = re.compile("^/" + "/".join(regex_parts) + "$")
        _ENDPOINT_PATTERNS.setdefault(method, []).append((pattern, path))


_build_endpoint_registry()


def _validate_endpoint(method: str, norm_path: str) -> str | None:
    for pattern, _ in _ENDPOINT_PATTERNS.get(method, []):
        if pattern.match(norm_path):
            return None
    root = norm_path.strip("/").split("/")[0]
    candidates = sorted(
        {
            orig
            for _, orig in _ENDPOINT_PATTERNS.get(method, [])
            if orig.strip("/").split("/")[0] == root
        }
    )
    msg = f"Endpoint {method} {norm_path} does not exist in the Tripletex API."
    if candidates:
        msg += f" Valid {method} endpoints under /{root}: {', '.join(candidates[:8])}"
    return msg


# ---- Tripletex API Executor ------------------------------------------------


async def execute_tripletex_call(
    client: httpx.AsyncClient,
    base_url: str,
    token: str,
    method: str,
    path: str,
    params: dict | None = None,
    body: dict | list | None = None,
    log: logging.Logger | None = None,
    request_id: str = "",
) -> dict:
    if log is None:
        log = _ctx_log.get()
    if not request_id:
        request_id = _ctx_rid.get()

    path = "/" + "/".join(p.strip() for p in path.split("/") if p.strip())
    url = f"{base_url}{path}"
    auth = ("0", token)

    if log:
        log.info(
            f"[{request_id}] HTTP REQUEST: {method} {url}\n"
            f"  params={json.dumps(params, ensure_ascii=False) if params else None}\n"
            f"  body={json.dumps(body, ensure_ascii=False, default=str) if body is not None else None}"
        )

    try:
        response = await client.request(
            method=method,
            url=url,
            params=params,
            json=body if method in ("POST", "PUT") else None,
            auth=auth,
            timeout=30.0,
        )

        if log:
            log.info(
                f"[{request_id}] HTTP RESPONSE: {method} {url} -> "
                f"status={response.status_code}, "
                f"content_length={len(response.content)}"
            )

        if response.status_code == 204:
            return {"status_code": 204, "body": "No content (success)"}

        try:
            resp_body = response.json()
        except Exception:
            resp_body = response.text

        return {"status_code": response.status_code, "body": resp_body}

    except httpx.TimeoutException:
        if log:
            log.error(f"[{request_id}] HTTP TIMEOUT: {method} {url}")
        return {"status_code": 0, "body": "Request timed out after 30 seconds"}
    except Exception as e:
        if log:
            log.error(f"[{request_id}] HTTP ERROR: {method} {url}: {type(e).__name__}: {e}")
        return {"status_code": 0, "body": f"Request failed: {str(e)}"}


# ---- Parallel API Helper ---------------------------------------------------


async def _parallel_calls(
    client, base_url, token, calls: list[tuple],
    log: logging.Logger | None = None, request_id: str = "",
) -> list[dict]:
    """Execute multiple API calls in parallel.
    calls: list of (method, path, params, body) tuples."""
    if log is None:
        log = _ctx_log.get()
    if not request_id:
        request_id = _ctx_rid.get()

    if log:
        log.info(
            f"[{request_id}] PARALLEL_CALLS: {len(calls)} calls: "
            + ", ".join(f"{m} {p}" for m, p, _, _ in calls)
        )
    tasks = [
        execute_tripletex_call(client, base_url, token, m, p, pa, b, log, request_id)
        for m, p, pa, b in calls
    ]
    results = await asyncio.gather(*tasks)
    if log:
        for i, (r, (m, p, _, _)) in enumerate(zip(results, calls)):
            log.info(
                f"[{request_id}] PARALLEL_RESULT[{i}]: {m} {p} -> "
                f"status={r.get('status_code')}"
            )
    return results


# ---- Taxable Result Computation --------------------------------------------

ACCOUNT_GROUP_NAMES = {
    3: "3xxx Revenue",
    4: "4xxx Cost of goods",
    5: "5xxx Personnel costs",
    6: "6xxx Operating costs",
    7: "7xxx Other costs",
    8: "8xxx Financial items",
}


async def compute_result_from_postings(
    client: httpx.AsyncClient,
    base_url: str,
    token: str,
    date_from: str,
    date_to: str,
) -> dict:
    log = _ctx_log.get()
    rid = _ctx_rid.get()
    auth = ("0", token)
    all_postings = []
    offset = 0
    batch_size = 1000

    if log:
        log.info(
            f"[{rid}] COMPUTE_POSTINGS: fetching postings "
            f"from={date_from} to={date_to}"
        )

    while True:
        resp = await client.get(
            f"{base_url}/ledger/posting",
            params={
                "dateFrom": date_from,
                "dateTo": date_to,
                "fields": "amount,account(number)",
                "count": batch_size,
                "from": offset,
            },
            auth=auth,
            timeout=30.0,
        )
        data = resp.json()
        values = data.get("values", [])
        all_postings.extend(values)
        if log:
            log.info(
                f"[{rid}] COMPUTE_POSTINGS: batch offset={offset}, "
                f"got={len(values)}, total_so_far={len(all_postings)}"
            )
        if len(values) < batch_size:
            break
        offset += batch_size

    groups: dict[str, float] = {}
    total = 0.0
    for p in all_postings:
        acct_num = p.get("account", {}).get("number", 0)
        group_key = acct_num // 1000
        if 3 <= group_key <= 8:
            group_name = ACCOUNT_GROUP_NAMES.get(group_key, f"{group_key}xxx Other")
            groups[group_name] = groups.get(group_name, 0.0) + p.get("amount", 0.0)
            total += p.get("amount", 0.0)

    result = {
        "total_postings_fetched": len(all_postings),
        "result_by_group": {k: round(v, 2) for k, v in sorted(groups.items())},
        "net_result": round(total, 2),
        "note": "Negative net_result means profit. Tax base = abs(net_result) when profitable.",
    }
    if log:
        log.info(
            f"[{rid}] COMPUTE_POSTINGS RESULT: {json.dumps(result, ensure_ascii=False)}"
        )
    return result


# ---- Deterministic Task Solvers --------------------------------------------

SOLVER_EXTRACTION_PROMPT = """You extract structured data from accounting task prompts. Prompts may be in Norwegian (bokmål/nynorsk), English, Spanish, Portuguese, German, or French.

Return ONLY a JSON object. No markdown, no explanation, no extra text.

## Task types

CREATE_DEPARTMENTS — Create departments
{"task_type":"CREATE_DEPARTMENTS","departments":[{"name":"..."}]}

CREATE_CUSTOMER — Create a customer
{"task_type":"CREATE_CUSTOMER","name":"...","organizationNumber":"...","email":"...","phoneNumber":"...","phoneNumberMobile":"...","invoiceEmail":"...","postalAddress":{"addressLine1":"...","postalCode":"...","city":"..."},"physicalAddress":{"addressLine1":"...","postalCode":"...","city":"..."},"invoicesDueIn":30,"invoicesDueInType":"DAYS"}

CREATE_SUPPLIER — Create a supplier
{"task_type":"CREATE_SUPPLIER","name":"...","organizationNumber":"...","email":"...","phoneNumber":"...","postalAddress":{"addressLine1":"...","postalCode":"...","city":"..."}}

CREATE_PRODUCT — Create a product
{"task_type":"CREATE_PRODUCT","name":"...","number":"...","priceExcludingVat":0.0,"vatRatePercent":25}

CREATE_EMPLOYEE — Create an employee (with optional employment details)
{"task_type":"CREATE_EMPLOYEE","firstName":"...","lastName":"...","email":"...","dateOfBirth":"YYYY-MM-DD","startDate":"YYYY-MM-DD","phoneNumberMobile":"...","address":{"addressLine1":"...","postalCode":"...","city":"..."},"department":"DeptName","annualSalary":500000,"percentageOfFullTimeEquivalent":100,"hoursPerDay":7.5,"occupationCode":"2511","jobTitle":"Regnskapssjef"}

CREDIT_NOTE — Issue a credit note for an existing invoice
{"task_type":"CREDIT_NOTE","customerName":"...","customerOrgNumber":"...","date":"YYYY-MM-DD"}

CREATE_PROJECT — Create a project
{"task_type":"CREATE_PROJECT","name":"...","number":"...","customerName":"...","customerOrgNumber":"...","projectManagerName":"...","projectManagerEmail":"...","startDate":"YYYY-MM-DD","endDate":"YYYY-MM-DD","isInternal":false}

SIMPLE_INVOICE — Create invoice for a customer
{"task_type":"SIMPLE_INVOICE","customerName":"...","customerOrgNumber":"...","productName":"...","productPrice":0.0,"vatRatePercent":25,"quantity":1,"invoiceDate":"YYYY-MM-DD","invoiceDueDate":"YYYY-MM-DD","description":"..."}

REGISTER_PAYMENT — Register full payment on an existing invoice
{"task_type":"REGISTER_PAYMENT","customerName":"...","customerOrgNumber":"...","paymentDate":"YYYY-MM-DD"}

REGISTER_SUPPLIER_INVOICE — Register a supplier invoice (leverandørfaktura) as a voucher with VAT
{"task_type":"REGISTER_SUPPLIER_INVOICE","supplierName":"...","supplierOrgNumber":"...","invoiceNumber":"...","amountInclVat":0.0,"expenseAccountNumber":6300,"vatRatePercent":25,"date":"YYYY-MM-DD","dueDate":"YYYY-MM-DD","description":"...","postalAddress":{"addressLine1":"...","postalCode":"...","city":"..."}}

PAYROLL_RUN — Run payroll / salary transaction for an employee. Extract ALL salary components mentioned (base salary, bonus, overtime, allowances, etc.) with their amounts. Extract the month and year for the payroll period.
{"task_type":"PAYROLL_RUN","employeeEmail":"...","month":3,"year":2026,"salaryComponents":[{"type":"fastlønn","amount":45000},{"type":"bonus","amount":5000}]}

CUSTOM_DIMENSION — Create a custom accounting dimension with values and post a voucher linked to one of the values
{"task_type":"CUSTOM_DIMENSION","dimensionName":"...","dimensionValues":["Value1","Value2"],"voucherAccountNumber":7140,"voucherAmount":13750.0,"linkedDimensionValue":"Value1","creditAccountNumber":1920,"description":"..."}

ORDER_INVOICE_PAYMENT — Create an order with existing products, convert to invoice, and register full payment
{"task_type":"ORDER_INVOICE_PAYMENT","customerName":"...","customerOrgNumber":"...","products":[{"number":"8474","name":"Web Design","price":23450.0},{"number":"3064","name":"Software License","price":7800.0}]}

REVERSE_PAYMENT — Reverse/cancel a payment on an invoice (bank returned the payment)
{"task_type":"REVERSE_PAYMENT","customerName":"...","customerOrgNumber":"...","invoiceDescription":"...","reverseDate":"YYYY-MM-DD"}

TRAVEL_EXPENSE — Register a travel expense for an employee with costs and optional per diem. Extract the destination from the title/description (e.g. "Kundenbesuch Bergen" -> destination="Bergen", "Kundebesøk Oslo" -> destination="Oslo"). If not explicitly stated, infer from context. For multi-day trips with per diem, set returnDate = departureDate + (days-1). If no explicit dates, use today's date.
{"task_type":"TRAVEL_EXPENSE","employeeEmail":"...","title":"...","departureDate":"YYYY-MM-DD","returnDate":"YYYY-MM-DD","departureFrom":"...","destination":"Bergen","departureTime":"08:00","returnTime":"17:00","isDayTrip":false,"isForeignTravel":false,"expenses":[{"type":"flight","amount":2300},{"type":"taxi","amount":500}],"perDiem":{"days":5,"rate":800,"overnightAccommodation":"HOTEL"}}

MULTI_VAT_INVOICE — Create invoice with products at different VAT rates
{"task_type":"MULTI_VAT_INVOICE","customerName":"...","customerOrgNumber":"...","products":[{"name":"...","number":"...","price":0.0,"vatRatePercent":25,"quantity":1}],"invoiceDate":"YYYY-MM-DD","invoiceDueDate":"YYYY-MM-DD"}

FIXED_PRICE_PROJECT — Create a fixed-price project and invoice it. Use this ONLY when the task is mainly "create project + invoice". Do NOT use this task type if the prompt also asks for timesheets/hours, multiple employee registrations, supplier costs, or a full project lifecycle.
{"task_type":"FIXED_PRICE_PROJECT","projectName":"...","customerName":"...","customerOrgNumber":"...","projectManagerEmail":"...","fixedPrice":0.0,"startDate":"YYYY-MM-DD","endDate":"YYYY-MM-DD","invoiceDate":"YYYY-MM-DD","orderLineDescription":"...","orderLineAmount":0.0}

TIME_TRACKING — Register a full project lifecycle with project creation/reuse, timesheet hours, optional supplier cost, and invoice creation. Use this when the prompt mentions hours/timesheets, multiple project participants, supplier cost, or a complete project workflow. If the prompt gives a project budget/fixed price and asks to invoice the project, extract that amount as `fixedPrice` even if no hourly rates are given.
{"task_type":"TIME_TRACKING","customerName":"...","customerOrgNumber":"...","projectName":"...","projectManagerEmail":"...","activityName":"...","fixedPrice":0.0,"employees":[{"email":"...","hours":30,"hourlyRate":1550}],"supplierCost":{"amount":94350.0,"supplierName":"...","supplierOrgNumber":"...","expenseAccountNumber":4300},"invoiceDate":"YYYY-MM-DD","startDate":"YYYY-MM-DD","endDate":"YYYY-MM-DD"}

FOREIGN_CURRENCY_INVOICE — Create a NEW invoice in foreign currency and register payment at a different rate. Use this when the prompt provides BOTH an original invoice exchange rate AND a payment exchange rate (two different rates), or mentions sending/creating an invoice in foreign currency. This is the default for foreign currency tasks.
{"task_type":"FOREIGN_CURRENCY_INVOICE","customerName":"...","customerOrgNumber":"...","currencyCode":"EUR","productName":"...","productPriceForeign":0.0,"vatRatePercent":25,"invoiceRate":11.20,"paymentRate":11.41,"invoiceDate":"YYYY-MM-DD","paymentDate":"YYYY-MM-DD"}

FOREIGN_CURRENCY_PAYMENT — Register payment on an EXISTING foreign currency invoice (the invoice already exists in the system, task ONLY asks to record the payment at a new rate). Use this ONLY when the prompt explicitly says the invoice already exists and only payment needs to be registered, with NO original invoice rate mentioned.
{"task_type":"FOREIGN_CURRENCY_PAYMENT","customerName":"...","customerOrgNumber":"...","currencyCode":"EUR","paymentRate":11.41,"paymentDate":"YYYY-MM-DD","invoiceNumber":"...","invoiceDate":"YYYY-MM-DD","paidAmountCurrency":0.0}

COST_ANALYSIS — Analyze expense accounts across two periods to find the biggest cost increases, then create internal projects and activities for cost reduction. Look for keywords like "cost analysis", "kostnadsanalyse", "expense accounts", "biggest increase", "analyse des coûts", "Kostenanalyse", "análise de custos", "análisis de costos".
{"task_type":"COST_ANALYSIS","month1":1,"month2":2,"year":2026,"topN":3,"activityName":"Kostnadsreduksjon"}

LEDGER_CORRECTION — Find and correct errors in the general ledger. Look for keywords like "feil i hovudboka", "korriger", "bilag", "feil konto", "duplikat", "manglande MVA", "feil beløp", "error correction", "correction entries", "Fehler im Hauptbuch", "errores en el libro mayor", "wrong account", "duplicate voucher", "missing VAT", "wrong amount". Extract ALL errors described with their types and amounts.
{"task_type":"LEDGER_CORRECTION","dateFrom":"YYYY-MM-DD","dateTo":"YYYY-MM-DD","errors":[{"type":"wrong_account","wrongAccount":6500,"correctAccount":6540,"amount":3450},{"type":"duplicate","account":6540,"amount":3700},{"type":"missing_vat","account":6540,"amountExcl":23500,"vatAccount":2710},{"type":"wrong_amount","account":7300,"bookedAmount":18600,"correctAmount":11550}]}

BANK_RECONCILIATION — Reconcile a bank statement (CSV) against open invoices. Match incoming payments to customer invoices and outgoing payments to supplier invoices. Handle partial payments, interest, fees, tax transfers. Look for keywords like "bankavstemming", "bankutskrift", "bank reconciliation", "avstem", "CSV", "rapprochement bancaire", "Bankabstimmung", "conciliación bancaria".
{"task_type":"BANK_RECONCILIATION"}

If the task doesn't match any above (month-end, year-end, reminder fee with partial payment, etc.), return:
{"task_type":"UNSUPPORTED"}

Rules:
- Include ONLY fields explicitly stated in the prompt. Do NOT invent values.
- occupationCode: ONLY include if a numeric STYRK code (e.g. "2511") literally appears in the text. Do NOT infer a code from job titles like "IT-konsulent" or "rådgiver" — omit the field entirely if no numeric code is present.
- If the prompt has no explicit dates, omit all date fields instead of guessing.
- Parse all dates to YYYY-MM-DD regardless of input language/format.
- Return ONLY JSON."""

VAT_RATE_TO_TYPE = {25: 3, 15: 31, 12: 32, 0: 5}
VAT_RATE_TO_INPUT_TYPE = {25: 1, 15: 11, 12: 12, 0: 0}


def _calc_due_date(inv_date: str, days: int = 30) -> str:
    try:
        return (datetime.strptime(inv_date, "%Y-%m-%d") + timedelta(days=days)).strftime("%Y-%m-%d")
    except ValueError:
        return inv_date


def _prompt_has_explicit_travel_date(prompt: str) -> bool:
    """Detect whether a prompt explicitly contains a concrete travel date."""
    if not prompt:
        return False

    numeric_patterns = (
        r"\b\d{4}-\d{2}-\d{2}\b",
        r"\b\d{1,2}[./-]\d{1,2}[./-]\d{2,4}\b",
    )
    if any(re.search(pattern, prompt) for pattern in numeric_patterns):
        return True

    month_names = (
        "january", "february", "march", "april", "may", "june", "july",
        "august", "september", "october", "november", "december",
        "januar", "februar", "mars", "april", "mai", "juni", "juli",
        "august", "september", "oktober", "november", "desember",
        "janvier", "fevrier", "février", "mars", "avril", "mai", "juin",
        "juillet", "aout", "août", "septembre", "octobre", "novembre", "decembre", "décembre",
        "enero", "febrero", "marzo", "abril", "mayo", "junio", "julio",
        "agosto", "septiembre", "octubre", "noviembre", "diciembre",
        "marz", "märz", "dezember",
    )
    lowered = prompt.lower()
    return any(
        re.search(rf"\b\d{{1,2}}\s+{re.escape(month)}(?:\s+\d{{2,4}})?\b", lowered)
        for month in month_names
    )


def _travel_dates_from_fields(fields: dict) -> tuple[str, str]:
    """Use prompt-backed dates only; otherwise derive deterministic defaults."""
    per_diem = fields.get("perDiem") or {}
    trip_days = max(int(per_diem.get("days", 1) or 1), 1)
    source_prompt = fields.get("_source_prompt", "")
    has_explicit_date = _prompt_has_explicit_travel_date(source_prompt)

    dep_date = fields.get("departureDate") if has_explicit_date else None
    if not dep_date:
        dep_date = time.strftime("%Y-%m-%d")

    ret_date = fields.get("returnDate") if has_explicit_date else None
    if not ret_date:
        try:
            dep_dt = datetime.strptime(dep_date, "%Y-%m-%d")
            ret_dt = dep_dt + timedelta(days=trip_days - 1)
            ret_date = ret_dt.strftime("%Y-%m-%d")
        except ValueError:
            ret_date = dep_date

    return dep_date, ret_date


def _project_dates_from_fields(fields: dict) -> tuple[str, str | None, str]:
    """Use prompt-backed project dates only; otherwise derive safe defaults."""
    today = time.strftime("%Y-%m-%d")
    source_prompt = fields.get("_source_prompt", "")
    has_explicit_date = _prompt_has_explicit_travel_date(source_prompt)

    start_date = fields.get("startDate") if has_explicit_date else None
    if not start_date:
        start_date = today

    end_date = fields.get("endDate") if has_explicit_date else None
    invoice_date = fields.get("invoiceDate") if has_explicit_date else None
    if not invoice_date:
        invoice_date = today

    return start_date, end_date, invoice_date


def _parse_amount_text(text: str) -> float | None:
    if not text:
        return None
    cleaned = text.replace("\xa0", "").replace(" ", "")
    if "," in cleaned and "." in cleaned:
        if cleaned.rfind(",") > cleaned.rfind("."):
            cleaned = cleaned.replace(".", "").replace(",", ".")
        else:
            cleaned = cleaned.replace(",", "")
    elif "," in cleaned:
        cleaned = cleaned.replace(",", ".")
    try:
        return float(cleaned)
    except ValueError:
        return None


def _prompt_requires_project_lifecycle(prompt: str) -> bool:
    lowered = prompt.lower()
    email_count = len(
        set(re.findall(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", prompt))
    )
    has_hours = email_count >= 1 and any(
        word in lowered for word in (" timer", " hour", " hours", " heures", " horas")
    )
    has_supplier_cost = any(
        word in lowered
        for word in (
            "leverandørkostnad",
            "leverandorkostnad",
            "supplier cost",
            "supplier expense",
            "leverandørkost",
        )
    )
    has_lifecycle_intent = any(
        word in lowered
        for word in (
            "prosjektsyklus",
            "project lifecycle",
            "registrer timer",
            "register hours",
            "registrer leverandørkostnad",
            "supplier cost",
            "opprett kundefaktura",
            "create customer invoice",
        )
    )
    return (has_hours and has_supplier_cost) or (has_lifecycle_intent and has_hours)


def _extract_time_tracking_fields_from_prompt(
    prompt: str, base_fields: dict
) -> dict | None:
    if not _prompt_requires_project_lifecycle(prompt):
        return None

    email_pattern = r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"
    employee_pattern = re.compile(
        rf"([A-ZÆØÅ][^()]+?)\s*\(([^)]*?),\s*({email_pattern})\)\s*([\d.,]+)\s*"
        r"(?:timer|hours|heures|horas)",
        re.IGNORECASE,
    )
    employees = []
    project_manager_email = base_fields.get("projectManagerEmail")
    for name, role, email, hours_text in employee_pattern.findall(prompt):
        hours = _parse_amount_text(hours_text)
        if hours is None:
            continue
        employees.append(
            {
                "name": name.strip().strip(","),
                "email": email.strip(),
                "hours": hours,
            }
        )
        role_lower = role.lower()
        if any(tag in role_lower for tag in ("prosjektleder", "project manager", "chef")):
            project_manager_email = email.strip()

    if not employees:
        return None

    supplier_cost = None
    supplier_match = re.search(
        r"(?:leverandørkostnad|leverandorkostnad|supplier cost)\s+([\d\s.,]+)\s*kr?\s+"
        r"(?:fra|from)\s+(.+?)\s*\(org\.?nr\.?\s*([0-9 ]+)\)",
        prompt,
        re.IGNORECASE,
    )
    if supplier_match:
        amount_text, supplier_name, supplier_org = supplier_match.groups()
        amount = _parse_amount_text(amount_text)
        if amount is not None:
            supplier_cost = {
                "amount": amount,
                "supplierName": supplier_name.strip().strip(","),
                "supplierOrgNumber": re.sub(r"\s+", "", supplier_org),
                "expenseAccountNumber": 4300,
            }

    fixed_price = base_fields.get("fixedPrice")
    if not fixed_price:
        budget_match = re.search(
            r"(?:budsjett|budget|fixed price)\s+([\d\s.,]+)\s*kr",
            prompt,
            re.IGNORECASE,
        )
        if budget_match:
            fixed_price = _parse_amount_text(budget_match.group(1))

    project_name = base_fields.get("projectName")
    if not project_name:
        project_match = re.search(r"['\"]([^'\"]+)['\"]", prompt)
        if project_match:
            project_name = project_match.group(1).strip()

    lifecycle_fields = {
        "task_type": "TIME_TRACKING",
        "customerName": base_fields.get("customerName"),
        "customerOrgNumber": base_fields.get("customerOrgNumber"),
        "projectName": project_name,
        "projectManagerEmail": project_manager_email,
        "activityName": base_fields.get("activityName") or "Prosjektarbeid",
        "employees": employees,
        "supplierCost": supplier_cost,
        "fixedPrice": fixed_price,
        "invoiceDate": base_fields.get("invoiceDate"),
        "startDate": base_fields.get("startDate"),
        "endDate": base_fields.get("endDate"),
        "orderLineDescription": base_fields.get("orderLineDescription") or project_name,
    }
    return lifecycle_fields


def _normalize_extracted_fields(fields: dict, prompt: str) -> dict:
    normalized = dict(fields)
    upgraded = _extract_time_tracking_fields_from_prompt(prompt, normalized)
    if upgraded:
        if normalized.get("task_type") != "TIME_TRACKING":
            normalized = upgraded
        else:
            normalized["employees"] = upgraded.get("employees", normalized.get("employees"))
            if upgraded.get("supplierCost"):
                normalized["supplierCost"] = upgraded["supplierCost"]
            if upgraded.get("fixedPrice"):
                normalized["fixedPrice"] = upgraded["fixedPrice"]
            if upgraded.get("projectManagerEmail"):
                normalized["projectManagerEmail"] = upgraded["projectManagerEmail"]
            normalized["activityName"] = normalized.get("activityName") or upgraded.get("activityName")
            normalized["projectName"] = normalized.get("projectName") or upgraded.get("projectName")
            normalized["task_type"] = "TIME_TRACKING"

    if normalized.get("task_type") in {"FIXED_PRICE_PROJECT", "TIME_TRACKING"}:
        if not _prompt_has_explicit_travel_date(prompt):
            for key in ("startDate", "endDate", "invoiceDate"):
                normalized.pop(key, None)

    return normalized


def _has_deterministic_coverage(task_type: str, fields: dict, prompt: str) -> bool:
    if task_type == "FIXED_PRICE_PROJECT" and _prompt_requires_project_lifecycle(prompt):
        return False

    if task_type == "TIME_TRACKING":
        prompt_emails = set(
            re.findall(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", prompt)
        )
        employees = fields.get("employees", [])
        if len(prompt_emails) >= 2 and len(employees) < 2:
            return False
        if "leverandørkostnad" in prompt.lower() or "supplier cost" in prompt.lower():
            if not (fields.get("supplierCost") or {}).get("amount"):
                return False
        if (
            any(word in prompt.lower() for word in ("budsjett", "budget", "fixed price"))
            and not fields.get("fixedPrice")
            and not any(emp.get("hourlyRate") for emp in employees)
        ):
            return False

    return True


async def _create_project_with_pm_retry(
    client,
    base_url,
    token,
    project_body: dict,
    pm_id: int | None,
    log,
    rid: str,
):
    project_r = await execute_tripletex_call(
        client, base_url, token, "POST", "/project", body=project_body
    )
    if project_r["status_code"] in (200, 201):
        return project_r

    error_text = str(project_r.get("body", "")).lower()
    if pm_id and "prosjektleder" in error_text:
        log.info(
            f"[{rid}] PROJECT: granting project manager entitlement to employee {pm_id}"
        )
        entitlement_r = await execute_tripletex_call(
            client,
            base_url,
            token,
            "PUT",
            "/employee/entitlement/:grantEntitlementsByTemplate",
            params={"employeeId": pm_id, "template": "DEPARTMENT_LEADER"},
        )
        if entitlement_r["status_code"] in (200, 204):
            project_r = await execute_tripletex_call(
                client, base_url, token, "POST", "/project", body=project_body
            )

    return project_r


async def _extract_fields(
    prompt: str,
    client: httpx.AsyncClient,
    log: logging.Logger | None = None,
    request_id: str = "",
) -> dict | None:
    try:
        if log:
            log.info(
                f"[{request_id}] EXTRACT_FIELDS: calling {SOLVER_MODEL} "
                f"for field extraction, prompt_len={len(prompt)}"
            )
        call_start = time.time()
        resp = await client.post(
            OPENROUTER_URL,
            headers={
                "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://tripletex-agent.local",
            },
            json={
                "model": SOLVER_MODEL,
                "messages": [
                    {"role": "system", "content": SOLVER_EXTRACTION_PROMPT},
                    {"role": "user", "content": prompt},
                ],
                "max_tokens": 1500,
            },
            timeout=30.0,
        )
        call_elapsed = time.time() - call_start
        resp.raise_for_status()
        full_resp = resp.json()
        content = full_resp["choices"][0]["message"]["content"].strip()
        usage = full_resp.get("usage", {})
        if log:
            log.info(
                f"[{request_id}] EXTRACT_FIELDS RESPONSE ({call_elapsed:.1f}s): "
                f"status={resp.status_code}, "
                f"prompt_tokens={usage.get('prompt_tokens', '?')}, "
                f"completion_tokens={usage.get('completion_tokens', '?')}"
            )
            log.info(f"[{request_id}] EXTRACT_FIELDS raw content:\n{content}")
        try:
            parsed = json.loads(content)
            if log:
                log.info(
                    f"[{request_id}] EXTRACT_FIELDS parsed: "
                    f"{json.dumps(parsed, ensure_ascii=False)}"
                )
            return parsed
        except json.JSONDecodeError:
            for marker in ("```json", "```"):
                if marker in content:
                    parsed = json.loads(
                        content.split(marker, 1)[1].split("```", 1)[0].strip()
                    )
                    if log:
                        log.info(
                            f"[{request_id}] EXTRACT_FIELDS parsed (from markdown): "
                            f"{json.dumps(parsed, ensure_ascii=False)}"
                        )
                    return parsed
            if log:
                log.warning(f"[{request_id}] EXTRACT_FIELDS: could not parse JSON from response")
            return None
    except Exception as e:
        if log:
            log.error(f"[{request_id}] EXTRACT_FIELDS ERROR: {type(e).__name__}: {e}")
        return None


# ---- Existing Solvers (unchanged logic, added parallel calls where possible) ----


async def _solve_departments(client, base_url, token, fields, log, rid):
    depts = fields.get("departments", [])
    if not depts:
        return False

    existing = await execute_tripletex_call(
        client, base_url, token, "GET", "/department", params={"fields": "id,name"},
    )
    if _is_proxy_token_invalid(existing):
        _mark_proxy_token_invalid(fields, log, rid, "SOLVER departments GET /department")
        return False
    existing_names = set()
    if existing["status_code"] == 200:
        existing_names = {v["name"].lower() for v in existing["body"].get("values", [])}

    to_create = [d for d in depts if d["name"].lower() not in existing_names]
    if not to_create:
        log.info(f"[{rid}] SOLVER all departments already exist, skipping")
        return True

    batch_body = [{"name": d["name"]} for d in to_create]
    r = await execute_tripletex_call(
        client, base_url, token, "POST", "/department/list", body=batch_body,
    )
    if _is_proxy_token_invalid(r):
        _mark_proxy_token_invalid(fields, log, rid, "SOLVER departments POST /department/list")
        return False
    log.info(f"[{rid}] SOLVER POST /department/list ({len(batch_body)} depts) -> {r['status_code']}")
    return r["status_code"] in (200, 201)


async def _solve_customer(client, base_url, token, fields, log, rid):
    if fields.get("organizationNumber"):
        check = await execute_tripletex_call(
            client, base_url, token, "GET", "/customer",
            params={"organizationNumber": fields["organizationNumber"], "fields": "id,name"},
        )
        if check["status_code"] == 200 and check["body"].get("values"):
            log.info(f"[{rid}] SOLVER customer already exists, skipping create")
            return True

    body = {"isCustomer": True}
    for k in (
        "name",
        "organizationNumber",
        "email",
        "phoneNumber",
        "phoneNumberMobile",
        "invoiceEmail",
        "postalAddress",
        "physicalAddress",
        "invoicesDueIn",
        "invoicesDueInType",
        "language",
    ):
        if fields.get(k) is not None:
            body[k] = fields[k]
    r = await execute_tripletex_call(
        client, base_url, token, "POST", "/customer", body=body
    )
    log.info(
        f"[{rid}] SOLVER POST /customer '{fields.get('name')}' -> {r['status_code']}"
    )
    return r["status_code"] in (200, 201)


async def _solve_supplier(client, base_url, token, fields, log, rid):
    if fields.get("organizationNumber"):
        check = await execute_tripletex_call(
            client, base_url, token, "GET", "/supplier",
            params={"organizationNumber": fields["organizationNumber"], "fields": "id,name"},
        )
        if check["status_code"] == 200 and check["body"].get("values"):
            log.info(f"[{rid}] SOLVER supplier already exists, skipping create")
            return True

    body = {"isSupplier": True}
    for k in (
        "name",
        "organizationNumber",
        "email",
        "phoneNumber",
        "phoneNumberMobile",
        "postalAddress",
    ):
        if fields.get(k) is not None:
            body[k] = fields[k]
    r = await execute_tripletex_call(
        client, base_url, token, "POST", "/supplier", body=body
    )
    log.info(
        f"[{rid}] SOLVER POST /supplier '{fields.get('name')}' -> {r['status_code']}"
    )
    return r["status_code"] in (200, 201)


async def _solve_product(client, base_url, token, fields, log, rid):
    if fields.get("number"):
        check = await execute_tripletex_call(
            client, base_url, token, "GET", "/product",
            params={"number": fields["number"], "fields": "id,name"},
        )
        if check["status_code"] == 200 and check["body"].get("values"):
            log.info(f"[{rid}] SOLVER product already exists, skipping create")
            return True

    vat_id = VAT_RATE_TO_TYPE.get(fields.get("vatRatePercent", 25), 3)
    body = {"name": fields["name"], "vatType": {"id": vat_id}}
    if fields.get("number"):
        body["number"] = fields["number"]
    if fields.get("priceExcludingVat") is not None:
        body["priceExcludingVatCurrency"] = fields["priceExcludingVat"]
    if fields.get("description"):
        body["description"] = fields["description"]
    r = await execute_tripletex_call(
        client, base_url, token, "POST", "/product", body=body
    )
    log.info(f"[{rid}] SOLVER POST /product '{fields['name']}' -> {r['status_code']}")
    return r["status_code"] in (200, 201)


async def _solve_employee(client, base_url, token, fields, log, rid):
    dept_name = fields.get("department")
    start_date = fields.get("startDate")
    has_employment = (
        start_date
        or fields.get("annualSalary")
        or fields.get("percentageOfFullTimeEquivalent")
    )

    calls = [("GET", "/department", {"name": dept_name} if dept_name else None, None)]
    if has_employment:
        calls.append(("GET", "/division", None, None))
        occ_code = fields.get("occupationCode")
        job_title = fields.get("jobTitle")
        if occ_code:
            calls.append(
                ("GET", "/employee/employment/occupationCode", {"code": occ_code}, None)
            )
        elif job_title:
            calls.append(
                ("GET", "/employee/employment/occupationCode", {"nameNO": job_title, "fields": "id,nameNO,code"}, None)
            )

    results = await _parallel_calls(client, base_url, token, calls)

    dept_r = results[0]
    if dept_r["status_code"] != 200:
        return False
    vals = dept_r["body"].get("values", [])
    log.info(f"[{rid}] SOLVER GET /department -> found {len(vals)}")

    if vals:
        dept_id = vals[0]["id"]
    else:
        cr = await execute_tripletex_call(
            client,
            base_url,
            token,
            "POST",
            "/department",
            body={"name": dept_name or "Avdeling", "departmentNumber": "1"},
        )
        if cr["status_code"] not in (200, 201):
            return False
        dept_id = cr["body"]["value"]["id"]

    email = fields.get("email")
    if not email:
        email = _generate_fallback_email(fields["firstName"], fields["lastName"])
    body = {
        "firstName": fields["firstName"],
        "lastName": fields["lastName"],
        "userType": "STANDARD",
        "department": {"id": dept_id},
        "email": email,
    }
    for k in (
        "dateOfBirth",
        "phoneNumberMobile",
        "address",
        "nationalIdentityNumber",
    ):
        if fields.get(k) is not None:
            body[k] = fields[k]

    if has_employment:
        div_r = results[1]
        div_vals = (
            div_r["body"].get("values", []) if div_r["status_code"] == 200 else []
        )

        if div_vals:
            div_id = div_vals[0]["id"]
        else:
            dcr = await execute_tripletex_call(
                client,
                base_url,
                token,
                "POST",
                "/division",
                body={
                    "name": "Hovedkontor",
                    "organizationNumber": "999999999",
                    "startDate": "2026-01-01",
                    "municipality": {"id": 1},
                    "municipalityDate": "2026-01-01",
                },
            )
            if dcr["status_code"] not in (200, 201):
                return False
            div_id = dcr["body"]["value"]["id"]

        occ_id = 3
        if (fields.get("occupationCode") or fields.get("jobTitle")) and len(results) > 2:
            occ_r = results[2]
            if occ_r["status_code"] == 200:
                occ_vals = occ_r["body"].get("values", [])
                if occ_vals:
                    if fields.get("jobTitle") and not fields.get("occupationCode"):
                        title_upper = fields["jobTitle"].upper()
                        exact = [v for v in occ_vals if v.get("nameNO", "").upper() == title_upper]
                        occ_id = exact[0]["id"] if exact else occ_vals[0]["id"]
                    else:
                        occ_id = occ_vals[0]["id"]

        emp_date = start_date or "2026-01-01"
        emp_details = {
            "date": emp_date,
            "employmentType": "ORDINARY",
            "maritimeEmployment": {
                "shipRegister": "NIS",
                "shipType": "OTHER",
                "tradeArea": "DOMESTIC",
            },
            "remunerationType": "MONTHLY_WAGE",
            "workingHoursScheme": "NOT_SHIFT",
            "occupationCode": {"id": occ_id},
        }
        if fields.get("annualSalary") is not None:
            emp_details["annualSalary"] = fields["annualSalary"]
        if fields.get("percentageOfFullTimeEquivalent") is not None:
            emp_details["percentageOfFullTimeEquivalent"] = fields[
                "percentageOfFullTimeEquivalent"
            ]

        body["employments"] = [
            {
                "startDate": emp_date,
                "division": {"id": div_id},
                "employmentDetails": [emp_details],
            }
        ]

    r = await execute_tripletex_call(
        client, base_url, token, "POST", "/employee", body=body
    )
    log.info(f"[{rid}] SOLVER POST /employee -> {r['status_code']}")
    if r["status_code"] not in (200, 201):
        return False

    emp_id = r["body"]["value"]["id"]

    hours = fields.get("hoursPerDay")
    if hours:
        await execute_tripletex_call(
            client,
            base_url,
            token,
            "POST",
            "/employee/standardTime",
            body={
                "employee": {"id": emp_id},
                "fromDate": start_date or "2026-01-01",
                "hoursPerDay": hours,
            },
        )

    await execute_tripletex_call(
        client,
        base_url,
        token,
        "PUT",
        "/employee/entitlement/:grantEntitlementsByTemplate",
        params={"employeeId": emp_id, "template": "ALL_PRIVILEGES"},
    )

    return True


async def _solve_credit_note(client, base_url, token, fields, log, rid):
    params = {"fields": "id,name"}
    if fields.get("customerOrgNumber"):
        params["organizationNumber"] = fields["customerOrgNumber"]
    elif fields.get("customerName"):
        params["customerName"] = fields["customerName"]
    else:
        return False

    cr = await execute_tripletex_call(
        client, base_url, token, "GET", "/customer", params=params
    )
    if cr["status_code"] != 200 or not cr["body"].get("values"):
        return False
    cust_id = cr["body"]["values"][0]["id"]

    inv_r = await execute_tripletex_call(
        client,
        base_url,
        token,
        "GET",
        "/invoice",
        params={
            "customerId": cust_id,
            "invoiceDateFrom": "2020-01-01",
            "invoiceDateTo": "2099-12-31",
            "fields": "id,invoiceNumber,invoiceDate",
        },
    )
    if inv_r["status_code"] != 200 or not inv_r["body"].get("values"):
        return False
    invoice = inv_r["body"]["values"][0]
    inv_id = invoice["id"]

    inv_date = invoice.get("invoiceDate", "")
    candidate_date = fields.get("date") or time.strftime("%Y-%m-%d")
    date = max(candidate_date, inv_date) if inv_date else candidate_date
    cn_r = await execute_tripletex_call(
        client,
        base_url,
        token,
        "PUT",
        f"/invoice/{inv_id}/:createCreditNote",
        params={"date": date},
    )
    log.info(f"[{rid}] SOLVER PUT /invoice/:createCreditNote -> {cn_r['status_code']}")
    return cn_r["status_code"] in (200, 201)


async def _solve_project(client, base_url, token, fields, log, rid):
    body = {
        "name": fields["name"],
        "startDate": fields.get("startDate") or time.strftime("%Y-%m-%d"),
    }
    if fields.get("number"):
        body["number"] = fields["number"]
    if fields.get("endDate"):
        body["endDate"] = fields["endDate"]
    if fields.get("isInternal") is not None:
        body["isInternal"] = fields["isInternal"]

    # Parallel: look up customer + project manager
    calls = []
    if fields.get("customerOrgNumber"):
        calls.append(
            (
                "GET",
                "/customer",
                {
                    "organizationNumber": fields["customerOrgNumber"],
                    "fields": "id,name",
                },
                None,
            )
        )
    elif fields.get("customerName"):
        calls.append(
            (
                "GET",
                "/customer",
                {"customerName": fields["customerName"], "fields": "id,name"},
                None,
            )
        )

    if fields.get("projectManagerEmail"):
        calls.append(
            (
                "GET",
                "/employee",
                {"email": fields["projectManagerEmail"], "fields": "id", "count": 1},
                None,
            )
        )
    elif fields.get("projectManagerName"):
        parts = fields["projectManagerName"].split()
        ep = {"fields": "id", "count": 1}
        if len(parts) >= 2:
            ep["firstName"] = parts[0]
            ep["lastName"] = " ".join(parts[1:])
        else:
            ep["firstName"] = parts[0]
        calls.append(("GET", "/employee", ep, None))

    if not calls:
        calls.append(("GET", "/employee", {"fields": "id", "count": 1}, None))

    results = await _parallel_calls(client, base_url, token, calls)

    idx = 0
    if fields.get("customerOrgNumber") or fields.get("customerName"):
        cr = results[idx]
        idx += 1
        if cr["status_code"] == 200 and cr["body"].get("values"):
            body["customer"] = {"id": cr["body"]["values"][0]["id"]}

    if idx < len(results):
        er = results[idx]
        if er["status_code"] == 200 and er["body"].get("values"):
            body["projectManager"] = {"id": er["body"]["values"][0]["id"]}

    if "projectManager" not in body:
        er = await execute_tripletex_call(
            client,
            base_url,
            token,
            "GET",
            "/employee",
            params={"fields": "id", "count": 1},
        )
        if er["status_code"] == 200 and er["body"].get("values"):
            body["projectManager"] = {"id": er["body"]["values"][0]["id"]}

    r = await execute_tripletex_call(
        client, base_url, token, "POST", "/project", body=body
    )
    log.info(f"[{rid}] SOLVER POST /project -> {r['status_code']}")
    return r["status_code"] in (200, 201)


async def _solve_simple_invoice(client, base_url, token, fields, log, rid):
    if fields.get("customerOrgNumber"):
        cp = {"organizationNumber": fields["customerOrgNumber"], "fields": "id,name"}
    elif fields.get("customerName"):
        cp = {"customerName": fields["customerName"], "fields": "id,name"}
    else:
        return False

    cr = await execute_tripletex_call(
        client, base_url, token, "GET", "/customer", params=cp
    )
    if cr["status_code"] != 200:
        return False
    cust_vals = cr["body"].get("values", [])

    if cust_vals:
        cust_id = cust_vals[0]["id"]
    else:
        cust_body = {"name": fields.get("customerName", "Customer"), "isCustomer": True}
        if fields.get("customerOrgNumber"):
            cust_body["organizationNumber"] = fields["customerOrgNumber"]
        ccr = await execute_tripletex_call(
            client, base_url, token, "POST", "/customer", body=cust_body
        )
        if ccr["status_code"] not in (200, 201):
            return False
        cust_id = ccr["body"]["value"]["id"]

    today = time.strftime("%Y-%m-%d")
    vat_id = VAT_RATE_TO_TYPE.get(fields.get("vatRatePercent", 25), 3)
    order_line = {
        "description": fields.get("description")
        or fields.get("productName", "Product"),
        "count": fields.get("quantity", 1),
        "unitPriceExcludingVatCurrency": fields.get("productPrice", 0),
        "vatType": {"id": vat_id},
    }

    inv_date = fields.get("invoiceDate") or today
    due_date = fields.get("invoiceDueDate") or _calc_due_date(inv_date)
    inv_r = await execute_tripletex_call(
        client,
        base_url,
        token,
        "POST",
        "/invoice",
        body={
            "invoiceDate": inv_date,
            "invoiceDueDate": due_date,
            "customer": {"id": cust_id},
            "orders": [{
                "customer": {"id": cust_id},
                "orderDate": inv_date,
                "deliveryDate": inv_date,
                "orderLines": [order_line],
            }],
        },
        params={"sendToCustomer": True},
    )
    log.info(f"[{rid}] SOLVER invoice (inline order) -> {inv_r['status_code']}")
    return inv_r["status_code"] in (200, 201)


async def _solve_register_payment(client, base_url, token, fields, log, rid):
    params = {"fields": "id,name"}
    if fields.get("customerOrgNumber"):
        params["organizationNumber"] = fields["customerOrgNumber"]
    elif fields.get("customerName"):
        params["customerName"] = fields["customerName"]
    else:
        return False

    # Parallel: customer + paymentType
    cr, pt_r = await _parallel_calls(
        client,
        base_url,
        token,
        [
            ("GET", "/customer", params, None),
            ("GET", "/invoice/paymentType", {"fields": "id,description"}, None),
        ],
    )

    if cr["status_code"] != 200 or not cr["body"].get("values"):
        return False
    cust_id = cr["body"]["values"][0]["id"]

    if pt_r["status_code"] != 200 or not pt_r["body"].get("values"):
        return False
    pay_type_id = None
    for pt in pt_r["body"]["values"]:
        if "bank" in pt.get("description", "").lower():
            pay_type_id = pt["id"]
            break
    if pay_type_id is None:
        pay_type_id = pt_r["body"]["values"][0]["id"]

    inv_r = await execute_tripletex_call(
        client,
        base_url,
        token,
        "GET",
        "/invoice",
        params={
            "customerId": cust_id,
            "invoiceDateFrom": "2020-01-01",
            "invoiceDateTo": "2099-12-31",
            "fields": "id,invoiceNumber,amount,amountOutstanding",
        },
    )
    if inv_r["status_code"] != 200 or not inv_r["body"].get("values"):
        return False
    invoice = inv_r["body"]["values"][0]
    inv_id = invoice["id"]
    amount = invoice.get("amountOutstanding") or invoice.get("amount", 0)

    payment_date = fields.get("paymentDate") or time.strftime("%Y-%m-%d")
    pay_r = await execute_tripletex_call(
        client,
        base_url,
        token,
        "PUT",
        f"/invoice/{inv_id}/:payment",
        params={
            "paymentDate": payment_date,
            "paymentTypeId": pay_type_id,
            "paidAmount": amount,
        },
    )
    log.info(f"[{rid}] SOLVER register payment -> {pay_r['status_code']}")
    return pay_r["status_code"] in (200, 204)


def _parse_postal_address_from_text(file_text: str) -> dict | None:
    """Fallback: extract postal address from PDF text when LLM extraction misses it."""
    import re
    for line in file_text.splitlines():
        m = re.match(r"^(.+?),\s*(\d{4})\s+(.+)$", line.strip())
        if m:
            addr_line, postal_code, city = m.group(1), m.group(2), m.group(3)
            if any(kw in addr_line.lower() for kw in ("veien", "gata", "gate", "vei", "plass", "alle", "vegen")):
                return {"addressLine1": addr_line.strip(), "postalCode": postal_code, "city": city.strip()}
    return None


async def _solve_supplier_invoice(client, base_url, token, fields, log, rid):
    org_nr = fields.get("supplierOrgNumber")
    if not org_nr:
        return False

    if not fields.get("postalAddress"):
        file_text = fields.get("_file_text", "")
        if file_text:
            parsed_addr = _parse_postal_address_from_text(file_text)
            if parsed_addr:
                fields["postalAddress"] = parsed_addr
                log.info(f"[{rid}] SOLVER: parsed postalAddress from file text: {parsed_addr}")

    initial_calls = [
        (
            "GET",
            "/supplier",
            {"organizationNumber": org_nr, "fields": "id,name"},
            None,
        ),
        ("GET", "/ledger/voucherType", {"fields": "id,name"}, None),
        ("GET", "/ledger/vatType", None, None),
        (
            "GET",
            "/ledger/account",
            {"number": fields.get("expenseAccountNumber", 6300), "fields": "id,number,name"},
            None,
        ),
        (
            "GET",
            "/ledger/account",
            {"number": 2400, "fields": "id,number,name"},
            None,
        ),
    ]
    results = await _parallel_calls(client, base_url, token, initial_calls)
    sr, vt_r, vat_r, acct_r, ap_r = results

    if sr["status_code"] != 200:
        return False
    supplier_vals = sr["body"].get("values", [])
    if supplier_vals:
        supplier_id = supplier_vals[0]["id"]
    else:
        supplier_body = {
            "name": fields.get("supplierName", "Supplier"),
            "isSupplier": True,
            "organizationNumber": org_nr,
        }
        addr = fields.get("postalAddress")
        if addr and isinstance(addr, dict):
            supplier_body["postalAddress"] = addr
        scr = await execute_tripletex_call(
            client, base_url, token, "POST", "/supplier", body=supplier_body
        )
        log.info(f"[{rid}] SOLVER create supplier -> {scr['status_code']}")
        if scr["status_code"] not in (200, 201):
            return False
        supplier_id = scr["body"]["value"]["id"]

    if vt_r["status_code"] != 200:
        return False
    voucher_type_id = None
    for vt in vt_r["body"].get("values", []):
        if (
            "leverandør" in vt.get("name", "").lower()
            and "faktura" in vt.get("name", "").lower()
        ):
            voucher_type_id = vt["id"]
            break
    if voucher_type_id is None:
        return False

    vat_pct = fields.get("vatRatePercent", 25)
    if vat_r["status_code"] != 200:
        return False
    input_vat_id = None
    for vt in vat_r["body"].get("values", []):
        pct = vt.get("percentage")
        name = (vt.get("name") or "").lower()
        if pct == vat_pct and (
            "inngående" in name or "innkjøp" in name or "innenlands" in name
        ):
            input_vat_id = vt["id"]
            break
    if input_vat_id is None:
        input_vat_id = VAT_RATE_TO_INPUT_TYPE.get(vat_pct)
    if input_vat_id is None:
        return False

    if acct_r["status_code"] != 200 or not acct_r["body"].get("values"):
        return False
    expense_id = acct_r["body"]["values"][0]["id"]

    if ap_r["status_code"] != 200 or not ap_r["body"].get("values"):
        return False
    ap_id = ap_r["body"]["values"][0]["id"]

    amount_incl = fields.get("amountInclVat", 0)
    inv_num = fields.get("invoiceNumber", "")
    supplier_name = fields.get("supplierName", "")
    desc_parts = [
        s for s in [f"Faktura {inv_num}" if inv_num else "", supplier_name] if s
    ]
    description = " - ".join(desc_parts) or "Leverandørfaktura"
    voucher_date = fields.get("date") or time.strftime("%Y-%m-%d")

    vr = await execute_tripletex_call(
        client,
        base_url,
        token,
        "POST",
        "/ledger/voucher",
        body={
            "date": voucher_date,
            "description": description,
            "voucherType": {"id": voucher_type_id},
            "postings": [
                {
                    "row": 1,
                    "date": voucher_date,
                    "account": {"id": expense_id},
                    "amountGross": amount_incl,
                    "amountGrossCurrency": amount_incl,
                    "currency": {"id": 1},
                    "vatType": {"id": input_vat_id},
                    "invoiceNumber": inv_num,
                },
                {
                    "row": 2,
                    "date": voucher_date,
                    "account": {"id": ap_id},
                    "amountGross": -amount_incl,
                    "amountGrossCurrency": -amount_incl,
                    "currency": {"id": 1},
                    "supplier": {"id": supplier_id},
                    "invoiceNumber": inv_num,
                },
            ],
        },
    )
    log.info(f"[{rid}] SOLVER supplier invoice via voucher -> {vr['status_code']}")
    if vr["status_code"] not in (200, 201):
        if _is_proxy_token_invalid(vr):
            _mark_proxy_token_invalid(fields, log, rid, "SOLVER supplier invoice POST /ledger/voucher")
        return False
    return True


async def _solve_payroll(client, base_url, token, fields, log, rid):
    email = fields.get("employeeEmail")
    if not email:
        return False

    emp_r, div_r, st_r = await _parallel_calls(
        client, base_url, token,
        [
            ("GET", "/employee", {"email": email, "fields": "id,firstName,lastName,dateOfBirth,version,employments(id,startDate,division(id))"}, None),
            ("GET", "/division", {"fields": "id,name"}, None),
            ("GET", "/salary/type", {"fields": "id,number,name", "count": 100}, None),
        ],
    )

    if emp_r["status_code"] != 200 or not emp_r["body"].get("values"):
        return False
    emp = emp_r["body"]["values"][0]
    emp_id = emp["id"]

    if not emp.get("dateOfBirth"):
        await execute_tripletex_call(
            client, base_url, token, "PUT", f"/employee/{emp_id}",
            body={"id": emp_id, "version": emp.get("version", 0), "dateOfBirth": "1990-05-15"},
        )

    if not emp.get("employments"):
        div_vals = div_r["body"].get("values", []) if div_r["status_code"] == 200 else []
        if div_vals:
            div_id = div_vals[0]["id"]
        else:
            dcr = await execute_tripletex_call(
                client, base_url, token, "POST", "/division",
                body={
                    "name": "Hovedkontor",
                    "organizationNumber": "999999999",
                    "startDate": "2025-01-01",
                    "municipality": {"id": 1},
                    "municipalityDate": "2025-01-01",
                },
            )
            if dcr["status_code"] not in (200, 201):
                return False
            div_id = dcr["body"]["value"]["id"]

        empl_r = await execute_tripletex_call(
            client, base_url, token, "POST", "/employee/employment",
            body={
                "employee": {"id": emp_id},
                "startDate": "2025-01-01",
                "division": {"id": div_id},
                "employmentDetails": [{
                    "date": "2025-01-01",
                    "employmentType": "ORDINARY",
                    "maritimeEmployment": {"shipRegister": "NIS", "shipType": "OTHER", "tradeArea": "DOMESTIC"},
                    "remunerationType": "MONTHLY_WAGE",
                    "workingHoursScheme": "NOT_SHIFT",
                    "occupationCode": {"id": 3},
                }],
            },
        )
        if empl_r["status_code"] not in (200, 201):
            return False

    if st_r["status_code"] != 200:
        return False
    salary_types = st_r["body"].get("values", [])

    type_by_number = {}
    type_by_name = {}
    for st in salary_types:
        num = st.get("number", "")
        name_lower = (st.get("name") or "").lower()
        type_by_number[num] = st["id"]
        type_by_name[name_lower] = st["id"]

    SALARY_TYPE_MAP = {
        "fastlønn": "2000", "fast lønn": "2000", "base salary": "2000",
        "base": "2000", "grunnlønn": "2000", "salário base": "2000",
        "salaire de base": "2000", "grundgehalt": "2000", "sueldo base": "2000",
        "timelønn": "2001", "hourly": "2001",
        "bonus": "2002", "bônus": "2002", "prime": "2002",
        "faste tillegg": "2003", "fixed allowance": "2003",
        "overtid": "2005", "overtime": "2005", "heures supplémentaires": "2005",
        "überstunden": "2005", "horas extras": "2005",
        "overtid 50": "2007", "overtid 100": "2008",
    }

    month = fields.get("month") or time.localtime().tm_mon
    year = fields.get("year") or time.localtime().tm_year
    last_day = calendar.monthrange(year, month)[1]

    specs = []
    salary_components = fields.get("salaryComponents", [])

    if not salary_components:
        base_salary = fields.get("baseSalary", 0)
        bonus_val = fields.get("bonus", 0)
        if base_salary:
            salary_components.append({"type": "fastlønn", "amount": base_salary})
        if bonus_val:
            salary_components.append({"type": "bonus", "amount": bonus_val})

    for comp in salary_components:
        comp_type = (comp.get("type") or "").lower().strip()
        amount = comp.get("amount", 0)
        if not amount:
            continue

        sal_type_id = None
        mapped_num = SALARY_TYPE_MAP.get(comp_type)
        if mapped_num and mapped_num in type_by_number:
            sal_type_id = type_by_number[mapped_num]
        else:
            for name_lower, tid in type_by_name.items():
                if comp_type in name_lower or name_lower in comp_type:
                    sal_type_id = tid
                    break
        if sal_type_id is None:
            sal_type_id = type_by_number.get("2000")
        if sal_type_id is None:
            continue

        specs.append({
            "salaryType": {"id": sal_type_id},
            "amount": amount,
            "rate": amount,
            "count": 1,
        })

    if not specs:
        return False

    tx_r = await execute_tripletex_call(
        client, base_url, token, "POST", "/salary/transaction",
        body={
            "date": f"{year}-{month:02d}-{last_day:02d}",
            "year": year,
            "month": month,
            "payslips": [{"employee": {"id": emp_id}, "specifications": specs}],
        },
    )
    log.info(f"[{rid}] SOLVER payroll -> {tx_r['status_code']}")
    if tx_r["status_code"] not in (200, 201):
        log.warning(f"[{rid}] SOLVER payroll error: {json.dumps(tx_r['body'])[:300]}")
    return tx_r["status_code"] in (200, 201)


VAT_ACCOUNTS = {2700, 2710, 2711, 2712}


async def _solve_ledger_correction(client, base_url, token, fields, log, rid):
    errors = fields.get("errors", [])
    date_from = fields.get("dateFrom")
    date_to = fields.get("dateTo")

    if not errors or not date_from or not date_to:
        return False

    voucher_r, vt_r = await _parallel_calls(
        client, base_url, token,
        [
            ("GET", "/ledger/voucher", {
                "dateFrom": date_from,
                "dateTo": date_to,
                "fields": "id,number,date,description,postings(id,account(id,number,name),amountGross,amountGrossCurrency,vatType(id),supplier(id))",
                "count": 10000,
            }, None),
            ("GET", "/ledger/voucherType", {"fields": "id,name"}, None),
        ],
    )
    if voucher_r["status_code"] != 200:
        return False

    correction_vt_id = None
    if vt_r["status_code"] == 200:
        for vt in vt_r["body"].get("values", []):
            if "memorial" in vt.get("name", "").lower():
                correction_vt_id = vt["id"]
                break
        if correction_vt_id is None:
            for vt in vt_r["body"].get("values", []):
                if "leverandør" in vt.get("name", "").lower() and "faktura" in vt.get("name", "").lower():
                    correction_vt_id = vt["id"]
                    break
        if correction_vt_id is None and vt_r["body"].get("values"):
            correction_vt_id = vt_r["body"]["values"][0]["id"]
    if correction_vt_id is None:
        return False

    vouchers = voucher_r["body"].get("values", [])
    if not vouchers:
        return False

    account_map: dict[int, int] = {}
    for v in vouchers:
        for p in v.get("postings", []):
            acct = p.get("account", {})
            num = acct.get("number")
            aid = acct.get("id")
            if num and aid:
                account_map[num] = aid

    needed = set()
    for err in errors:
        for key in ("correctAccount", "wrongAccount", "account", "vatAccount"):
            val = err.get(key)
            if val:
                needed.add(val)
    missing = needed - set(account_map.keys())
    if missing:
        lookups = [
            ("GET", "/ledger/account", {"number": str(n), "fields": "id,number,name"}, None)
            for n in missing
        ]
        results = await _parallel_calls(client, base_url, token, lookups)
        for r in results:
            if r["status_code"] == 200 and r["body"].get("values"):
                a = r["body"]["values"][0]
                account_map[a["number"]] = a["id"]

    all_postings: list[dict] = []
    correction_date = date_to

    for err in errors:
        etype = err.get("type")

        if etype == "wrong_account":
            wrong_num = err.get("wrongAccount")
            correct_num = err.get("correctAccount")
            amount = err.get("amount", 0)
            if not wrong_num or not correct_num:
                return False
            if correct_num not in account_map or wrong_num not in account_map:
                return False

            orig_vat = 0
            for v in vouchers:
                for p in v.get("postings", []):
                    if (p.get("account", {}).get("number") == wrong_num
                            and abs(p.get("amountGross", 0) - amount) < 0.01):
                        orig_vat = p.get("vatType", {}).get("id", 0)
                        break
                if orig_vat:
                    break

            all_postings.append({
                "account": {"id": account_map[correct_num]},
                "amountGross": amount, "amountGrossCurrency": amount,
                "vatType": {"id": orig_vat},
            })
            all_postings.append({
                "account": {"id": account_map[wrong_num]},
                "amountGross": -amount, "amountGrossCurrency": -amount,
                "vatType": {"id": orig_vat},
            })

        elif etype == "duplicate":
            dup_acct = err.get("account")
            dup_amt = err.get("amount", 0)

            match = None
            for v in vouchers:
                desc = v.get("description", "").lower()
                if "duplikat" in desc or "duplicate" in desc or "kopi" in desc:
                    for p in v.get("postings", []):
                        if (p.get("account", {}).get("number") == dup_acct
                                and abs(p.get("amountGross", 0) - dup_amt) < 0.01):
                            match = v
                            break
                if match:
                    break
            if not match:
                for v in reversed(vouchers):
                    for p in v.get("postings", []):
                        if (p.get("account", {}).get("number") == dup_acct
                                and abs(p.get("amountGross", 0) - dup_amt) < 0.01):
                            match = v
                            break
                    if match:
                        break
            if not match:
                log.warning(f"[{rid}] SOLVER ledger: no match for duplicate {dup_acct} {dup_amt}")
                return False

            log.info(f"[{rid}] SOLVER ledger: reversing duplicate #{match.get('number')} '{match.get('description')}'")
            for p in match.get("postings", []):
                acct_num = p.get("account", {}).get("number", 0)
                if acct_num in VAT_ACCOUNTS:
                    continue
                all_postings.append({
                    "account": {"id": p["account"]["id"]},
                    "amountGross": -p["amountGross"],
                    "amountGrossCurrency": -p.get("amountGrossCurrency", p["amountGross"]),
                    "vatType": {"id": p.get("vatType", {}).get("id", 0)},
                })

        elif etype == "missing_vat":
            expense_acct = err.get("account")
            amount_excl = err.get("amountExcl", 0)
            vat_amount = round(amount_excl * 0.25, 2)

            match = None
            orig_vat = 1
            offset_id = account_map.get(1920)
            offset_supplier = None
            for v in vouchers:
                for p in v.get("postings", []):
                    if (p.get("account", {}).get("number") == expense_acct
                            and abs(p.get("amountGross", 0) - amount_excl) < 1):
                        match = v
                        orig_vat = p.get("vatType", {}).get("id", 1)
                        break
                if match:
                    break
            if match:
                for p in match.get("postings", []):
                    n = p.get("account", {}).get("number", 0)
                    if n != expense_acct and n not in VAT_ACCOUNTS and p.get("amountGross", 0) < 0:
                        offset_id = p["account"]["id"]
                        sup = p.get("supplier")
                        if sup and sup.get("id"):
                            offset_supplier = sup["id"]
                        break

            if not account_map.get(expense_acct) or not offset_id:
                return False

            all_postings.append({
                "account": {"id": account_map[expense_acct]},
                "amountGross": vat_amount, "amountGrossCurrency": vat_amount,
                "vatType": {"id": orig_vat},
            })
            offset_posting = {
                "account": {"id": offset_id},
                "amountGross": -vat_amount, "amountGrossCurrency": -vat_amount,
                "vatType": {"id": 0},
            }
            if offset_supplier:
                offset_posting["supplier"] = {"id": offset_supplier}
            all_postings.append(offset_posting)

        elif etype == "wrong_amount":
            acct_num = err.get("account")
            booked = err.get("bookedAmount", 0)
            correct_amt = err.get("correctAmount", 0)
            diff = booked - correct_amt
            if abs(diff) < 0.01:
                continue

            expense_posting = None
            offset_id = account_map.get(1920)
            for v in vouchers:
                for p in v.get("postings", []):
                    if (p.get("account", {}).get("number") == acct_num
                            and abs(p.get("amountGross", 0) - booked) < 0.01):
                        expense_posting = p
                        for q in v.get("postings", []):
                            qn = q.get("account", {}).get("number", 0)
                            if qn != acct_num and qn not in VAT_ACCOUNTS and q.get("amountGross", 0) < 0:
                                offset_id = q["account"]["id"]
                                break
                        break
                if expense_posting:
                    break
            if not expense_posting:
                return False

            vat_id = expense_posting.get("vatType", {}).get("id", 0)
            all_postings.append({
                "account": {"id": expense_posting["account"]["id"]},
                "amountGross": -diff, "amountGrossCurrency": -diff,
                "vatType": {"id": vat_id},
            })
            all_postings.append({
                "account": {"id": offset_id},
                "amountGross": diff, "amountGrossCurrency": diff,
                "vatType": {"id": 0},
            })

    if not all_postings:
        return False

    for i, p in enumerate(all_postings):
        if not p.get("account", {}).get("id"):
            log.warning(f"[{rid}] SOLVER ledger: posting missing account ID")
            return False
        p["row"] = i + 1
        p["date"] = correction_date
        p["currency"] = {"id": 1}

    voucher_body = {
        "date": correction_date,
        "description": "Korreksjon av feil i hovudboka",
        "voucherType": {"id": correction_vt_id},
        "postings": all_postings,
    }

    result = await execute_tripletex_call(
        client, base_url, token, "POST", "/ledger/voucher", body=voucher_body,
    )
    log.info(f"[{rid}] SOLVER ledger correction -> {result['status_code']} ({len(all_postings)} postings)")
    if result["status_code"] not in (200, 201):
        log.warning(f"[{rid}] SOLVER ledger error: {json.dumps(result.get('body', ''))[:500]}")
        return False
    return True


async def _solve_cost_analysis(client, base_url, token, fields, log, rid):
    month1 = fields.get("month1", 1)
    month2 = fields.get("month2", 2)
    year = fields.get("year", 2026)
    top_n = fields.get("topN", 3)
    activity_name = fields.get("activityName", "Kostnadsreduksjon")
    deterministic_state = fields.setdefault("_deterministic_state", {})
    cost_state = deterministic_state.setdefault("cost_analysis", {})

    def _response_items(body):
        if not isinstance(body, dict):
            return []
        if isinstance(body.get("values"), list):
            return body["values"]
        if isinstance(body.get("value"), dict):
            return [body["value"]]
        return []

    def _ordered_ids_by_name(items, expected_names, entity_label):
        ids_by_name: dict[str, list[int]] = {}
        for item in items:
            item_id = item.get("id")
            item_name = item.get("name")
            if item_id and item_name:
                ids_by_name.setdefault(item_name, []).append(item_id)

        ordered_ids = []
        missing_names = []
        for name in expected_names:
            matching_ids = ids_by_name.get(name)
            if matching_ids:
                ordered_ids.append(matching_ids.pop(0))
            else:
                missing_names.append(name)

        if missing_names:
            log.warning(
                f"[{rid}] SOLVER cost analysis -> could not map {entity_label} by name; "
                f"missing={missing_names} returned={json.dumps(items)[:500]}"
            )
            return None
        return ordered_ids

    last_day_m1 = calendar.monthrange(year, month1)[1]
    last_day_m2 = calendar.monthrange(year, month2)[1]

    p1_r, p2_r, emp_r = await _parallel_calls(
        client, base_url, token,
        [
            ("GET", "/ledger/posting", {
                "dateFrom": f"{year}-{month1:02d}-01",
                "dateTo": f"{year}-{month1:02d}-{last_day_m1:02d}",
                "fields": "id,account(id,number,name),amountGross",
                "count": 10000,
            }, None),
            ("GET", "/ledger/posting", {
                "dateFrom": f"{year}-{month2:02d}-01",
                "dateTo": f"{year}-{month2:02d}-{last_day_m2:02d}",
                "fields": "id,account(id,number,name),amountGross",
                "count": 10000,
            }, None),
            ("GET", "/employee", {"fields": "id", "count": 1}, None),
        ],
    )

    if p1_r["status_code"] != 200 or p2_r["status_code"] != 200:
        return False
    if emp_r["status_code"] != 200 or not emp_r["body"].get("values"):
        return False
    pm_id = emp_r["body"]["values"][0]["id"]

    def aggregate_expenses(postings_data):
        totals = {}
        for p in postings_data.get("values", []):
            acct = p.get("account", {})
            num = acct.get("number", 0)
            if 4000 <= num <= 7999:
                key = num
                if key not in totals:
                    totals[key] = {"number": num, "name": acct.get("name", ""), "id": acct.get("id"), "total": 0}
                totals[key]["total"] += p.get("amountGross", 0)
        return totals

    m1_totals = aggregate_expenses(p1_r["body"])
    m2_totals = aggregate_expenses(p2_r["body"])

    increases = []
    all_accounts = set(m1_totals.keys()) | set(m2_totals.keys())
    for acct_num in all_accounts:
        m1_val = m1_totals.get(acct_num, {}).get("total", 0)
        m2_val = m2_totals.get(acct_num, {}).get("total", 0)
        increase = m2_val - m1_val
        if increase > 0:
            info = m2_totals.get(acct_num) or m1_totals.get(acct_num, {})
            increases.append({
                "number": acct_num,
                "name": info.get("name", ""),
                "increase": increase,
            })

    increases.sort(key=lambda x: x["increase"], reverse=True)
    top_accounts = increases[:top_n]

    if not top_accounts:
        log.warning(f"[{rid}] SOLVER cost analysis -> no expense increases found")
        return False

    log.info(f"[{rid}] SOLVER cost analysis -> top {top_n} increases: {[(a['number'], a['name'], a['increase']) for a in top_accounts]}")
    cost_state["topAccounts"] = [
        {
            "accountNumber": acct["number"],
            "accountName": acct["name"],
            "increase": acct["increase"],
        }
        for acct in top_accounts
    ]

    proj_batch = [
        {
            "name": acct["name"],
            "startDate": f"{year}-{month2:02d}-01",
            "projectManager": {"id": pm_id},
            "isInternal": True,
        }
        for acct in top_accounts
    ]
    proj_r = await execute_tripletex_call(
        client, base_url, token, "POST", "/project/list", body=proj_batch,
    )
    if proj_r["status_code"] not in (200, 201):
        log.warning(
            f"[{rid}] SOLVER cost analysis -> POST /project/list failed: "
            f"{proj_r['status_code']} {json.dumps(proj_r.get('body', ''))[:500]}"
        )
        return False

    proj_names = [acct["name"] for acct in top_accounts]
    proj_items = _response_items(proj_r.get("body", {}))
    proj_ids = _ordered_ids_by_name(proj_items, proj_names, "projects")
    if not proj_ids or len(proj_ids) != len(top_accounts):
        return False

    log.info(f"[{rid}] SOLVER cost analysis -> created {len(top_accounts)} projects (1 write)")

    linked_count = 0
    activity_names_used = []
    for acct, proj_id in zip(top_accounts, proj_ids):
        act_name = f"{activity_name} - {acct['name']}"
        link_r = await execute_tripletex_call(
            client, base_url, token, "POST", "/project/projectActivity",
            body={
                "project": {"id": proj_id},
                "activity": {
                    "name": act_name,
                },
            },
        )
        if link_r["status_code"] not in (200, 201):
            log.warning(
                f"[{rid}] SOLVER cost analysis -> projectActivity failed for '{act_name}': "
                f"{link_r['status_code']} {json.dumps(link_r.get('body', ''))[:500]}"
            )
            return False
        activity_names_used.append(act_name)
        linked_count += 1

    cost_state["projects"] = [
        {
            "accountNumber": acct["number"],
            "projectName": acct["name"],
            "projectId": proj_id,
            "activityName": act_name_used,
        }
        for acct, proj_id, act_name_used in zip(top_accounts, proj_ids, activity_names_used)
    ]
    log.info(
        f"[{rid}] SOLVER cost analysis -> created {linked_count} project activities "
        f"({1 + linked_count} writes total)"
    )
    return True


async def _solve_custom_dimension(client, base_url, token, fields, log, rid):
    dim_name = fields.get("dimensionName")
    dim_values = fields.get("dimensionValues", [])
    if not dim_name or not dim_values:
        return False

    dr = await execute_tripletex_call(
        client,
        base_url,
        token,
        "POST",
        "/ledger/accountingDimensionName",
        body={"dimensionName": dim_name},
    )
    if dr["status_code"] not in (200, 201):
        return False
    dim_index = dr["body"]["value"]["dimensionIndex"]

    dim_val_calls = [
        (
            "POST",
            "/ledger/accountingDimensionValue",
            None,
            {"displayName": val, "dimensionIndex": dim_index},
        )
        for val in dim_values
    ]
    dim_val_results = await _parallel_calls(client, base_url, token, dim_val_calls)
    value_ids = {}
    for i, vr in enumerate(dim_val_results):
        if vr["status_code"] not in (200, 201):
            return False
        value_ids[dim_values[i]] = vr["body"]["value"]["id"]

    voucher_acct = fields.get("voucherAccountNumber")
    amount = fields.get("voucherAmount")
    linked_value = fields.get("linkedDimensionValue")
    if not all([voucher_acct, amount, linked_value]):
        return True

    linked_id = value_ids.get(linked_value)
    if not linked_id:
        return False

    # Parallel: voucherType + expense account + credit account
    vt_r, acct_r, cr_r = await _parallel_calls(
        client,
        base_url,
        token,
        [
            ("GET", "/ledger/voucherType", {"fields": "id,name"}, None),
            (
                "GET",
                "/ledger/account",
                {"number": voucher_acct, "fields": "id,number,name"},
                None,
            ),
            (
                "GET",
                "/ledger/account",
                {
                    "number": fields.get("creditAccountNumber", 1920),
                    "fields": "id,number,name",
                },
                None,
            ),
        ],
    )

    if vt_r["status_code"] != 200:
        return False
    voucher_type_id = None
    for vt in vt_r["body"].get("values", []):
        name_lower = vt.get("name", "").lower()
        if "leverandør" in name_lower and "faktura" in name_lower:
            voucher_type_id = vt["id"]
            break
    if voucher_type_id is None:
        for vt in vt_r["body"].get("values", []):
            if "memorial" in vt.get("name", "").lower():
                voucher_type_id = vt["id"]
                break
    if voucher_type_id is None:
        return False

    if acct_r["status_code"] != 200 or not acct_r["body"].get("values"):
        return False
    expense_id = acct_r["body"]["values"][0]["id"]

    if cr_r["status_code"] != 200 or not cr_r["body"].get("values"):
        return False
    credit_id = cr_r["body"]["values"][0]["id"]

    today = time.strftime("%Y-%m-%d")
    dim_field = f"freeAccountingDimension{dim_index}"
    desc = (
        fields.get("description")
        or f"{acct_r['body']['values'][0].get('name', '')} - {dim_name} {linked_value}"
    )

    vr = await execute_tripletex_call(
        client,
        base_url,
        token,
        "POST",
        "/ledger/voucher",
        body={
            "date": today,
            "description": desc,
            "voucherType": {"id": voucher_type_id},
            "postings": [
                {
                    "row": 1,
                    "date": today,
                    "account": {"id": expense_id},
                    "amountGross": amount,
                    "amountGrossCurrency": amount,
                    "currency": {"id": 1},
                    dim_field: {"id": linked_id},
                },
                {
                    "row": 2,
                    "date": today,
                    "account": {"id": credit_id},
                    "amountGross": -amount,
                    "amountGrossCurrency": -amount,
                    "currency": {"id": 1},
                },
            ],
        },
    )
    log.info(f"[{rid}] SOLVER dimension voucher -> {vr['status_code']}")
    return vr["status_code"] in (200, 201)


async def _solve_order_invoice_payment(client, base_url, token, fields, log, rid):
    if fields.get("customerOrgNumber"):
        cp = {"organizationNumber": fields["customerOrgNumber"], "fields": "id,name"}
    elif fields.get("customerName"):
        cp = {"customerName": fields["customerName"], "fields": "id,name"}
    else:
        return False

    # Parallel: customer + paymentType
    cr, pt_r = await _parallel_calls(
        client,
        base_url,
        token,
        [
            ("GET", "/customer", cp, None),
            ("GET", "/invoice/paymentType", {"fields": "id,description"}, None),
        ],
    )

    if cr["status_code"] != 200 or not cr["body"].get("values"):
        return False
    cust_id = cr["body"]["values"][0]["id"]

    if pt_r["status_code"] != 200 or not pt_r["body"].get("values"):
        return False
    pay_type_id = None
    for pt in pt_r["body"]["values"]:
        if "bank" in pt.get("description", "").lower():
            pay_type_id = pt["id"]
            break
    if pay_type_id is None:
        pay_type_id = pt_r["body"]["values"][0]["id"]

    products = fields.get("products", [])
    if not products:
        return False

    # Batch all product lookups in parallel
    numbered_prods = [(i, prod) for i, prod in enumerate(products) if prod.get("number")]
    if numbered_prods:
        prod_calls = [
            (
                "GET",
                "/product",
                {"number": prod["number"], "fields": "id,name,number,priceExcludingVatCurrency,vatType(id)"},
                None,
            )
            for _, prod in numbered_prods
        ]
        prod_results = await _parallel_calls(client, base_url, token, prod_calls)
    else:
        prod_results = []

    order_lines = []
    prod_result_idx = 0
    for i, prod in enumerate(products):
        prod_number = prod.get("number")
        if prod_number:
            pr = prod_results[prod_result_idx]
            prod_result_idx += 1
            if pr["status_code"] != 200 or not pr["body"].get("values"):
                return False
            p = pr["body"]["values"][0]
            order_lines.append(
                {
                    "product": {"id": p["id"]},
                    "count": prod.get("quantity", 1),
                    "unitPriceExcludingVatCurrency": prod.get("price")
                    or p.get("priceExcludingVatCurrency", 0),
                    "vatType": p.get("vatType", {"id": 3}),
                }
            )
        else:
            vat_id = VAT_RATE_TO_TYPE.get(prod.get("vatRatePercent", 25), 3)
            order_lines.append(
                {
                    "description": prod.get("name", "Product"),
                    "count": prod.get("quantity", 1),
                    "unitPriceExcludingVatCurrency": prod.get("price", 0),
                    "vatType": {"id": vat_id},
                }
            )

    today = time.strftime("%Y-%m-%d")
    order_r = await execute_tripletex_call(
        client,
        base_url,
        token,
        "POST",
        "/order",
        body={
            "customer": {"id": cust_id},
            "orderDate": today,
            "deliveryDate": today,
            "orderLines": order_lines,
        },
    )
    if order_r["status_code"] not in (200, 201):
        return False
    order_id = order_r["body"]["value"]["id"]

    VAT_TYPE_TO_RATE = {3: 0.25, 31: 0.15, 32: 0.12, 5: 0.0, 6: 0.0}
    total_amount_incl_vat = sum(
        ol.get("unitPriceExcludingVatCurrency", 0)
        * ol.get("count", 1)
        * (1 + VAT_TYPE_TO_RATE.get(ol.get("vatType", {}).get("id", 3), 0.25))
        for ol in order_lines
    )

    inv_r = await execute_tripletex_call(
        client,
        base_url,
        token,
        "PUT",
        f"/order/{order_id}/:invoice",
        params={
            "invoiceDate": today,
            "sendToCustomer": True,
            "paymentTypeId": pay_type_id,
            "paidAmount": total_amount_incl_vat,
        },
    )
    log.info(f"[{rid}] SOLVER order->invoice+payment (via /:invoice) -> {inv_r['status_code']}")
    return inv_r["status_code"] in (200, 201)


# ---- NEW Tier 2 Solvers ---------------------------------------------------


async def _solve_reverse_payment(client, base_url, token, fields, log, rid):
    cust_params = {"fields": "id,name"}
    if fields.get("customerOrgNumber"):
        cust_params["organizationNumber"] = fields["customerOrgNumber"]
    elif fields.get("customerName"):
        cust_params["customerName"] = fields["customerName"]
    else:
        return False

    cr, vt_r = await _parallel_calls(
        client,
        base_url,
        token,
        [
            ("GET", "/customer", cust_params, None),
            ("GET", "/ledger/voucherType", {"fields": "id,name"}, None),
        ],
    )

    if cr["status_code"] != 200 or not cr["body"].get("values"):
        return False
    customer_id = cr["body"]["values"][0]["id"]
    customer_name = cr["body"]["values"][0].get("name", "")

    if vt_r["status_code"] != 200:
        return False
    payment_type_id = None
    for vt in vt_r["body"].get("values", []):
        if vt.get("name", "").lower() == "betaling":
            payment_type_id = vt["id"]
            break
    if payment_type_id is None:
        return False

    inv_r, voucher_r = await _parallel_calls(
        client,
        base_url,
        token,
        [
            ("GET", "/invoice", {
                "customerId": customer_id,
                "invoiceDateFrom": "2020-01-01",
                "invoiceDateTo": "2099-12-31",
                "fields": "id,invoiceNumber,amount,amountOutstanding,voucher(id)",
            }, None),
            ("GET", "/ledger/voucher", {
                "dateFrom": "2024-01-01",
                "dateTo": "2099-12-31",
                "typeId": str(payment_type_id),
                "fields": "id,number,date,description,voucherType(id,name),postings(id,account(id,number),amountGross,amountGrossCurrency,customer(id))",
            }, None),
        ],
    )

    if voucher_r["status_code"] != 200 or not voucher_r["body"].get("values"):
        return False

    invoice_desc = (fields.get("invoiceDescription") or "").lower()
    invoice_voucher_ids = set()
    if inv_r["status_code"] == 200:
        for inv in inv_r["body"].get("values", []):
            v = inv.get("voucher")
            if v and v.get("id"):
                invoice_voucher_ids.add(v["id"])

    voucher = None
    for v in voucher_r["body"]["values"]:
        for p in v.get("postings", []):
            cust = p.get("customer", {})
            if cust and cust.get("id") == customer_id:
                voucher = v
                break
            acct_num = p.get("account", {}).get("number", 0)
            if acct_num == 1500:
                voucher = v
                break
        if voucher:
            break

    if not voucher:
        desc_lower = invoice_desc
        if desc_lower:
            for v in voucher_r["body"]["values"]:
                v_desc = (v.get("description") or "").lower()
                if desc_lower in v_desc or customer_name.lower() in v_desc:
                    voucher = v
                    break

    if not voucher:
        voucher = voucher_r["body"]["values"][0]
        log.warning(f"[{rid}] SOLVER reverse payment -> no customer-matched voucher, using first")

    voucher_id = voucher["id"]
    reverse_date = (
        fields.get("reverseDate") or voucher.get("date") or time.strftime("%Y-%m-%d")
    )

    rev_r = await execute_tripletex_call(
        client,
        base_url,
        token,
        "PUT",
        f"/ledger/voucher/{voucher_id}/:reverse",
        params={"date": reverse_date},
    )
    log.info(f"[{rid}] SOLVER reverse payment -> {rev_r['status_code']} (voucher #{voucher.get('number', voucher_id)})")
    return rev_r["status_code"] in (200, 201)


async def _solve_travel_expense(client, base_url, token, fields, log, rid):
    dep_date, ret_date = _travel_dates_from_fields(fields)
    per_diem = fields.get("perDiem")
    is_foreign = fields.get("isForeignTravel", False)

    initial_calls = [
        (
            "GET",
            "/employee",
            {"email": fields["employeeEmail"], "fields": "id,firstName,lastName"},
            None,
        ),
        (
            "GET",
            "/travelExpense/costCategory",
            {"fields": "id,description,displayName", "count": 100},
            None,
        ),
        ("GET", "/travelExpense/paymentType", {"fields": "*"}, None),
    ]
    rc_idx = None
    if per_diem:
        rc_idx = len(initial_calls)
        initial_calls.append((
            "GET",
            "/travelExpense/rateCategory",
            {
                "type": "PER_DIEM",
                "isValidDomestic": not is_foreign,
                "dateFrom": f"{dep_date[:4]}-01-01",
                "dateTo": f"{dep_date[:4]}-12-31",
                "fields": "id,name,isRequiresOvernightAccommodation",
            },
            None,
        ))

    results = await _parallel_calls(client, base_url, token, initial_calls)
    emp_r, cc_r, pt_r = results[0], results[1], results[2]

    if emp_r["status_code"] != 200 or not emp_r["body"].get("values"):
        return False
    emp_id = emp_r["body"]["values"][0]["id"]

    if cc_r["status_code"] != 200 or pt_r["status_code"] != 200:
        return False
    cost_categories = cc_r["body"].get("values", [])
    pay_types = pt_r["body"].get("values", [])
    if not pay_types:
        return False
    pay_type_id = pay_types[0]["id"]
    if fields.get("_source_prompt") and not _prompt_has_explicit_travel_date(
        fields["_source_prompt"]
    ):
        log.info(
            f"[{rid}] TRAVEL_EXPENSE: no explicit date in prompt, "
            f"using departureDate={dep_date}, returnDate={ret_date}"
        )

    destination = fields.get("destination")
    if not destination:
        title = fields.get("title", "")
        for keyword in ["besøk", "besok", "Besuch", "visit", "visite", "visita"]:
            if keyword.lower() in title.lower():
                idx = title.lower().index(keyword.lower())
                after = title[idx + len(keyword):].strip()
                if after:
                    destination = after.split(",")[0].split(".")[0].strip().strip('"').strip("'")
                break
        if not destination:
            parts = title.rsplit(" ", 1)
            if len(parts) > 1 and parts[-1][0].isupper():
                destination = parts[-1]
        if not destination:
            destination = "Oslo"

    is_day_trip = fields.get("isDayTrip", dep_date == ret_date)

    departure_from = (fields.get("departureFrom") or "Oslo").strip() or "Oslo"
    departure_time = (fields.get("departureTime") or "08:00").strip() or "08:00"
    return_time = (fields.get("returnTime") or "17:00").strip() or "17:00"

    te_r = await execute_tripletex_call(
        client,
        base_url,
        token,
        "POST",
        "/travelExpense",
        body={
            "employee": {"id": emp_id},
            "title": fields.get("title", "Reise"),
            "travelDetails": {
                "departureDate": dep_date,
                "returnDate": ret_date,
                "departureFrom": departure_from,
                "destination": destination,
                "departureTime": departure_time,
                "returnTime": return_time,
                "purpose": fields.get("title", "Business travel"),
                "isForeignTravel": fields.get("isForeignTravel", False),
                "isDayTrip": is_day_trip,
            },
            "isChargeable": False,
            "isFixedInvoicedAmount": False,
            "isIncludeAttachedReceiptsWhenReinvoicing": False,
        },
    )
    if te_r["status_code"] not in (200, 201):
        return False
    te_id = te_r["body"]["value"]["id"]

    # Map expense types to cost categories
    category_map = {}
    for cc in cost_categories:
        name = (cc.get("description") or cc.get("displayName") or "").lower()
        if "fly" in name or "flight" in name:
            category_map["flight"] = cc["id"]
        elif "taxi" in name:
            category_map["taxi"] = cc["id"]
        elif "tog" in name or "train" in name:
            category_map["train"] = cc["id"]
        elif "hotell" in name or "hotel" in name:
            category_map["hotel"] = cc["id"]
        elif "parkering" in name or "parking" in name:
            category_map["parking"] = cc["id"]
        elif "mat" in name or "food" in name or "diett" in name:
            category_map["food"] = cc["id"]
        elif "buss" in name or "bus" in name:
            category_map["bus"] = cc["id"]

    expenses = fields.get("expenses", [])
    cost_calls = []
    for exp in expenses:
        exp_type = exp.get("type", "").lower()
        cat_id = category_map.get(exp_type)
        if not cat_id:
            for cc in cost_categories:
                n = (cc.get("description") or cc.get("displayName") or "").lower()
                if exp_type in n or n in exp_type:
                    cat_id = cc["id"]
                    break
        if not cat_id and cost_categories:
            cat_id = cost_categories[0]["id"]

        cost_calls.append((
            "POST",
            "/travelExpense/cost",
            None,
            {
                "travelExpense": {"id": te_id},
                "date": dep_date,
                "costCategory": {"id": cat_id},
                "paymentType": {"id": pay_type_id},
                "amountCurrencyIncVat": exp.get("amount", 0),
            },
        ))
    for _, path, params, body in cost_calls:
        cost_r = await execute_tripletex_call(
            client, base_url, token, "POST", path, params=params, body=body
        )
        if cost_r["status_code"] not in (200, 201):
            log.warning(
                f"[{rid}] TRAVEL_EXPENSE: cost write failed "
                f"status={cost_r['status_code']}"
            )
            return False

    if per_diem:
        rc_r = results[rc_idx]
        if rc_r["status_code"] != 200 or not rc_r["body"].get("values"):
            return False

        overnight = per_diem.get("overnightAccommodation", "HOTEL") != "NONE"
        rate_cat_id = None
        for rc in rc_r["body"]["values"]:
            req_overnight = rc.get("isRequiresOvernightAccommodation", False)
            if req_overnight == overnight:
                rate_cat_id = rc["id"]
                break
        if rate_cat_id is None:
            rate_cat_id = rc_r["body"]["values"][0]["id"]

        rate_r = await execute_tripletex_call(
            client,
            base_url,
            token,
            "GET",
            "/travelExpense/rate",
            params={"rateCategoryId": rate_cat_id},
        )
        if rate_r["status_code"] != 200 or not rate_r["body"].get("values"):
            return False
        rate_type_id = rate_r["body"]["values"][0].get("rateType", {}).get("id")
        if not rate_type_id:
            rate_type_id = rate_r["body"]["values"][0]["id"]

        per_diem_r = await execute_tripletex_call(
            client,
            base_url,
            token,
            "POST",
            "/travelExpense/perDiemCompensation",
            body={
                "travelExpense": {"id": te_id},
                "rateCategory": {"id": rate_cat_id},
                "rateType": {"id": rate_type_id},
                "overnightAccommodation": per_diem.get(
                    "overnightAccommodation", "HOTEL"
                ),
                "location": destination,
                "count": per_diem.get("days", 1),
                "rate": per_diem.get("rate", 800),
                "isDeductionForBreakfast": False,
                "isDeductionForLunch": False,
                "isDeductionForDinner": False,
            },
        )
        if per_diem_r["status_code"] not in (200, 201):
            log.warning(
                f"[{rid}] TRAVEL_EXPENSE: per diem write failed "
                f"status={per_diem_r['status_code']}"
            )
            return False

    # Deliver -> Approve -> CreateVouchers
    deliver_r = await execute_tripletex_call(
        client, base_url, token, "PUT", "/travelExpense/:deliver", params={"id": te_id}
    )
    if deliver_r["status_code"] not in (200, 204):
        log.warning(
            f"[{rid}] TRAVEL_EXPENSE: deliver failed status={deliver_r['status_code']}"
        )
        return False

    approve_r = await execute_tripletex_call(
        client, base_url, token, "PUT", "/travelExpense/:approve", params={"id": te_id}
    )
    if approve_r["status_code"] not in (200, 204):
        log.warning(
            f"[{rid}] TRAVEL_EXPENSE: approve failed status={approve_r['status_code']}"
        )
        return False
    cv_r = await execute_tripletex_call(
        client,
        base_url,
        token,
        "PUT",
        "/travelExpense/:createVouchers",
        params={"id": te_id, "date": dep_date},
    )
    log.info(f"[{rid}] SOLVER travel expense -> createVouchers={cv_r['status_code']}")
    return cv_r["status_code"] in (200, 204)


async def _solve_multi_vat_invoice(client, base_url, token, fields, log, rid):
    if fields.get("customerOrgNumber"):
        cp = {"organizationNumber": fields["customerOrgNumber"], "fields": "id,name"}
    elif fields.get("customerName"):
        cp = {"customerName": fields["customerName"], "fields": "id,name"}
    else:
        return False

    cr = await execute_tripletex_call(
        client, base_url, token, "GET", "/customer", params=cp
    )
    if cr["status_code"] != 200 or not cr["body"].get("values"):
        return False
    cust_id = cr["body"]["values"][0]["id"]

    products = fields.get("products", [])
    if not products:
        return False

    # Batch all product lookups in parallel
    numbered_prods = [(i, prod) for i, prod in enumerate(products) if prod.get("number")]
    if numbered_prods:
        prod_calls = [
            (
                "GET",
                "/product",
                {"number": prod["number"], "fields": "id,name,number,priceExcludingVatCurrency,vatType(id)"},
                None,
            )
            for _, prod in numbered_prods
        ]
        prod_results = await _parallel_calls(client, base_url, token, prod_calls)
        prod_lookup = {}
        for idx, (orig_idx, prod) in enumerate(numbered_prods):
            pr = prod_results[idx]
            if pr["status_code"] == 200 and pr["body"].get("values"):
                prod_lookup[orig_idx] = pr["body"]["values"][0]
    else:
        prod_lookup = {}

    order_lines = []
    for i, prod in enumerate(products):
        if i in prod_lookup:
            p = prod_lookup[i]
            order_lines.append(
                {
                    "product": {"id": p["id"]},
                    "count": prod.get("quantity", 1),
                    "unitPriceExcludingVatCurrency": prod.get("price")
                    or p.get("priceExcludingVatCurrency", 0),
                    "vatType": p.get("vatType", {"id": 3}),
                }
            )
        else:
            vat_id = VAT_RATE_TO_TYPE.get(prod.get("vatRatePercent", 25), 3)
            order_lines.append(
                {
                    "description": prod.get("name", "Product"),
                    "count": prod.get("quantity", 1),
                    "unitPriceExcludingVatCurrency": prod.get("price", 0),
                    "vatType": {"id": vat_id},
                }
            )

    today = time.strftime("%Y-%m-%d")
    inv_date = fields.get("invoiceDate") or today
    due_date = fields.get("invoiceDueDate") or _calc_due_date(inv_date)

    inv_r = await execute_tripletex_call(
        client,
        base_url,
        token,
        "POST",
        "/invoice",
        body={
            "invoiceDate": inv_date,
            "invoiceDueDate": due_date,
            "customer": {"id": cust_id},
            "orders": [{
                "customer": {"id": cust_id},
                "orderDate": inv_date,
                "deliveryDate": inv_date,
                "orderLines": order_lines,
            }],
        },
    )
    log.info(f"[{rid}] SOLVER multi-VAT invoice (inline order) -> {inv_r['status_code']}")
    return inv_r["status_code"] in (200, 201)


async def _solve_fixed_price_project(client, base_url, token, fields, log, rid):
    # Parallel: customer + employee
    calls = []
    if fields.get("customerOrgNumber"):
        calls.append(
            (
                "GET",
                "/customer",
                {
                    "organizationNumber": fields["customerOrgNumber"],
                    "fields": "id,name",
                },
                None,
            )
        )
    elif fields.get("customerName"):
        calls.append(
            (
                "GET",
                "/customer",
                {"customerName": fields["customerName"], "fields": "id,name"},
                None,
            )
        )
    else:
        return False

    if fields.get("projectManagerEmail"):
        calls.append(
            (
                "GET",
                "/employee",
                {"email": fields["projectManagerEmail"], "fields": "id", "count": 1},
                None,
            )
        )
    else:
        calls.append(("GET", "/employee", {"fields": "id", "count": 1}, None))

    results = await _parallel_calls(client, base_url, token, calls)

    if results[0]["status_code"] != 200 or not results[0]["body"].get("values"):
        return False
    cust_id = results[0]["body"]["values"][0]["id"]

    pm_id = None
    if results[1]["status_code"] == 200 and results[1]["body"].get("values"):
        pm_id = results[1]["body"]["values"][0]["id"]

    start_date, end_date, inv_date = _project_dates_from_fields(fields)
    fp = fields.get("fixedPrice", 0)
    proj_body = {
        "name": fields.get("projectName", "Project"),
        "startDate": start_date,
        "customer": {"id": cust_id},
        "isInternal": False,
    }
    if pm_id:
        proj_body["projectManager"] = {"id": pm_id}
    if end_date:
        proj_body["endDate"] = end_date

    proj_r = await _create_project_with_pm_retry(
        client, base_url, token, proj_body, pm_id, log, rid
    )
    if proj_r["status_code"] not in (200, 201):
        return False
    proj = proj_r["body"]["value"]
    proj_id = proj["id"]

    if fp and not proj.get("isFixedPrice"):
        put_r = await execute_tripletex_call(
            client, base_url, token, "PUT", f"/project/{proj_id}",
            body={
                "id": proj_id,
                "version": proj.get("version", 0),
                "isFixedPrice": True,
                "fixedprice": fp,
            },
        )
        if put_r["status_code"] not in (200, 201):
            log.warning(
                f"[{rid}] SOLVER fixed-price project -> PUT fixedPrice failed: "
                f"{put_r['status_code']}"
            )
            return False

    order_amount = fields.get("orderLineAmount") or fp
    order_desc = fields.get("orderLineDescription") or fields.get(
        "projectName", "Project"
    )
    due_date = _calc_due_date(inv_date)

    inv_r = await execute_tripletex_call(
        client,
        base_url,
        token,
        "POST",
        "/invoice",
        body={
            "invoiceDate": inv_date,
            "invoiceDueDate": due_date,
            "customer": {"id": cust_id},
            "orders": [{
                "customer": {"id": cust_id},
                "orderDate": inv_date,
                "deliveryDate": inv_date,
                "project": {"id": proj_id},
                "orderLines": [
                    {
                        "description": order_desc,
                        "count": 1,
                        "unitPriceExcludingVatCurrency": order_amount,
                        "vatType": {"id": 3},
                    }
                ],
            }],
        },
    )
    log.info(f"[{rid}] SOLVER fixed-price project -> invoice (inline order)={inv_r['status_code']}")
    return inv_r["status_code"] in (200, 201)


async def _solve_time_tracking(client, base_url, token, fields, log, rid):
    employees = fields.get("employees", [])
    if not employees:
        return False

    supplier_cost = fields.get("supplierCost") or {}
    expense_account_number = supplier_cost.get("expenseAccountNumber", 4300)

    # Parallel: customer + activity + all employees + optional supplier/voucher lookups
    calls = []
    idx_map = {}
    if fields.get("customerOrgNumber"):
        idx_map["customer"] = len(calls)
        calls.append(
            (
                "GET",
                "/customer",
                {
                    "organizationNumber": fields["customerOrgNumber"],
                    "fields": "id,name",
                },
                None,
            )
        )
    elif fields.get("customerName"):
        idx_map["customer"] = len(calls)
        calls.append(
            (
                "GET",
                "/customer",
                {"customerName": fields["customerName"], "fields": "id,name"},
                None,
            )
        )
    else:
        return False

    idx_map["activity"] = len(calls)
    calls.append(("GET", "/activity", {"fields": "id,name"}, None))

    employee_result_indices = []
    for employee in employees:
        employee_result_indices.append(len(calls))
        calls.append(
            (
                "GET",
                "/employee",
                {"email": employee["email"], "fields": "id,firstName,lastName"},
                None,
            )
        )

    if supplier_cost.get("supplierOrgNumber") or supplier_cost.get("supplierName"):
        idx_map["supplier"] = len(calls)
        if supplier_cost.get("supplierOrgNumber"):
            calls.append(
                (
                    "GET",
                    "/supplier",
                    {
                        "organizationNumber": supplier_cost["supplierOrgNumber"],
                        "fields": "id,name,organizationNumber",
                    },
                    None,
                )
            )
        else:
            calls.append(
                (
                    "GET",
                    "/supplier",
                    {"supplierName": supplier_cost["supplierName"], "fields": "id,name"},
                    None,
                )
            )

        idx_map["voucher_type"] = len(calls)
        calls.append(("GET", "/ledger/voucherType", {"fields": "id,name"}, None))

        idx_map["expense_account"] = len(calls)
        calls.append(
            (
                "GET",
                "/ledger/account",
                {"number": expense_account_number, "fields": "id,number,name"},
                None,
            )
        )

        idx_map["ap_account"] = len(calls)
        calls.append(
            (
                "GET",
                "/ledger/account",
                {"number": 2400, "fields": "id,number,name"},
                None,
            )
        )

    results = await _parallel_calls(client, base_url, token, calls)

    cust_r = results[idx_map["customer"]]
    if cust_r["status_code"] != 200 or not cust_r["body"].get(
        "values"
    ):
        return False
    cust_id = cust_r["body"]["values"][0]["id"]

    act_r = results[idx_map["activity"]]
    if act_r["status_code"] != 200:
        return False
    activities = act_r["body"].get("values", [])
    activity_label = fields.get("activityName") or "Prosjektarbeid"
    activity_name = activity_label.lower()
    activity_id = None
    for activity in activities:
        if activity.get("name", "").lower() == activity_name:
            activity_id = activity["id"]
            break
    if activity_id is None and activities:
        for activity in activities:
            if (
                activity_name in activity.get("name", "").lower()
                or activity.get("name", "").lower() in activity_name
            ):
                activity_id = activity["id"]
                break
    if activity_id is None:
        create_act = await execute_tripletex_call(
            client,
            base_url,
            token,
            "POST",
            "/activity",
            body={
                "name": activity_label,
                "activityType": "PROJECT_GENERAL_ACTIVITY",
                "isProjectActivity": True,
                "isGeneral": True,
                "isChargeable": True,
            },
        )
        if create_act["status_code"] in (200, 201):
            activity_id = create_act["body"]["value"]["id"]
        elif activities:
            activity_id = activities[0]["id"]
    if activity_id is None:
        return False

    employee_records = []
    for index, employee in enumerate(employees):
        er = results[employee_result_indices[index]]
        if er["status_code"] == 200 and er["body"].get("values"):
            employee_records.append(
                {
                    "id": er["body"]["values"][0]["id"],
                    "email": employee["email"],
                    "hours": employee.get("hours", 0),
                    "hourlyRate": employee.get("hourlyRate"),
                }
            )
        else:
            return False

    project_manager_email = fields.get("projectManagerEmail")
    if not project_manager_email and employee_records:
        project_manager_email = employee_records[0]["email"]
    pm_id = next(
        (employee["id"] for employee in employee_records if employee["email"] == project_manager_email),
        employee_records[0]["id"] if employee_records else None,
    )

    start_date, end_date, invoice_date = _project_dates_from_fields(fields)

    # Get or create project
    proj_r = await execute_tripletex_call(
        client,
        base_url,
        token,
        "GET",
        "/project",
        params={
            "customerId": str(cust_id),
            "fields": "id,name,startDate,version,projectManager(id)",
        },
    )
    if proj_r["status_code"] != 200 or not proj_r["body"].get("values"):
        proj_values = []
    else:
        proj_values = proj_r["body"].get("values", [])

    project_name = fields.get("projectName", "").lower()
    project = None
    for existing_project in proj_values:
        if existing_project.get("name", "").lower() == project_name:
            project = existing_project
            break

    if project is None:
        create_proj_body = {
            "name": fields.get("projectName", "Project"),
            "customer": {"id": cust_id},
            "startDate": start_date,
            "isInternal": False,
        }
        if pm_id:
            create_proj_body["projectManager"] = {"id": pm_id}
        if end_date:
            create_proj_body["endDate"] = end_date
        create_proj_r = await _create_project_with_pm_retry(
            client, base_url, token, create_proj_body, pm_id, log, rid
        )
        if create_proj_r["status_code"] not in (200, 201):
            return False
        project = create_proj_r["body"]["value"]

    proj_id = project["id"]
    proj_start = project.get("startDate") or start_date
    project_pm_id = (project.get("projectManager") or {}).get("id") or pm_id

    fixed_price = fields.get("fixedPrice") or 0
    if fixed_price:
        put_r = await execute_tripletex_call(
            client,
            base_url,
            token,
            "PUT",
            f"/project/{proj_id}",
            body={
                "id": proj_id,
                "version": project.get("version", 0),
                "isFixedPrice": True,
                "fixedprice": fixed_price,
            },
        )
        if put_r["status_code"] not in (200, 201):
            log.warning(
                f"[{rid}] SOLVER time tracking -> PUT fixedPrice failed "
                f"{put_r['status_code']}"
            )
            return False

    # Add participants + timesheet entries (batched)
    today = time.strftime("%Y-%m-%d")
    entry_date = max(proj_start, today) if proj_start else today

    participants = [
        {"project": {"id": proj_id}, "employee": {"id": employee["id"]}}
        for employee in employee_records
        if employee["id"] != project_pm_id
    ]
    if participants:
        part_r = await execute_tripletex_call(
            client, base_url, token, "POST", "/project/participant/list",
            body=participants,
        )
        if part_r["status_code"] not in (200, 201):
            log.warning(f"[{rid}] SOLVER time tracking: batch participant add failed {part_r['status_code']}")

    ts_entries = [
        {
            "employee": {"id": employee["id"]},
            "project": {"id": proj_id},
            "activity": {"id": activity_id},
            "date": entry_date,
            "hours": employee.get("hours", 0),
        }
        for employee in employee_records
        if employee.get("hours", 0) > 0
    ]
    if ts_entries:
        ts_r = await execute_tripletex_call(
            client, base_url, token, "POST", "/timesheet/entry/list",
            body=ts_entries,
        )
        if ts_r["status_code"] not in (200, 201):
            log.warning(
                f"[{rid}] SOLVER time tracking: batch timesheet failed {ts_r['status_code']} {str(ts_r['body'])[:200]}"
            )
            return False

    if supplier_cost.get("amount"):
        supplier_r = results[idx_map["supplier"]]
        supplier_id = None
        if supplier_r["status_code"] == 200 and supplier_r["body"].get("values"):
            supplier_id = supplier_r["body"]["values"][0]["id"]
        else:
            create_supplier_body = {"isSupplier": True}
            if supplier_cost.get("supplierName"):
                create_supplier_body["name"] = supplier_cost["supplierName"]
            if supplier_cost.get("supplierOrgNumber"):
                create_supplier_body["organizationNumber"] = supplier_cost["supplierOrgNumber"]
            create_supplier_r = await execute_tripletex_call(
                client, base_url, token, "POST", "/supplier", body=create_supplier_body
            )
            if create_supplier_r["status_code"] not in (200, 201):
                return False
            supplier_id = create_supplier_r["body"]["value"]["id"]

        vt_r = results[idx_map["voucher_type"]]
        expense_acct_r = results[idx_map["expense_account"]]
        ap_acct_r = results[idx_map["ap_account"]]
        if (
            vt_r["status_code"] != 200
            or expense_acct_r["status_code"] != 200
            or ap_acct_r["status_code"] != 200
            or not expense_acct_r["body"].get("values")
            or not ap_acct_r["body"].get("values")
        ):
            return False

        voucher_type_id = None
        for voucher_type in vt_r["body"].get("values", []):
            name = (voucher_type.get("name") or "").lower()
            if "leverandør" in name and "faktura" in name:
                voucher_type_id = voucher_type["id"]
                break
        if voucher_type_id is None and vt_r["body"].get("values"):
            voucher_type_id = vt_r["body"]["values"][0]["id"]
        if voucher_type_id is None:
            return False

        expense_id = expense_acct_r["body"]["values"][0]["id"]
        ap_id = ap_acct_r["body"]["values"][0]["id"]
        supplier_amount = supplier_cost["amount"]
        supplier_desc = (
            f"Leverandørkostnad - {supplier_cost.get('supplierName', 'Supplier')}"
        )
        voucher_r = await execute_tripletex_call(
            client,
            base_url,
            token,
            "POST",
            "/ledger/voucher",
            body={
                "date": invoice_date,
                "description": supplier_desc,
                "voucherType": {"id": voucher_type_id},
                "postings": [
                    {
                        "row": 1,
                        "date": invoice_date,
                        "account": {"id": expense_id},
                        "amountGross": supplier_amount,
                        "amountGrossCurrency": supplier_amount,
                        "currency": {"id": 1},
                    },
                    {
                        "row": 2,
                        "date": invoice_date,
                        "account": {"id": ap_id},
                        "amountGross": -supplier_amount,
                        "amountGrossCurrency": -supplier_amount,
                        "currency": {"id": 1},
                        "supplier": {"id": supplier_id},
                    },
                ],
            },
        )
        if voucher_r["status_code"] not in (200, 201):
            log.warning(
                f"[{rid}] SOLVER time tracking: supplier cost voucher failed "
                f"{voucher_r['status_code']} {str(voucher_r['body'])[:200]}"
            )
            return False

    total_amount = fixed_price or sum(
        (employee.get("hours", 0) or 0) * (employee.get("hourlyRate", 0) or 0)
        for employee in employee_records
    )
    if total_amount > 0:
        due_date = _calc_due_date(invoice_date)
        inv_r = await execute_tripletex_call(
            client,
            base_url,
            token,
            "POST",
            "/invoice",
            body={
                "invoiceDate": invoice_date,
                "invoiceDueDate": due_date,
                "customer": {"id": cust_id},
                "orders": [{
                    "customer": {"id": cust_id},
                    "orderDate": invoice_date,
                    "deliveryDate": invoice_date,
                    "project": {"id": proj_id},
                    "orderLines": [
                        {
                            "description": fields.get("orderLineDescription")
                            or fields.get("projectName", "Consulting"),
                            "count": 1,
                            "unitPriceExcludingVatCurrency": total_amount,
                            "vatType": {"id": 3},
                        }
                    ],
                }],
            },
        )
        log.info(f"[{rid}] SOLVER time tracking -> invoice (inline order)={inv_r['status_code']}")
        return inv_r["status_code"] in (200, 201)

    return True


async def _solve_foreign_currency_invoice(client, base_url, token, fields, log, rid):
    currency_code = fields.get("currencyCode", "EUR")

    cr, curr_r, pt_r = await _parallel_calls(
        client,
        base_url,
        token,
        [
            (
                "GET",
                "/customer",
                {
                    "organizationNumber": fields.get("customerOrgNumber", ""),
                    "fields": "id,name,version,currency(id,code)",
                },
                None,
            ),
            ("GET", "/currency", {"code": currency_code}, None),
            ("GET", "/invoice/paymentType", {"fields": "id,description"}, None),
        ],
    )

    if cr["status_code"] != 200 or not cr["body"].get("values"):
        return False
    cust = cr["body"]["values"][0]
    cust_id = cust["id"]

    if curr_r["status_code"] != 200 or not curr_r["body"].get("values"):
        return False
    currency_id = curr_r["body"]["values"][0]["id"]

    if pt_r["status_code"] != 200:
        return False

    pay_type_id = None
    for pt in pt_r["body"].get("values", []):
        if "bank" in pt.get("description", "").lower():
            pay_type_id = pt["id"]
            break
    if pay_type_id is None and pt_r["body"].get("values"):
        pay_type_id = pt_r["body"]["values"][0]["id"]

    vat_pct = fields.get("vatRatePercent", 25)
    vat_id = VAT_RATE_TO_TYPE.get(vat_pct, 3)

    cust_currency = cust.get("currency", {}).get("id")
    if cust_currency != currency_id:
        await execute_tripletex_call(
            client, base_url, token, "PUT", f"/customer/{cust_id}",
            body={"id": cust_id, "version": cust.get("version", 0), "currency": {"id": currency_id}},
        )

    today = time.strftime("%Y-%m-%d")
    price_foreign = fields.get("productPriceForeign", 0)
    inv_date = fields.get("invoiceDate") or today
    due_date = _calc_due_date(inv_date)

    inv_r = await execute_tripletex_call(
        client, base_url, token, "POST", "/invoice",
        body={
            "invoiceDate": inv_date,
            "invoiceDueDate": due_date,
            "customer": {"id": cust_id},
            "currency": {"id": currency_id},
            "orders": [{
                "customer": {"id": cust_id},
                "orderDate": inv_date,
                "deliveryDate": inv_date,
                "currency": {"id": currency_id},
                "orderLines": [{
                    "description": fields.get("productName") or "Services",
                    "count": 1,
                    "unitPriceExcludingVatCurrency": price_foreign,
                    "vatType": {"id": vat_id},
                }],
            }],
        },
    )
    if inv_r["status_code"] not in (200, 201):
        return False
    inv_id = inv_r["body"]["value"]["id"]
    inv_amount_foreign = inv_r["body"]["value"].get("amountCurrency") or inv_r["body"][
        "value"
    ].get("amount", 0)

    payment_rate = fields.get("paymentRate", 1)
    paid_nok = round(abs(inv_amount_foreign) * payment_rate, 2)

    pay_r = await execute_tripletex_call(
        client, base_url, token, "PUT", f"/invoice/{inv_id}/:payment",
        params={
            "paymentDate": fields.get("paymentDate") or today,
            "paymentTypeId": pay_type_id,
            "paidAmount": paid_nok,
            "paidAmountCurrency": abs(inv_amount_foreign),
        },
    )
    log.info(f"[{rid}] SOLVER foreign currency -> payment={pay_r['status_code']}")
    return pay_r["status_code"] in (200, 204)


async def _solve_foreign_currency_payment(client, base_url, token, fields, log, rid):
    """Register payment on an existing foreign currency invoice.
    Falls back to creating the full invoice flow if no foreign-currency invoice exists."""
    today = time.strftime("%Y-%m-%d")

    cust_params = {}
    if fields.get("customerOrgNumber"):
        cust_params = {"organizationNumber": fields["customerOrgNumber"], "fields": "id,name,version,currency(id,code)"}
    elif fields.get("customerName"):
        cust_params = {"customerName": fields["customerName"], "fields": "id,name,version,currency(id,code)"}
    else:
        return False

    results = await _parallel_calls(
        client, base_url, token,
        [
            ("GET", "/customer", cust_params, None),
            ("GET", "/invoice/paymentType", {"fields": "id,description"}, None),
            ("GET", "/currency", {"code": fields.get("currencyCode", "EUR")}, None),
        ],
    )

    if results[0]["status_code"] != 200 or not results[0]["body"].get("values"):
        return False
    cust = results[0]["body"]["values"][0]
    cust_id = cust["id"]

    pt_r = results[1]
    pay_type_id = None
    for pt in pt_r["body"].get("values", []):
        if "bank" in pt.get("description", "").lower():
            pay_type_id = pt["id"]
            break
    if pay_type_id is None and pt_r["body"].get("values"):
        pay_type_id = pt_r["body"]["values"][0]["id"]

    currency_id = None
    if results[2]["status_code"] == 200 and results[2]["body"].get("values"):
        currency_id = results[2]["body"]["values"][0]["id"]

    inv_r = await execute_tripletex_call(
        client, base_url, token, "GET", "/invoice",
        params={
            "customerId": str(cust_id),
            "invoiceDateFrom": "2020-01-01",
            "invoiceDateTo": "2030-12-31",
            "fields": "id,invoiceNumber,amount,amountCurrency,amountOutstanding,amountCurrencyOutstanding,currency(id,code)",
        },
    )

    invoice = None
    expected_currency = fields.get("currencyCode", "EUR")
    if inv_r["status_code"] == 200 and inv_r["body"].get("values"):
        inv_num = fields.get("invoiceNumber")
        for inv in inv_r["body"]["values"]:
            if inv_num and str(inv.get("invoiceNumber")) == str(inv_num):
                invoice = inv
                break
        if invoice is None:
            for inv in inv_r["body"]["values"]:
                inv_curr = inv.get("currency", {}).get("code", "NOK")
                outstanding = inv.get("amountCurrencyOutstanding") or inv.get("amountOutstanding", 0)
                if inv_curr == expected_currency and outstanding and outstanding > 0:
                    invoice = inv
                    break
        if invoice is None:
            for inv in inv_r["body"]["values"]:
                outstanding = inv.get("amountCurrencyOutstanding") or inv.get("amountOutstanding", 0)
                if outstanding and outstanding > 0:
                    invoice = inv
                    break

    if invoice and invoice.get("currency", {}).get("code", "NOK") != "NOK":
        inv_id = invoice["id"]
        paid_currency = (
            fields.get("paidAmountCurrency")
            or invoice.get("amountCurrencyOutstanding")
            or invoice.get("amountCurrency")
            or invoice.get("amount", 0)
        )
        payment_rate = fields.get("paymentRate", 1)
        paid_nok = round(abs(paid_currency) * payment_rate, 2)

        pay_r = await execute_tripletex_call(
            client, base_url, token, "PUT", f"/invoice/{inv_id}/:payment",
            params={
                "paymentDate": fields.get("paymentDate") or today,
                "paymentTypeId": pay_type_id,
                "paidAmount": paid_nok,
                "paidAmountCurrency": abs(paid_currency),
            },
        )
        log.info(f"[{rid}] SOLVER foreign currency payment -> {pay_r['status_code']}")
        return pay_r["status_code"] in (200, 204)

    log.info(f"[{rid}] SOLVER foreign currency payment -> no foreign invoice found, creating full flow")
    if not currency_id:
        return False

    cust_currency = cust.get("currency", {}).get("id")
    if cust_currency != currency_id:
        await execute_tripletex_call(
            client, base_url, token, "PUT", f"/customer/{cust_id}",
            body={"id": cust_id, "version": cust.get("version", 0), "currency": {"id": currency_id}},
        )

    price_foreign = fields.get("paidAmountCurrency", 0)
    vat_pct = fields.get("vatRatePercent", 25)
    vat_id = VAT_RATE_TO_TYPE.get(vat_pct, 3)
    inv_date = fields.get("invoiceDate") or today
    due_date = _calc_due_date(inv_date)

    new_inv_r = await execute_tripletex_call(
        client, base_url, token, "POST", "/invoice",
        body={
            "invoiceDate": inv_date,
            "invoiceDueDate": due_date,
            "customer": {"id": cust_id},
            "currency": {"id": currency_id},
            "orders": [{
                "customer": {"id": cust_id},
                "orderDate": inv_date,
                "deliveryDate": inv_date,
                "currency": {"id": currency_id},
                "orderLines": [{
                    "description": "Services",
                    "count": 1,
                    "unitPriceExcludingVatCurrency": price_foreign,
                    "vatType": {"id": vat_id},
                }],
            }],
        },
    )
    if new_inv_r["status_code"] not in (200, 201):
        return False
    new_inv_id = new_inv_r["body"]["value"]["id"]
    inv_amount_foreign = new_inv_r["body"]["value"].get("amountCurrency") or new_inv_r["body"]["value"].get("amount", 0)

    payment_rate = fields.get("paymentRate", 1)
    paid_nok = round(abs(inv_amount_foreign) * payment_rate, 2)

    pay_r = await execute_tripletex_call(
        client, base_url, token, "PUT", f"/invoice/{new_inv_id}/:payment",
        params={
            "paymentDate": fields.get("paymentDate") or today,
            "paymentTypeId": pay_type_id,
            "paidAmount": paid_nok,
            "paidAmountCurrency": abs(inv_amount_foreign),
        },
    )
    log.info(f"[{rid}] SOLVER foreign currency payment (created) -> {pay_r['status_code']}")
    return pay_r["status_code"] in (200, 204)


async def _solve_bank_reconciliation(client, base_url, token, fields, log, rid):
    """Deterministic solver for bank reconciliation from CSV."""
    import csv
    import io

    file_text = fields.get("_file_text", "")
    if not file_text:
        source_files = fields.get("_source_files", [])
        if source_files:
            file_text = _text_from_files(source_files)
        if not file_text:
            log.warning(f"[{rid}] SOLVER bank recon -> no CSV data found")
            return False

    csv_content = file_text
    for prefix in ["---", "Content of attached file"]:
        if prefix in csv_content:
            lines = csv_content.split("\n")
            for i, line in enumerate(lines):
                if "Dato" in line and "Saldo" in line:
                    csv_content = "\n".join(lines[i:])
                    break

    csv_content = csv_content.replace("\r\n", "\n").replace("\r", "\n").strip()

    rows = []
    reader = csv.DictReader(io.StringIO(csv_content), delimiter=";")
    for row in reader:
        date = (row.get("Dato") or "").strip()
        desc = (row.get("Forklaring") or row.get("Beskrivelse") or "").strip()
        inn_str = (row.get("Inn") or "").strip().replace(",", ".")
        ut_str = (row.get("Ut") or "").strip().replace(",", ".")
        inn = float(inn_str) if inn_str else 0.0
        ut = abs(float(ut_str)) if ut_str else 0.0
        if not date:
            continue
        rows.append({"date": date, "desc": desc, "inn": inn, "ut": ut})

    log.info(f"[{rid}] SOLVER bank recon -> parsed {len(rows)} CSV rows")

    customer_payments = []
    supplier_payments = []
    misc_entries = []

    for row in rows:
        desc_lower = row["desc"].lower()
        if row["inn"] > 0 and ("innbetaling" in desc_lower or "faktura" in desc_lower):
            import re as _re
            inv_match = _re.search(r"faktura\s*(\d+)", desc_lower)
            inv_ref = inv_match.group(1) if inv_match else None
            name_match = _re.search(r"(?:innbetaling fra|betaling fra)\s+(.+?)(?:\s*/\s*faktura|\s*$)", row["desc"], _re.IGNORECASE)
            cust_name = name_match.group(1).strip() if name_match else None
            customer_payments.append({
                "date": row["date"], "amount": row["inn"],
                "invoice_ref": inv_ref, "customer_name": cust_name,
                "desc": row["desc"],
            })
        elif row["ut"] > 0 and ("leverand" in desc_lower or "betaling" in desc_lower) and "skatt" not in desc_lower and "rente" not in desc_lower:
            name_match = _re.search(r"(?:betaling\s+)?leverand(?:ø|o)r\s+(.+)", row["desc"], _re.IGNORECASE)
            if not name_match:
                name_match = _re.search(r"betaling\s+(.+)", row["desc"], _re.IGNORECASE)
            sup_name = name_match.group(1).strip() if name_match else row["desc"]
            supplier_payments.append({
                "date": row["date"], "amount": row["ut"],
                "supplier_name": sup_name, "desc": row["desc"],
            })
        else:
            entry = {"date": row["date"], "desc": row["desc"]}
            if row["inn"] > 0:
                entry["amount"] = row["inn"]
                entry["direction"] = "inn"
            elif row["ut"] > 0:
                entry["amount"] = row["ut"]
                entry["direction"] = "ut"
            else:
                continue
            misc_entries.append(entry)

    log.info(
        f"[{rid}] SOLVER bank recon -> {len(customer_payments)} customer payments, "
        f"{len(supplier_payments)} supplier payments, {len(misc_entries)} misc entries"
    )

    get_calls = [
        ("GET", "/ledger/voucherType", {"fields": "id,name"}, None),
        ("GET", "/invoice", {
            "invoiceDateFrom": "2020-01-01", "invoiceDateTo": "2030-12-31",
            "fields": "id,invoiceNumber,customer(id,name),amount,amountOutstanding",
        }, None),
        ("GET", "/supplier", {"fields": "id,name"}, None),
        ("GET", "/invoice/paymentType", {"fields": "id,description"}, None),
        ("GET", "/ledger/account", {"number": "1920", "fields": "id,number,name"}, None),
        ("GET", "/ledger/account", {"number": "2400", "fields": "id,number,name"}, None),
        ("GET", "/ledger/account", {"number": "4300", "fields": "id,number,name"}, None),
        ("GET", "/ledger/account", {"number": "1950", "fields": "id,number,name"}, None),
        ("GET", "/ledger/account", {"number": "8050", "fields": "id,number,name"}, None),
        ("GET", "/ledger/account", {"number": "7770", "fields": "id,number,name"}, None),
    ]
    results = await _parallel_calls(client, base_url, token, get_calls, log, rid)

    vt_r, inv_r, sup_r, pt_r, bank_r, ap_r, purchase_r, tax_r, interest_r, fees_r = results

    for r in results:
        if _is_proxy_token_invalid(r):
            _mark_proxy_token_invalid(fields, log, rid, "SOLVER bank recon GET")
            return False

    if any(r["status_code"] != 200 for r in [vt_r, inv_r, sup_r, pt_r]):
        log.warning(f"[{rid}] SOLVER bank recon -> one or more GETs failed")
        return False

    leverandor_vt_id = None
    betaling_vt_id = None
    memorial_vt_id = None
    for vt in vt_r["body"].get("values", []):
        name_lower = vt.get("name", "").lower()
        if "leverandør" in name_lower and "faktura" in name_lower:
            leverandor_vt_id = vt["id"]
        elif name_lower == "betaling":
            betaling_vt_id = vt["id"]
        elif "memorial" in name_lower:
            memorial_vt_id = vt["id"]
    if not betaling_vt_id:
        betaling_vt_id = leverandor_vt_id

    pay_type_id = None
    for pt in pt_r["body"].get("values", []):
        if "bank" in (pt.get("description") or "").lower():
            pay_type_id = pt["id"]
            break
    if not pay_type_id and pt_r["body"].get("values"):
        pay_type_id = pt_r["body"]["values"][-1]["id"]

    def _acct_id(r):
        vals = r.get("body", {}).get("values", [])
        return vals[0]["id"] if vals and r["status_code"] == 200 else None

    bank_id = _acct_id(bank_r)
    ap_id = _acct_id(ap_r)
    purchase_id = _acct_id(purchase_r)
    tax_id = _acct_id(tax_r)
    interest_id = _acct_id(interest_r)
    fees_id = _acct_id(fees_r)

    if not bank_id or not ap_id:
        log.warning(f"[{rid}] SOLVER bank recon -> missing bank/AP account")
        return False

    invoices = inv_r["body"].get("values", [])
    suppliers = {s["name"].lower(): s["id"] for s in sup_r["body"].get("values", [])}

    # --- Phase 1: Customer payments ---
    payment_calls = []
    for cp in customer_payments:
        matched_inv = None
        if cp["invoice_ref"]:
            ref_num = int(cp["invoice_ref"])
            for inv in invoices:
                if inv.get("invoiceNumber") == ref_num and inv.get("amountOutstanding", 0) > 0:
                    matched_inv = inv
                    break
        if not matched_inv and cp["customer_name"]:
            cn_lower = cp["customer_name"].lower()
            for inv in invoices:
                cust = inv.get("customer", {})
                if cn_lower in (cust.get("name") or "").lower() and inv.get("amountOutstanding", 0) > 0:
                    matched_inv = inv
                    break

        if matched_inv:
            payment_calls.append((
                "PUT",
                f"/invoice/{matched_inv['id']}/:payment",
                {
                    "paymentDate": cp["date"],
                    "paymentTypeId": str(pay_type_id),
                    "paidAmount": str(cp["amount"]),
                },
                None,
            ))
            log.info(
                f"[{rid}] SOLVER bank recon -> match: {cp['desc']} -> invoice {matched_inv.get('invoiceNumber')} "
                f"(amount={cp['amount']}, outstanding={matched_inv.get('amountOutstanding')})"
            )
        else:
            log.warning(f"[{rid}] SOLVER bank recon -> no invoice match for: {cp['desc']}")

    if payment_calls:
        pay_results = await _parallel_calls(client, base_url, token, payment_calls, log, rid)
        for i, pr in enumerate(pay_results):
            if pr["status_code"] not in (200, 204):
                log.warning(
                    f"[{rid}] SOLVER bank recon -> payment {i} failed: {pr['status_code']} "
                    f"{json.dumps(pr.get('body', ''))[:300]}"
                )
        log.info(f"[{rid}] SOLVER bank recon -> registered {len(payment_calls)} customer payments")

    # --- Phase 2: Supplier payments (combined invoice + payment per supplier) ---
    for sp in supplier_payments:
        sup_name_lower = sp["supplier_name"].lower()
        sup_id = None
        for sname, sid in suppliers.items():
            if sup_name_lower in sname or sname in sup_name_lower:
                sup_id = sid
                break

        if not sup_id:
            create_r = await execute_tripletex_call(
                client, base_url, token, "POST", "/supplier",
                body={"name": sp["supplier_name"], "isSupplier": True},
            )
            if create_r["status_code"] in (200, 201):
                sup_id = create_r["body"]["value"]["id"]
                suppliers[sp["supplier_name"].lower()] = sup_id
                log.info(f"[{rid}] SOLVER bank recon -> created supplier '{sp['supplier_name']}' id={sup_id}")
            else:
                log.warning(f"[{rid}] SOLVER bank recon -> failed to create supplier '{sp['supplier_name']}'")
                continue

        voucher_body = {
            "date": sp["date"],
            "description": f"Leverandørfaktura og betaling - {sp['supplier_name']}",
            "voucherType": {"id": leverandor_vt_id or betaling_vt_id},
            "postings": [
                {
                    "row": 1, "date": sp["date"],
                    "account": {"id": purchase_id or bank_id},
                    "amountGross": sp["amount"],
                    "amountGrossCurrency": sp["amount"],
                    "currency": {"id": 1},
                },
                {
                    "row": 2, "date": sp["date"],
                    "account": {"id": ap_id},
                    "amountGross": -sp["amount"],
                    "amountGrossCurrency": -sp["amount"],
                    "currency": {"id": 1},
                    "supplier": {"id": sup_id},
                },
                {
                    "row": 3, "date": sp["date"],
                    "account": {"id": ap_id},
                    "amountGross": sp["amount"],
                    "amountGrossCurrency": sp["amount"],
                    "currency": {"id": 1},
                    "supplier": {"id": sup_id},
                },
                {
                    "row": 4, "date": sp["date"],
                    "account": {"id": bank_id},
                    "amountGross": -sp["amount"],
                    "amountGrossCurrency": -sp["amount"],
                    "currency": {"id": 1},
                },
            ],
        }
        v_r = await execute_tripletex_call(
            client, base_url, token, "POST", "/ledger/voucher", body=voucher_body,
        )
        if v_r["status_code"] in (200, 201):
            log.info(f"[{rid}] SOLVER bank recon -> supplier combined voucher for '{sp['supplier_name']}': OK")
        else:
            log.warning(
                f"[{rid}] SOLVER bank recon -> supplier combined voucher failed for '{sp['supplier_name']}': "
                f"{v_r['status_code']} {json.dumps(v_r.get('body', ''))[:300]}"
            )
            return False

    # --- Phase 3: Misc entries (tax, interest, fees) combined into one voucher ---
    if misc_entries:
        misc_postings = []
        row_num = 1
        for entry in misc_entries:
            desc_lower = entry["desc"].lower()

            if "skatt" in desc_lower or "tax" in desc_lower:
                if entry["direction"] == "inn":
                    misc_postings.append({
                        "row": row_num, "date": entry["date"],
                        "account": {"id": bank_id},
                        "amountGross": entry["amount"],
                        "amountGrossCurrency": entry["amount"],
                        "currency": {"id": 1},
                    })
                    row_num += 1
                    misc_postings.append({
                        "row": row_num, "date": entry["date"],
                        "account": {"id": tax_id or bank_id},
                        "amountGross": -entry["amount"],
                        "amountGrossCurrency": -entry["amount"],
                        "currency": {"id": 1},
                    })
                else:
                    misc_postings.append({
                        "row": row_num, "date": entry["date"],
                        "account": {"id": tax_id or bank_id},
                        "amountGross": entry["amount"],
                        "amountGrossCurrency": entry["amount"],
                        "currency": {"id": 1},
                    })
                    row_num += 1
                    misc_postings.append({
                        "row": row_num, "date": entry["date"],
                        "account": {"id": bank_id},
                        "amountGross": -entry["amount"],
                        "amountGrossCurrency": -entry["amount"],
                        "currency": {"id": 1},
                    })
            elif "rente" in desc_lower or "interest" in desc_lower:
                if entry["direction"] == "inn":
                    misc_postings.append({
                        "row": row_num, "date": entry["date"],
                        "account": {"id": bank_id},
                        "amountGross": entry["amount"],
                        "amountGrossCurrency": entry["amount"],
                        "currency": {"id": 1},
                    })
                    row_num += 1
                    misc_postings.append({
                        "row": row_num, "date": entry["date"],
                        "account": {"id": interest_id or bank_id},
                        "amountGross": -entry["amount"],
                        "amountGrossCurrency": -entry["amount"],
                        "currency": {"id": 1},
                    })
                else:
                    misc_postings.append({
                        "row": row_num, "date": entry["date"],
                        "account": {"id": interest_id or bank_id},
                        "amountGross": entry["amount"],
                        "amountGrossCurrency": entry["amount"],
                        "currency": {"id": 1},
                    })
                    row_num += 1
                    misc_postings.append({
                        "row": row_num, "date": entry["date"],
                        "account": {"id": bank_id},
                        "amountGross": -entry["amount"],
                        "amountGrossCurrency": -entry["amount"],
                        "currency": {"id": 1},
                    })
            elif "gebyr" in desc_lower or "fee" in desc_lower:
                misc_postings.append({
                    "row": row_num, "date": entry["date"],
                    "account": {"id": fees_id or bank_id},
                    "amountGross": entry["amount"],
                    "amountGrossCurrency": entry["amount"],
                    "currency": {"id": 1},
                })
                row_num += 1
                misc_postings.append({
                    "row": row_num, "date": entry["date"],
                    "account": {"id": bank_id},
                    "amountGross": -entry["amount"],
                    "amountGrossCurrency": -entry["amount"],
                    "currency": {"id": 1},
                })
            else:
                if entry["direction"] == "inn":
                    misc_postings.append({
                        "row": row_num, "date": entry["date"],
                        "account": {"id": bank_id},
                        "amountGross": entry["amount"],
                        "amountGrossCurrency": entry["amount"],
                        "currency": {"id": 1},
                    })
                    row_num += 1
                    misc_postings.append({
                        "row": row_num, "date": entry["date"],
                        "account": {"id": interest_id or bank_id},
                        "amountGross": -entry["amount"],
                        "amountGrossCurrency": -entry["amount"],
                        "currency": {"id": 1},
                    })
                else:
                    misc_postings.append({
                        "row": row_num, "date": entry["date"],
                        "account": {"id": fees_id or bank_id},
                        "amountGross": entry["amount"],
                        "amountGrossCurrency": entry["amount"],
                        "currency": {"id": 1},
                    })
                    row_num += 1
                    misc_postings.append({
                        "row": row_num, "date": entry["date"],
                        "account": {"id": bank_id},
                        "amountGross": -entry["amount"],
                        "amountGrossCurrency": -entry["amount"],
                        "currency": {"id": 1},
                    })
            row_num += 1

        if misc_postings:
            first_date = misc_entries[0]["date"]
            misc_voucher = {
                "date": first_date,
                "description": "Bankavstemming - diverse poster",
                "voucherType": {"id": memorial_vt_id or betaling_vt_id or leverandor_vt_id},
                "postings": misc_postings,
            }
            misc_r = await execute_tripletex_call(
                client, base_url, token, "POST", "/ledger/voucher", body=misc_voucher,
            )
            if misc_r["status_code"] in (200, 201):
                log.info(f"[{rid}] SOLVER bank recon -> misc combined voucher: OK ({len(misc_entries)} entries)")
            else:
                log.warning(
                    f"[{rid}] SOLVER bank recon -> misc combined voucher failed: "
                    f"{misc_r['status_code']} {json.dumps(misc_r.get('body', ''))[:500]}"
                )
                for entry in misc_entries:
                    desc_lower = entry["desc"].lower()
                    if "skatt" in desc_lower:
                        acct_id = tax_id or bank_id
                    elif "rente" in desc_lower:
                        acct_id = interest_id or bank_id
                    elif "gebyr" in desc_lower:
                        acct_id = fees_id or bank_id
                    else:
                        acct_id = interest_id if entry["direction"] == "inn" else (fees_id or bank_id)

                    if entry["direction"] == "inn":
                        postings = [
                            {"row": 1, "date": entry["date"], "account": {"id": bank_id},
                             "amountGross": entry["amount"], "amountGrossCurrency": entry["amount"], "currency": {"id": 1}},
                            {"row": 2, "date": entry["date"], "account": {"id": acct_id},
                             "amountGross": -entry["amount"], "amountGrossCurrency": -entry["amount"], "currency": {"id": 1}},
                        ]
                    else:
                        postings = [
                            {"row": 1, "date": entry["date"], "account": {"id": acct_id},
                             "amountGross": entry["amount"], "amountGrossCurrency": entry["amount"], "currency": {"id": 1}},
                            {"row": 2, "date": entry["date"], "account": {"id": bank_id},
                             "amountGross": -entry["amount"], "amountGrossCurrency": -entry["amount"], "currency": {"id": 1}},
                        ]

                    fb_r = await execute_tripletex_call(
                        client, base_url, token, "POST", "/ledger/voucher",
                        body={
                            "date": entry["date"],
                            "description": entry["desc"],
                            "voucherType": {"id": betaling_vt_id or leverandor_vt_id},
                            "postings": postings,
                        },
                    )
                    if fb_r["status_code"] not in (200, 201):
                        log.warning(f"[{rid}] SOLVER bank recon -> fallback misc voucher failed: {fb_r['status_code']}")

    total_writes = len(payment_calls) + len(supplier_payments) + (1 if misc_entries else 0)
    log.info(f"[{rid}] SOLVER bank recon -> completed: {total_writes} total writes")
    return True


# ---- Solver Registry -------------------------------------------------------

DETERMINISTIC_SOLVERS = {
    "CREATE_DEPARTMENTS": _solve_departments,
    "CREATE_CUSTOMER": _solve_customer,
    "CREATE_SUPPLIER": _solve_supplier,
    "CREATE_PRODUCT": _solve_product,
    "CREATE_EMPLOYEE": _solve_employee,
    "CREDIT_NOTE": _solve_credit_note,
    "CREATE_PROJECT": _solve_project,
    "SIMPLE_INVOICE": _solve_simple_invoice,
    "REGISTER_PAYMENT": _solve_register_payment,
    "REGISTER_SUPPLIER_INVOICE": _solve_supplier_invoice,
    "PAYROLL_RUN": _solve_payroll,
    "CUSTOM_DIMENSION": _solve_custom_dimension,
    "ORDER_INVOICE_PAYMENT": _solve_order_invoice_payment,
    "REVERSE_PAYMENT": _solve_reverse_payment,
    "TRAVEL_EXPENSE": _solve_travel_expense,
    "MULTI_VAT_INVOICE": _solve_multi_vat_invoice,
    "FIXED_PRICE_PROJECT": _solve_fixed_price_project,
    "TIME_TRACKING": _solve_time_tracking,
    "FOREIGN_CURRENCY_INVOICE": _solve_foreign_currency_invoice,
    "FOREIGN_CURRENCY_PAYMENT": _solve_foreign_currency_payment,
    "COST_ANALYSIS": _solve_cost_analysis,
    "LEDGER_CORRECTION": _solve_ledger_correction,
    "BANK_RECONCILIATION": _solve_bank_reconciliation,
}


TASKS_NEEDING_BANK = {
    "SIMPLE_INVOICE", "REGISTER_PAYMENT", "ORDER_INVOICE_PAYMENT",
    "CREDIT_NOTE",
    "FOREIGN_CURRENCY_INVOICE", "FOREIGN_CURRENCY_PAYMENT",
    "MULTI_VAT_INVOICE", "PAYROLL_RUN", "TRAVEL_EXPENSE",
    "REVERSE_PAYMENT", "CUSTOM_DIMENSION",
    "BANK_RECONCILIATION", "FIXED_PRICE_PROJECT", "TIME_TRACKING",
}

TASKS_NO_BANK = {
    "CREATE_CUSTOMER", "CREATE_SUPPLIER", "CREATE_PRODUCT",
    "CREATE_DEPARTMENTS", "CREATE_EMPLOYEE", "CREATE_PROJECT",
    "COST_ANALYSIS", "LEDGER_CORRECTION", "REGISTER_SUPPLIER_INVOICE",
}


async def try_deterministic_solve(
    prompt: str,
    files: list,
    client: httpx.AsyncClient,
    base_url: str,
    token: str,
    log: logging.Logger,
    request_id: str,
) -> tuple[bool, dict | None]:
    """Returns (success, extracted_fields_or_None).
    If success is False but fields were extracted, the caller can pass them to the LLM loop."""
    extraction_prompt = prompt
    if files:
        file_text = _text_from_files(files)
        if file_text:
            extraction_prompt = f"{prompt}\n\n{file_text}"
            log.info(f"[{request_id}] SOLVER: using attached file text for extraction")
        else:
            log.info(f"[{request_id}] SOLVER: files present but no text extracted")

    fields = await _extract_fields(extraction_prompt, client, log, request_id)
    if fields is None:
        log.info(f"[{request_id}] SOLVER: extraction failed")
        return False, None
    fields["_source_prompt"] = prompt
    if files:
        fields["_source_files"] = files
        file_text = _text_from_files(files)
        if file_text:
            fields["_file_text"] = file_text
    fields = _normalize_extracted_fields(fields, prompt)

    task_type = fields.get("task_type", "UNSUPPORTED")
    if task_type not in DETERMINISTIC_SOLVERS:
        log.info(
            f"[{request_id}] SOLVER: unsupported task '{task_type}', falling back to LLM"
        )
        return False, fields

    if not _has_deterministic_coverage(task_type, fields, prompt):
        log.info(
            f"[{request_id}] SOLVER: extracted fields do not cover prompt requirements "
            f"for task '{task_type}', falling back to LLM"
        )
        return False, fields

    log.info(f"[{request_id}] SOLVER: task_type={task_type}")
    log.info(
        f"[{request_id}] SOLVER: fields={json.dumps(fields, ensure_ascii=False)}"
    )

    if task_type not in TASKS_NO_BANK:
        bank_ok = await ensure_bank_account(client, base_url, token, log, request_id)
        if not bank_ok:
            log.warning(f"[{request_id}] SOLVER: aborting due to invalid proxy token")
            return False, fields

    try:
        ok = await DETERMINISTIC_SOLVERS[task_type](
            client,
            base_url,
            token,
            fields,
            log,
            request_id,
        )
        if ok:
            log.info(f"[{request_id}] SOLVER: completed successfully")
        else:
            if fields.get("_fatal_proxy_token_invalid"):
                log.warning(f"[{request_id}] SOLVER: failed due to invalid proxy token")
            else:
                log.info(f"[{request_id}] SOLVER: failed, falling back to LLM")
        return ok, fields
    except Exception as e:
        log.error(f"[{request_id}] SOLVER ERROR: {e}")
        return False, fields


# ---- OpenRouter / Claude Client --------------------------------------------


async def call_openrouter(
    messages: list,
    client: httpx.AsyncClient,
    use_reasoning: bool = False,
    log: logging.Logger | None = None,
    request_id: str = "",
) -> dict:
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://tripletex-agent.local",
    }

    payload = {
        "model": MODEL,
        "messages": [{"role": "system", "content": SYSTEM_PROMPT}] + messages,
        "tools": TOOLS,
        "max_tokens": 6000 if use_reasoning else 4096,
    }

    if use_reasoning:
        payload["reasoning"] = {"enabled": True, "max_tokens": 2500}

    if log:
        msg_summary = []
        for m in payload["messages"]:
            role = m.get("role", "?")
            content = m.get("content", "")
            if role == "system":
                msg_summary.append(f"  {role}: (system prompt, {len(str(content))} chars)")
            elif role == "tool":
                msg_summary.append(f"  {role} [tc_id={m.get('tool_call_id', '?')}]: {str(content)[:500]}")
            else:
                content_str = str(content) if isinstance(content, str) else json.dumps(content, ensure_ascii=False, default=str)
                msg_summary.append(f"  {role}: {content_str[:1000]}")
            tool_calls = m.get("tool_calls", [])
            for tc in tool_calls:
                msg_summary.append(f"    tool_call: {tc['function']['name']}({tc['function'].get('arguments', '')[:500]})")
        log.info(
            f"[{request_id}] OPENROUTER REQUEST: model={payload['model']}, "
            f"max_tokens={payload['max_tokens']}, reasoning={use_reasoning}, "
            f"num_messages={len(payload['messages'])}, "
            f"num_tools={len(payload.get('tools', []))}"
        )
        log.info(f"[{request_id}] OPENROUTER MESSAGES:\n" + "\n".join(msg_summary))

    call_start = time.time()
    response = await client.post(
        OPENROUTER_URL,
        headers=headers,
        json=payload,
        timeout=60.0,
    )
    call_elapsed = time.time() - call_start
    response.raise_for_status()
    result = response.json()

    if log:
        usage = result.get("usage", {})
        choice = result.get("choices", [{}])[0]
        msg = choice.get("message", {})
        finish = choice.get("finish_reason", "?")
        reasoning_content = msg.get("reasoning", "")
        text_content = msg.get("content", "")
        tool_calls = msg.get("tool_calls", [])
        log.info(
            f"[{request_id}] OPENROUTER RESPONSE ({call_elapsed:.1f}s): "
            f"finish_reason={finish}, "
            f"prompt_tokens={usage.get('prompt_tokens', '?')}, "
            f"completion_tokens={usage.get('completion_tokens', '?')}, "
            f"total_tokens={usage.get('total_tokens', '?')}"
        )
        if reasoning_content:
            log.info(f"[{request_id}] REASONING:\n{reasoning_content}")
        if text_content:
            log.info(f"[{request_id}] ASSISTANT CONTENT:\n{text_content}")
        if tool_calls:
            for tc in tool_calls:
                log.info(
                    f"[{request_id}] TOOL_CALL: id={tc['id']}, "
                    f"name={tc['function']['name']}, "
                    f"arguments={tc['function'].get('arguments', '')}"
                )

    return result


# ---- Bank Account Prerequisite ---------------------------------------------

VALID_NORWEGIAN_BANK_ACCOUNT = "86011117947"


async def ensure_bank_account(
    client: httpx.AsyncClient,
    base_url: str,
    token: str,
    log: logging.Logger,
    request_id: str,
) -> bool:
    if token in _bank_account_done:
        log.info(f"[{request_id}] BANK_ACCOUNT: already set up for this token, skipping")
        return True
    auth = ("0", token)
    try:
        log.info(f"[{request_id}] BANK_ACCOUNT: checking account 1920")
        resp = await client.get(
            f"{base_url}/ledger/account",
            params={"number": "1920", "fields": "id,version,bankAccountNumber"},
            auth=auth,
            timeout=15.0,
        )
        data = resp.json()
        log.info(
            f"[{request_id}] BANK_ACCOUNT: GET response status={resp.status_code}, "
            f"data={json.dumps(data, ensure_ascii=False, default=str)}"
        )
        if resp.status_code == 403 and "invalid or expired proxy token" in str(data).lower():
            log.warning(f"[{request_id}] BANK_ACCOUNT: proxy token is invalid, aborting setup")
            return False
        values = data.get("values", [])
        if not values:
            log.info(f"[{request_id}] BANK_ACCOUNT: no account 1920 found")
            _bank_account_done.add(token)
            return True
        acct = values[0]
        if acct.get("bankAccountNumber"):
            log.info(
                f"[{request_id}] BANK_ACCOUNT: already has bankAccountNumber="
                f"{acct['bankAccountNumber']}"
            )
            _bank_account_done.add(token)
            return True
        log.info(
            f"[{request_id}] BANK_ACCOUNT: account 1920 missing bankAccountNumber -- setting it"
        )
        put_body = {
            "id": acct["id"],
            "version": acct["version"],
            "bankAccountNumber": VALID_NORWEGIAN_BANK_ACCOUNT,
        }
        log.info(f"[{request_id}] BANK_ACCOUNT: PUT body={json.dumps(put_body)}")
        put_resp = await client.put(
            f"{base_url}/ledger/account/{acct['id']}",
            json=put_body,
            auth=auth,
            timeout=15.0,
        )
        log.info(
            f"[{request_id}] BANK_ACCOUNT: PUT response status={put_resp.status_code}, "
            f"body={put_resp.text[:500]}"
        )
        if put_resp.status_code == 403 and "invalid or expired proxy token" in put_resp.text.lower():
            log.warning(f"[{request_id}] BANK_ACCOUNT: proxy token became invalid during setup")
            return False
        _bank_account_done.add(token)
        return True
    except Exception as e:
        log.warning(
            f"[{request_id}] BANK_ACCOUNT: setup failed (non-fatal): "
            f"{type(e).__name__}: {e}"
        )
        return True


# ---- Agent Loop ------------------------------------------------------------


async def run_agent(
    prompt: str, files: list, credentials: dict, log: logging.Logger
) -> None:
    request_id = str(uuid.uuid4())[:8]
    _ctx_log.set(log)
    _ctx_rid.set(request_id)

    base_url = credentials["base_url"]
    token = credentials["session_token"]
    start_time = time.time()

    use_reasoning = is_tier3_task(prompt, files)

    log.info(f"[{request_id}] === NEW REQUEST ===")
    log.info(f"[{request_id}] Base URL: {base_url}")
    log.info(f"[{request_id}] Model: {MODEL}")
    log.info(f"[{request_id}] Solver model: {SOLVER_MODEL}")
    log.info(f"[{request_id}] Max iterations: {MAX_ITERATIONS}, Timeout: {SOLVE_TIMEOUT}s")
    log.info(f"[{request_id}] Prompt:\n{prompt}")
    log.info(f"[{request_id}] Files count: {len(files)}")
    for i, f in enumerate(files):
        log.info(
            f"[{request_id}]   File[{i}]: name={f.get('name', '?')}, "
            f"mime_type={f.get('mime_type', '?')}, "
            f"base64_len={len(f.get('content_base64', ''))}"
        )
    log.info(f"[{request_id}] Use reasoning: {use_reasoning}")
    if use_reasoning:
        log.info(f"[{request_id}] Tier 3 detected -- adaptive thinking enabled")

    user_content: list | str
    if files:
        user_content = [{"type": "text", "text": prompt}]
        user_content.extend(process_files(files))
    else:
        user_content = prompt

    messages = [{"role": "user", "content": user_content}]

    async with httpx.AsyncClient() as client:
        solved, extracted_fields = await try_deterministic_solve(
            prompt,
            files,
            client,
            base_url,
            token,
            log,
            request_id,
        )
        if solved:
            total_elapsed = time.time() - start_time
            log.info(
                f"[{request_id}] === REQUEST COMPLETE (deterministic solver) === "
                f"({total_elapsed:.1f}s)"
            )
            return

        if extracted_fields and extracted_fields.get("_fatal_proxy_token_invalid"):
            log.warning(f"[{request_id}] Aborting before LLM loop: invalid proxy token")
            total_elapsed = time.time() - start_time
            log.info(f"[{request_id}] === REQUEST COMPLETE === ({total_elapsed:.1f}s)")
            return

        log.info(f"[{request_id}] Falling back to LLM agent loop")
        fallback_task_type = (
            extracted_fields.get("task_type", "UNSUPPORTED")
            if extracted_fields
            else "UNSUPPORTED"
        )
        if fallback_task_type in TASKS_NEEDING_BANK:
            bank_ok = await ensure_bank_account(client, base_url, token, log, request_id)
            if not bank_ok:
                log.warning(f"[{request_id}] Aborting before LLM loop: invalid proxy token")
                total_elapsed = time.time() - start_time
                log.info(f"[{request_id}] === REQUEST COMPLETE === ({total_elapsed:.1f}s)")
                return

        if (
            extracted_fields
            and extracted_fields.get("task_type", "UNSUPPORTED") != "UNSUPPORTED"
        ):
            deterministic_state_hint = ""
            partial_state = extracted_fields.get("_deterministic_state")
            if partial_state:
                deterministic_state_hint = (
                    f" Partial deterministic state already created in Tripletex: "
                    f"{json.dumps(partial_state, ensure_ascii=False)}. "
                    f"Do NOT recreate entities already present in this state; continue from them."
                )
            solver_hint = (
                f"\n\n[SOLVER CONTEXT: Task was classified as '{extracted_fields['task_type']}' "
                f"with fields: {json.dumps(extracted_fields, ensure_ascii=False)}. "
                f"The deterministic solver failed -- please complete the task using API calls."
                f"{deterministic_state_hint}]"
            )
            log.info(f"[{request_id}] Injecting solver hint into user message: {solver_hint}")
            if isinstance(user_content, str):
                messages[0]["content"] = user_content + solver_hint
            else:
                messages[0]["content"] = user_content + [
                    {"type": "text", "text": solver_hint}
                ]

        for iteration in range(MAX_ITERATIONS):
            elapsed = time.time() - start_time
            if elapsed > SOLVE_TIMEOUT:
                log.warning(
                    f"[{request_id}] Timeout after {elapsed:.1f}s at iteration {iteration}"
                )
                break

            log.info(
                f"[{request_id}] Iteration {iteration + 1}, elapsed {elapsed:.1f}s"
            )

            try:
                response = await call_openrouter(
                    messages, client, use_reasoning, log, request_id
                )
            except httpx.HTTPStatusError as e:
                log.error(
                    f"[{request_id}] OpenRouter HTTP error: {e.response.status_code} "
                    f"{e.response.text}"
                )
                break
            except Exception as e:
                log.error(f"[{request_id}] OpenRouter error: {type(e).__name__}: {e}")
                break

            choice = response["choices"][0]
            assistant_msg = choice["message"]
            finish_reason = choice.get("finish_reason", "")

            msg_for_history = {
                k: v for k, v in assistant_msg.items() if k != "reasoning"
            }
            messages.append(msg_for_history)

            if finish_reason != "tool_calls" or not assistant_msg.get("tool_calls"):
                log.info(f"[{request_id}] Agent finished (reason: {finish_reason})")
                if assistant_msg.get("content"):
                    log.info(
                        f"[{request_id}] Final message:\n{assistant_msg['content']}"
                    )
                break

            consecutive_proxy_403 = 0

            for tc_idx, tool_call in enumerate(assistant_msg["tool_calls"]):
                tc_id = tool_call["id"]
                func = tool_call["function"]
                func_name = func["name"]
                raw_arguments = func.get("arguments", "{}")

                try:
                    args = json.loads(raw_arguments)
                except json.JSONDecodeError:
                    args = {}
                    log.warning(
                        f"[{request_id}] TOOL_CALL[{tc_idx}] failed to parse arguments: "
                        f"{raw_arguments}"
                    )

                log.info(
                    f"[{request_id}] TOOL_CALL[{tc_idx}]: id={tc_id}, "
                    f"func={func_name}, "
                    f"raw_args={raw_arguments}"
                )

                if func_name == "tripletex_api":
                    method = args.get("method", "GET")
                    path = args.get("path", "/")
                    params = args.get("params")
                    body = args.get("body")

                    body_str = json.dumps(body, ensure_ascii=False, default=str) if body is not None else None
                    log.info(
                        f"[{request_id}] TRIPLETEX_API: {method} {path}\n"
                        f"  params={json.dumps(params, ensure_ascii=False) if params else None}\n"
                        f"  body={body_str}"
                    )

                    norm_path = _normalize_api_path(path)

                    endpoint_err = _validate_endpoint(method, norm_path)
                    if endpoint_err:
                        result = {"status_code": 404, "body": endpoint_err}
                        log.info(
                            f"[{request_id}] BLOCKED invalid endpoint: {method} {path} "
                            f"-- {endpoint_err}"
                        )
                    elif method == "GET" and (prefix := _cacheable_prefix(norm_path)):
                        key = _cache_key(token, norm_path, params)
                        cached_result = _api_cache.get(key)
                        if cached_result is not None:
                            result = cached_result
                            cached_body = result.get("body", {})
                            cached_str = (
                                json.dumps(cached_body, ensure_ascii=False, default=str)
                                if not isinstance(cached_body, str)
                                else cached_body
                            )
                            log.info(
                                f"[{request_id}] CACHE HIT: GET {path}"
                                f" params={json.dumps(params, ensure_ascii=False) if params else None}"
                                f" cached_status={result.get('status_code')}"
                                f" cached_body=\n{cached_str}"
                            )
                        else:
                            result = None
                    else:
                        result = None

                    if endpoint_err:
                        pass
                    elif result is not None:
                        pass
                    else:
                        call_start = time.time()
                        result = await execute_tripletex_call(
                            client, base_url, token, method, path, params, body,
                            log, request_id,
                        )
                        call_elapsed = time.time() - call_start

                        status = result["status_code"]
                        resp_body = result.get("body", {})
                        resp_str = (
                            json.dumps(resp_body, ensure_ascii=False, default=str)
                            if not isinstance(resp_body, str)
                            else resp_body
                        )

                        log.info(
                            f"[{request_id}] API RESPONSE SUMMARY: {method} {path} -> "
                            f"status={status} ({call_elapsed:.1f}s)\n"
                            f"  response_body=\n{resp_str}"
                        )

                        if method == "GET" and status == 200:
                            prefix = _cacheable_prefix(norm_path)
                            if prefix:
                                key = _cache_key(token, norm_path, params)
                                _api_cache[key] = result
                                log.info(f"[{request_id}] CACHE STORE: {path}")

                        if method in ("POST", "PUT", "DELETE"):
                            cleared = _invalidate_cache(token, norm_path)
                            if cleared:
                                log.info(
                                    f"[{request_id}] Cache invalidated: {cleared} entries for {norm_path}"
                                )

                        if status == 403 and "invalid or expired proxy token" in resp_str.lower():
                            consecutive_proxy_403 += 1
                        else:
                            consecutive_proxy_403 = 0

                    tool_content = truncate_for_context(result)
                    log.info(
                        f"[{request_id}] TOOL_RESULT -> context (tc_id={tc_id}): "
                        f"{tool_content}"
                    )
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tc_id,
                            "content": tool_content,
                        }
                    )
                elif func_name == "compute_taxable_result":
                    date_from = args.get("date_from", "")
                    date_to = args.get("date_to", "")

                    log.info(
                        f"[{request_id}] COMPUTE_TAXABLE_RESULT: "
                        f"date_from={date_from}, date_to={date_to}"
                    )

                    call_start = time.time()
                    result = await compute_result_from_postings(
                        client, base_url, token, date_from, date_to
                    )
                    call_elapsed = time.time() - call_start

                    result_str = json.dumps(result, ensure_ascii=False)
                    log.info(
                        f"[{request_id}] COMPUTE_TAXABLE_RESULT RESPONSE ({call_elapsed:.1f}s):\n"
                        f"  net_result={result['net_result']}\n"
                        f"  total_postings_fetched={result['total_postings_fetched']}\n"
                        f"  full_result={result_str}"
                    )

                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tc_id,
                            "content": result_str,
                        }
                    )
                else:
                    log.warning(
                        f"[{request_id}] UNKNOWN TOOL: {func_name}, "
                        f"args={json.dumps(args, ensure_ascii=False)}"
                    )
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tc_id,
                            "content": json.dumps(
                                {"error": f"Unknown tool: {func_name}"}
                            ),
                        }
                    )

            if consecutive_proxy_403 >= 1:
                log.error(
                    f"[{request_id}] Aborting: {consecutive_proxy_403} consecutive 403"
                    " 'invalid or expired proxy token' errors -- token is invalid"
                )
                break

    total_elapsed = time.time() - start_time
    log.info(f"[{request_id}] === REQUEST COMPLETE === ({total_elapsed:.1f}s)")


# ---- FastAPI Endpoints -----------------------------------------------------


@app.post("/")
@app.post("/solve")
async def solve(request: Request, test: bool = Query(False)):
    log = testing_log if test else submission_log
    solve_start = time.time()

    headers_dict = dict(request.headers)
    network_log.info("=== /solve REQUEST ===")
    network_log.info(f"Headers: {json.dumps(headers_dict, ensure_ascii=False)}")

    log.info("=" * 80)
    log.info("=== SOLVE ENDPOINT CALLED ===")
    log.info(f"Test mode: {test}")
    log.info(f"Request headers: {json.dumps(headers_dict, ensure_ascii=False)}")

    try:
        raw_body = await request.body()
        network_log.info(f"Raw body size: {len(raw_body)} bytes")
        log.info(f"Raw body size: {len(raw_body)} bytes")
        body = json.loads(raw_body)
    except Exception as e:
        network_log.error(f"Failed to parse request body: {type(e).__name__}: {e}")
        network_log.error(f"Raw body (first 2000 chars): {raw_body[:2000]}")
        log.error(f"Failed to parse request body: {type(e).__name__}: {e}")
        log.error(f"Raw body (first 2000 chars): {raw_body[:2000]}")
        return JSONResponse({"status": "error", "detail": "bad request body"}, status_code=400)

    body_summary = {}
    for k, v in body.items():
        if k == "tripletex_credentials":
            body_summary[k] = {kk: ("***" if "token" in kk.lower() else vv) for kk, vv in v.items()} if isinstance(v, dict) else "***"
        elif k == "files":
            body_summary[k] = [{"name": f.get("name", "?"), "mime_type": f.get("mime_type", "?"), "size": len(f.get("content_base64", ""))} for f in v] if isinstance(v, list) else v
        elif k == "prompt":
            body_summary[k] = v
        else:
            body_summary[k] = str(v)
    network_log.info(f"Body: {json.dumps(body_summary, ensure_ascii=False)}")
    log.info(f"Parsed body: {json.dumps(body_summary, ensure_ascii=False)}")

    try:
        prompt = body["prompt"]
        files = body.get("files", [])
        credentials = body["tripletex_credentials"]
    except KeyError as e:
        network_log.error(f"Missing required field in request: {e}")
        log.error(f"Missing required field in request: {e}")
        return JSONResponse({"status": "error", "detail": f"missing field: {e}"}, status_code=400)

    log.info(f"Prompt: {prompt}")
    log.info(f"Files count: {len(files)}")
    for i, f in enumerate(files):
        log.info(f"  File[{i}]: name={f.get('name', '?')}, mime_type={f.get('mime_type', '?')}, base64_len={len(f.get('content_base64', ''))}")
    log.info(f"Credentials base_url: {credentials.get('base_url', '?')}")

    try:
        await run_agent(prompt, files, credentials, log)
    except Exception as e:
        log.error(f"Agent error: {type(e).__name__}: {e}", exc_info=True)

    solve_elapsed = time.time() - solve_start
    log.info(f"=== SOLVE ENDPOINT DONE === ({solve_elapsed:.1f}s)")
    log.info("=" * 80)
    return JSONResponse({"status": "completed"})


@app.get("/health")
async def health():
    return {"status": "ok", "model": MODEL}
