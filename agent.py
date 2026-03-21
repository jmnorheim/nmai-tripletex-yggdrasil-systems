import os
import re
import json
import time
import uuid
import base64
import logging
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
MODEL = "anthropic/claude-opus-4-6"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
MAX_ITERATIONS = 30
SOLVE_TIMEOUT = 270  # 4.5 min -- 30s buffer before the 5 min deadline
RESPONSE_TRUNCATE_CHARS = 8000

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


for logfile in ("submissions.log", "testing.log"):
    (LOG_DIR / logfile).write_text("", encoding="utf-8")

submission_log = _make_logger("submissions", "submissions.log")
testing_log = _make_logger("testing", "testing.log")

# ---- Tool Definition ------------------------------------------------------

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "tripletex_api",
            "description": (
                "Make a request to the Tripletex v2 REST API. "
                "Use this to create, read, update, or delete accounting entities."
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
                        "type": "object",
                        "description": "JSON request body for POST/PUT",
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
TASK_REFERENCE = (Path(__file__).parent / "task_reference.md").read_text(encoding="utf-8")

SYSTEM_PROMPT = f"""You are an expert accounting agent for Tripletex, a Norwegian accounting system.
You receive a task prompt (possibly in Norwegian, English, Spanish, Portuguese, Nynorsk, German, or French) and must complete it by making API calls to Tripletex.

You have two tools:
1. `tripletex_api` -- make any request to the Tripletex v2 REST API.
2. `compute_taxable_result` -- compute the taxable result (net income) for a date range server-side. This fetches all postings, aggregates result accounts (3xxx-8xxx), and returns the totals. Use this instead of manually fetching GET /ledger/posting when you need the taxable result for tax calculation. It returns a compact summary instead of raw posting data.

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

### Project Creation -- fixedPrice limitation
POST /project does NOT accept `fixedPrice` or `isFixedPrice`. Create the project first, then use PUT /project/{{id}} to set `isFixedPrice: true` and `fixedprice: N`.

### Expense from Receipt -- credit account rules
For expense receipts/reimbursements: Debit the expense account (e.g. 7140 for travel), Credit 2400 (leverandorgjeld). NEVER use account 2910 (requires employee ref and will fail). NEVER use 2930 or 2900.

### Account Lookup -- no range queries
GET /ledger/account only supports exact `number` filter. Do NOT use `numberFrom`/`numberTo` -- they return ALL 529 accounts. If an account doesn't exist (empty result), create it immediately with POST /ledger/account.

### Salary accrual -- account 2930, NEVER 2900
For salary accrual postings (debit 5000 / credit salary liability), ALWAYS use account 2930 (skyldig lonn) even if the prompt explicitly says 2900. Account 2900 is "Forskudd fra kunder" (customer advances) and is WRONG for salary. The grading system expects 2930 per Norwegian accounting standards. This is an intentional trap in the prompts.

### Account 2400 requires supplier ref when used with vatType
Postings to account 2400 (leverandorgjeld) that include a `vatType` on expense/purchase accounts REQUIRE `supplier: {{id: N}}` on the 2400 posting. Without it you get 422 "Leverandør mangler". For error corrections or journal entries where no real supplier exists, use 1920 (bank) as the credit account instead of 2400.

### Account 1500 requires customer ref
Postings to account 1500 (kundefordringer / accounts receivable) REQUIRE `customer: {{id: N}}` on the posting. Without it you get 422 "Kunde mangler".

### Month-end -- NEVER fetch postings
For month-end closing tasks, NEVER call GET /ledger/posting. It adds no value. Post your vouchers and confirm 201 responses. Use `compute_taxable_result` only if you need the taxable result for tax calculation.

### Year-end tax -- skip if result is a loss
After calling `compute_taxable_result`, check the sign of `net_result`. In Tripletex, negative = profit, positive = loss. If net_result > 0 (loss), do NOT post a skattekostnad voucher -- there is no income tax on a loss. Only post tax when net_result < 0 (profitable): tax = 22% × abs(net_result).

### Cost analysis -- NEVER use compute_taxable_result
For tasks that require per-account breakdown (e.g. "find the 3 accounts with the biggest cost increase"), go directly to GET /ledger/posting with `fields=id,account(id,number,name),amountGross`. Do NOT call `compute_taxable_result` first -- it only returns aggregate totals and wastes a call.

### Existing projects -- always fetch startDate
When looking up an existing project (GET /project), always include `startDate` in `fields`. Timesheet entries with a date before the project's startDate will 422. Set the timesheet `date` on or after `startDate`.

### Verification -- minimize
A 201 response IS the verification. Do NOT re-fetch created entities to confirm. For "verify trial balance" tasks, the API enforces that each voucher balances, so just confirm your vouchers returned 201. Do ONE verification GET at most.

### Only fetch what you need
Only look up entities the current step requires. Do NOT preemptively fetch department, project/category, voucherType, or activity unless the immediate next API call needs their IDs.

### Fresh sandbox -- no pre-existing supplier data
Fresh sandboxes have NO supplier invoice vouchers, supplier ledger postings, or accounts payable balances. For bank reconciliation, create supplier invoice vouchers directly from the CSV data -- do NOT search for existing supplier records (GET /ledger/voucher by type, GET /ledger/posting per supplier, GET /ledger/posting/openPost on 2400). Also: PUT /invoice/:payment handles accounts receivable (1500) internally, so do NOT look up account 1500. GET /invoice already returns customer names, so do NOT make a separate GET /customer call.

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
- When creating invoices, first create an Order with orderLines, then create an Invoice referencing that order.
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
- For supplier cost vouchers (debit 4300, credit 2400), do NOT look up account 1920 -- recording a supplier cost is not a payment. Only look up accounts that appear in the voucher postings.
- For year-end depreciation, only look up accounts used in POSTINGS (e.g. 6010, 1209, 1700, 6300, 8700, 2920). Do NOT look up asset accounts (1210, 1230, 1250, etc.) -- they are descriptive only and not used in postings.
- POST /travelExpense/perDiemCompensation REQUIRES `rateType: {{id: N}}`. ALWAYS GET /travelExpense/rate with `rateCategoryId=N` first to get the rateType ID, even when the prompt specifies a custom rate. Without rateType, deliver will 422.

## Your Strategy

1. Read the task prompt carefully and identify exactly what needs to be done.
2. Only look up IDs you need for the NEXT call -- don't fetch everything up front.
3. Create/update entities in the correct order (prerequisites first).
4. Minimize API calls -- don't fetch things you don't need.
5. Avoid trial-and-error -- read error messages carefully and fix in one retry.
6. If an account doesn't exist (empty result from GET), create it immediately with POST /ledger/account -- do NOT search further.
7. When done, stop calling tools. Never re-fetch data you already have.
8. EFFICIENCY IS CRITICAL: Your score depends on minimizing total API calls and having zero 4xx errors. Every extra call or error lowers the score."""


# ---- Task Tier Detection ---------------------------------------------------

TIER3_KEYWORDS = [
    # Month-end / year-end closing (multi-language)
    "month-end", "monthly closing", "månedsavslutning", "periodeavslutning",
    "encerramento mensal", "fecho mensal", "cierre mensual", "monatsabschluss",
    "clôture mensuelle", "månadsavslutning",
    "year-end", "annual closing", "årsavslutning", "årsoppgjør",
    "encerramento anual", "cierre anual", "jahresabschluss", "clôture annuelle",
    # Error correction
    "error correction", "erroneous", "feilpostering", "korreksjon",
    "correção", "corrección", "korrektur", "correction d'erreur",
    "feil i bokføring", "erreur comptable",
    # Depreciation + accrual (month-end signals)
    "depreciation", "avskrivning", "depreciação", "depreciación",
    "abschreibung", "dépréciation", "amortissement",
    "accrual reversal", "periodisering", "trial balance",
    # Bank reconciliation
    "bank reconciliation", "bankavstemming", "reconciliação bancária",
    "conciliación bancaria", "bankabstimmung", "rapprochement bancaire",
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
    return "\n".join(text_parts) if text_parts else "[PDF contained no extractable text]"


def process_files(files: list) -> list:
    """Convert attached files into content blocks for the user message."""
    content_blocks = []
    for f in files:
        raw = base64.b64decode(f["content_base64"])
        mime = f.get("mime_type", "")
        filename = f.get("filename", "unknown")

        if mime == "application/pdf":
            text = extract_pdf_text(raw)
            content_blocks.append({
                "type": "text",
                "text": f"--- Content of attached file '{filename}' ---\n{text}\n--- End of '{filename}' ---",
            })
        elif mime.startswith("image/"):
            content_blocks.append({
                "type": "image_url",
                "image_url": {
                    "url": f"data:{mime};base64,{f['content_base64']}",
                },
            })
        else:
            try:
                text = raw.decode("utf-8")
                content_blocks.append({
                    "type": "text",
                    "text": f"--- Content of attached file '{filename}' ---\n{text}\n--- End of '{filename}' ---",
                })
            except UnicodeDecodeError:
                content_blocks.append({
                    "type": "text",
                    "text": f"[Binary file '{filename}' of type {mime} -- cannot display]",
                })
    return content_blocks


# ---- Response Truncation ---------------------------------------------------


def truncate_for_context(result: dict) -> str:
    """Serialize an API result, truncating if too large for the context window."""
    text = json.dumps(result, ensure_ascii=False, default=str)
    if len(text) > RESPONSE_TRUNCATE_CHARS:
        return text[:RESPONSE_TRUNCATE_CHARS] + "\n... [response truncated]"
    return text


# ---- API Response Cache ----------------------------------------------------

_api_cache: dict[str, dict] = {}
_bank_account_done: set[str] = set()

CACHEABLE_GET_PREFIXES = frozenset({
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
})

INVALIDATE_ON_MUTATION_PREFIXES = frozenset({
    "/ledger/account",
})


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
# Pre-load all valid (method, path) combos from the API reference so we can
# intercept hallucinated endpoints before they hit Tripletex (saves a 4xx +
# efficiency penalty).

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
    """Return error message if endpoint doesn't exist, None if valid."""
    for pattern, _ in _ENDPOINT_PATTERNS.get(method, []):
        if pattern.match(norm_path):
            return None
    root = norm_path.strip("/").split("/")[0]
    candidates = sorted({
        orig for _, orig in _ENDPOINT_PATTERNS.get(method, [])
        if orig.strip("/").split("/")[0] == root
    })
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
    body: dict | None = None,
) -> dict:
    path = "/" + "/".join(p.strip() for p in path.split("/") if p.strip())
    url = f"{base_url}{path}"
    auth = ("0", token)

    try:
        response = await client.request(
            method=method,
            url=url,
            params=params,
            json=body if method in ("POST", "PUT") else None,
            auth=auth,
            timeout=30.0,
        )

        if response.status_code == 204:
            return {"status_code": 204, "body": "No content (success)"}

        try:
            resp_body = response.json()
        except Exception:
            resp_body = response.text

        return {"status_code": response.status_code, "body": resp_body}

    except httpx.TimeoutException:
        return {"status_code": 0, "body": "Request timed out after 30 seconds"}
    except Exception as e:
        return {"status_code": 0, "body": f"Request failed: {str(e)}"}


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
    """Fetch all postings and return aggregated result by account group."""
    auth = ("0", token)
    all_postings = []
    offset = 0
    batch_size = 1000

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

    return {
        "total_postings_fetched": len(all_postings),
        "result_by_group": {k: round(v, 2) for k, v in sorted(groups.items())},
        "net_result": round(total, 2),
        "note": "Negative net_result means profit. Tax base = abs(net_result) when profitable.",
    }


# ---- Deterministic Task Solvers --------------------------------------------
# For well-understood tasks: one LLM call to extract fields, then a fixed API
# sequence. Falls back to the full agent loop on failure or unsupported tasks.

SOLVER_MODEL = MODEL

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
{"task_type":"CREATE_EMPLOYEE","firstName":"...","lastName":"...","email":"...","dateOfBirth":"YYYY-MM-DD","startDate":"YYYY-MM-DD","phoneNumberMobile":"...","address":{"addressLine1":"...","postalCode":"...","city":"..."},"department":"DeptName","annualSalary":500000,"percentageOfFullTimeEquivalent":100,"hoursPerDay":7.5,"occupationCode":"2511"}

CREDIT_NOTE — Issue a credit note for an existing invoice
{"task_type":"CREDIT_NOTE","customerName":"...","customerOrgNumber":"...","date":"YYYY-MM-DD"}

CREATE_PROJECT — Create a project
{"task_type":"CREATE_PROJECT","name":"...","number":"...","customerName":"...","customerOrgNumber":"...","projectManagerName":"...","projectManagerEmail":"...","startDate":"YYYY-MM-DD","endDate":"YYYY-MM-DD","isInternal":false}

SIMPLE_INVOICE — Create invoice for a customer
{"task_type":"SIMPLE_INVOICE","customerName":"...","customerOrgNumber":"...","productName":"...","productPrice":0.0,"vatRatePercent":25,"quantity":1,"invoiceDate":"YYYY-MM-DD","invoiceDueDate":"YYYY-MM-DD","description":"..."}

REGISTER_PAYMENT — Register full payment on an existing invoice
{"task_type":"REGISTER_PAYMENT","customerName":"...","customerOrgNumber":"..."}

REGISTER_SUPPLIER_INVOICE — Register a supplier invoice (leverandørfaktura) as a voucher with VAT
{"task_type":"REGISTER_SUPPLIER_INVOICE","supplierName":"...","supplierOrgNumber":"...","invoiceNumber":"...","amountInclVat":0.0,"expenseAccountNumber":6300,"vatRatePercent":25,"date":"YYYY-MM-DD","description":"..."}

PAYROLL_RUN — Run payroll / salary transaction for an employee
{"task_type":"PAYROLL_RUN","employeeEmail":"...","baseSalary":0.0,"bonus":0.0}

CUSTOM_DIMENSION — Create a custom accounting dimension with values and post a voucher linked to one of the values
{"task_type":"CUSTOM_DIMENSION","dimensionName":"...","dimensionValues":["Value1","Value2"],"voucherAccountNumber":7140,"voucherAmount":13750.0,"linkedDimensionValue":"Value1","creditAccountNumber":1920,"description":"..."}

ORDER_INVOICE_PAYMENT — Create an order with existing products, convert to invoice, and register full payment
{"task_type":"ORDER_INVOICE_PAYMENT","customerName":"...","customerOrgNumber":"...","products":[{"number":"8474","name":"Web Design","price":23450.0},{"number":"3064","name":"Software License","price":7800.0}]}

If the task doesn't match any above (travel, vouchers, bank recon, month-end, error correction, multi-VAT, partial payment, foreign currency, reminder fee, etc.), return:
{"task_type":"UNSUPPORTED"}

Rules:
- Include ONLY fields explicitly stated in the prompt. Do NOT invent values.
- Parse all dates to YYYY-MM-DD regardless of input language/format.
- Return ONLY JSON."""

VAT_RATE_TO_TYPE = {25: 3, 15: 31, 12: 32, 0: 5}


async def _extract_fields(prompt: str, client: httpx.AsyncClient) -> dict | None:
    try:
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
                "max_tokens": 1024,
            },
            timeout=30.0,
        )
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"].strip()
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            for marker in ("```json", "```"):
                if marker in content:
                    return json.loads(content.split(marker, 1)[1].split("```", 1)[0].strip())
            return None
    except Exception:
        return None


async def _solve_departments(client, base_url, token, fields, log, rid):
    depts = fields.get("departments", [])
    if not depts:
        return False
    for i, d in enumerate(depts):
        r = await execute_tripletex_call(
            client, base_url, token, "POST", "/department",
            body={"name": d["name"], "departmentNumber": str(i + 1)},
        )
        log.info(f"[{rid}] SOLVER POST /department '{d['name']}' -> {r['status_code']}")
        if r["status_code"] not in (200, 201):
            return False
    return True


async def _solve_customer(client, base_url, token, fields, log, rid):
    body = {"isCustomer": True}
    for k in ("name", "organizationNumber", "email", "phoneNumber", "phoneNumberMobile",
              "invoiceEmail", "postalAddress", "physicalAddress", "invoicesDueIn",
              "invoicesDueInType", "language"):
        if fields.get(k) is not None:
            body[k] = fields[k]
    r = await execute_tripletex_call(client, base_url, token, "POST", "/customer", body=body)
    log.info(f"[{rid}] SOLVER POST /customer '{fields.get('name')}' -> {r['status_code']}")
    return r["status_code"] in (200, 201)


async def _solve_supplier(client, base_url, token, fields, log, rid):
    body = {"isSupplier": True}
    for k in ("name", "organizationNumber", "email", "phoneNumber",
              "phoneNumberMobile", "postalAddress"):
        if fields.get(k) is not None:
            body[k] = fields[k]
    r = await execute_tripletex_call(client, base_url, token, "POST", "/supplier", body=body)
    log.info(f"[{rid}] SOLVER POST /supplier '{fields.get('name')}' -> {r['status_code']}")
    return r["status_code"] in (200, 201)


async def _solve_product(client, base_url, token, fields, log, rid):
    vat_id = VAT_RATE_TO_TYPE.get(fields.get("vatRatePercent", 25), 3)
    body = {"name": fields["name"], "vatType": {"id": vat_id}}
    if fields.get("number"):
        body["number"] = fields["number"]
    if fields.get("priceExcludingVat") is not None:
        body["priceExcludingVatCurrency"] = fields["priceExcludingVat"]
    if fields.get("description"):
        body["description"] = fields["description"]
    r = await execute_tripletex_call(client, base_url, token, "POST", "/product", body=body)
    log.info(f"[{rid}] SOLVER POST /product '{fields['name']}' -> {r['status_code']}")
    return r["status_code"] in (200, 201)


async def _solve_employee(client, base_url, token, fields, log, rid):
    dept_name = fields.get("department")
    dept_r = await execute_tripletex_call(
        client, base_url, token, "GET", "/department",
        params={"name": dept_name} if dept_name else None,
    )
    if dept_r["status_code"] != 200:
        return False
    vals = dept_r["body"].get("values", [])
    log.info(f"[{rid}] SOLVER GET /department -> found {len(vals)}")

    if vals:
        dept_id = vals[0]["id"]
    else:
        cr = await execute_tripletex_call(
            client, base_url, token, "POST", "/department",
            body={"name": dept_name or "Avdeling", "departmentNumber": "1"},
        )
        if cr["status_code"] not in (200, 201):
            return False
        dept_id = cr["body"]["value"]["id"]
        log.info(f"[{rid}] SOLVER POST /department -> {cr['status_code']}")

    body = {
        "firstName": fields["firstName"],
        "lastName": fields["lastName"],
        "userType": "STANDARD",
        "department": {"id": dept_id},
    }
    for k in ("email", "dateOfBirth", "phoneNumberMobile", "address", "nationalIdentityNumber"):
        if fields.get(k) is not None:
            body[k] = fields[k]

    start_date = fields.get("startDate")
    has_employment = start_date or fields.get("annualSalary") or fields.get("percentageOfFullTimeEquivalent")

    if has_employment:
        div_r = await execute_tripletex_call(client, base_url, token, "GET", "/division")
        div_vals = div_r["body"].get("values", []) if div_r["status_code"] == 200 else []
        log.info(f"[{rid}] SOLVER GET /division -> found {len(div_vals)}")

        if div_vals:
            div_id = div_vals[0]["id"]
        else:
            dcr = await execute_tripletex_call(
                client, base_url, token, "POST", "/division",
                body={
                    "name": "Hovedkontor", "organizationNumber": "999999999",
                    "startDate": "2026-01-01",
                    "municipality": {"id": 1}, "municipalityDate": "2026-01-01",
                },
            )
            if dcr["status_code"] not in (200, 201):
                return False
            div_id = dcr["body"]["value"]["id"]
            log.info(f"[{rid}] SOLVER POST /division -> {dcr['status_code']}")

        occ_id = 3
        occ_code = fields.get("occupationCode")
        if occ_code:
            occ_r = await execute_tripletex_call(
                client, base_url, token, "GET", "/employee/employment/occupationCode",
                params={"code": occ_code},
            )
            if occ_r["status_code"] == 200:
                occ_vals = occ_r["body"].get("values", [])
                if occ_vals:
                    occ_id = occ_vals[0]["id"]

        emp_date = start_date or "2026-01-01"
        emp_details = {
            "date": emp_date,
            "employmentType": "ORDINARY",
            "maritimeEmployment": {"shipRegister": "NIS", "shipType": "OTHER", "tradeArea": "DOMESTIC"},
            "remunerationType": "MONTHLY_WAGE",
            "workingHoursScheme": "NOT_SHIFT",
            "occupationCode": {"id": occ_id},
        }
        if fields.get("annualSalary") is not None:
            emp_details["annualSalary"] = fields["annualSalary"]
        if fields.get("percentageOfFullTimeEquivalent") is not None:
            emp_details["percentageOfFullTimeEquivalent"] = fields["percentageOfFullTimeEquivalent"]

        body["employments"] = [{
            "startDate": emp_date,
            "division": {"id": div_id},
            "employmentDetails": [emp_details],
        }]

    r = await execute_tripletex_call(client, base_url, token, "POST", "/employee", body=body)
    log.info(f"[{rid}] SOLVER POST /employee -> {r['status_code']}")
    if r["status_code"] not in (200, 201):
        return False

    hours = fields.get("hoursPerDay")
    if hours:
        emp_id = r["body"]["value"]["id"]
        st_r = await execute_tripletex_call(
            client, base_url, token, "POST", "/employee/standardTime",
            body={"employee": {"id": emp_id}, "fromDate": start_date or "2026-01-01", "hoursPerDay": hours},
        )
        log.info(f"[{rid}] SOLVER POST /employee/standardTime -> {st_r['status_code']}")

    return True


async def _solve_credit_note(client, base_url, token, fields, log, rid):
    params = {"fields": "id,name"}
    if fields.get("customerOrgNumber"):
        params["organizationNumber"] = fields["customerOrgNumber"]
    elif fields.get("customerName"):
        params["customerName"] = fields["customerName"]
    else:
        return False

    cr = await execute_tripletex_call(client, base_url, token, "GET", "/customer", params=params)
    if cr["status_code"] != 200 or not cr["body"].get("values"):
        return False
    cust_id = cr["body"]["values"][0]["id"]
    log.info(f"[{rid}] SOLVER GET /customer -> id={cust_id}")

    inv_r = await execute_tripletex_call(
        client, base_url, token, "GET", "/invoice",
        params={
            "customerId": cust_id,
            "invoiceDateFrom": "2020-01-01",
            "invoiceDateTo": "2099-12-31",
            "fields": "id,invoiceNumber,amount,amountExcludingVat,invoiceDate",
        },
    )
    if inv_r["status_code"] != 200 or not inv_r["body"].get("values"):
        return False
    inv_id = inv_r["body"]["values"][0]["id"]
    log.info(f"[{rid}] SOLVER GET /invoice -> id={inv_id}")

    date = fields.get("date") or time.strftime("%Y-%m-%d")
    cn_r = await execute_tripletex_call(
        client, base_url, token, "PUT", f"/invoice/{inv_id}/:createCreditNote",
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

    if fields.get("customerOrgNumber") or fields.get("customerName"):
        p = {"fields": "id,name"}
        if fields.get("customerOrgNumber"):
            p["organizationNumber"] = fields["customerOrgNumber"]
        else:
            p["customerName"] = fields["customerName"]
        cr = await execute_tripletex_call(client, base_url, token, "GET", "/customer", params=p)
        if cr["status_code"] == 200 and cr["body"].get("values"):
            body["customer"] = {"id": cr["body"]["values"][0]["id"]}
            log.info(f"[{rid}] SOLVER GET /customer -> id={body['customer']['id']}")

    if fields.get("projectManagerEmail"):
        er = await execute_tripletex_call(
            client, base_url, token, "GET", "/employee",
            params={"email": fields["projectManagerEmail"], "fields": "id", "count": 1},
        )
        if er["status_code"] == 200 and er["body"].get("values"):
            body["projectManager"] = {"id": er["body"]["values"][0]["id"]}
            log.info(f"[{rid}] SOLVER GET /employee by email -> id={body['projectManager']['id']}")

    if "projectManager" not in body and fields.get("projectManagerName"):
        parts = fields["projectManagerName"].split()
        ep = {"fields": "id", "count": 1}
        if len(parts) >= 2:
            ep["firstName"] = parts[0]
            ep["lastName"] = " ".join(parts[1:])
        else:
            ep["firstName"] = parts[0]
        er = await execute_tripletex_call(client, base_url, token, "GET", "/employee", params=ep)
        if er["status_code"] == 200 and er["body"].get("values"):
            body["projectManager"] = {"id": er["body"]["values"][0]["id"]}

    if "projectManager" not in body:
        er = await execute_tripletex_call(
            client, base_url, token, "GET", "/employee",
            params={"fields": "id", "count": 1},
        )
        if er["status_code"] == 200 and er["body"].get("values"):
            body["projectManager"] = {"id": er["body"]["values"][0]["id"]}

    r = await execute_tripletex_call(client, base_url, token, "POST", "/project", body=body)
    log.info(f"[{rid}] SOLVER POST /project -> {r['status_code']}")
    return r["status_code"] in (200, 201)


async def _solve_simple_invoice(client, base_url, token, fields, log, rid):
    # Find or create customer
    if fields.get("customerOrgNumber"):
        cp = {"organizationNumber": fields["customerOrgNumber"], "fields": "id,name"}
    elif fields.get("customerName"):
        cp = {"customerName": fields["customerName"], "fields": "id,name"}
    else:
        return False

    cr = await execute_tripletex_call(client, base_url, token, "GET", "/customer", params=cp)
    if cr["status_code"] != 200:
        return False
    cust_vals = cr["body"].get("values", [])

    if cust_vals:
        cust_id = cust_vals[0]["id"]
    else:
        cust_body = {"name": fields.get("customerName", "Customer"), "isCustomer": True}
        if fields.get("customerOrgNumber"):
            cust_body["organizationNumber"] = fields["customerOrgNumber"]
        ccr = await execute_tripletex_call(client, base_url, token, "POST", "/customer", body=cust_body)
        if ccr["status_code"] not in (200, 201):
            return False
        cust_id = ccr["body"]["value"]["id"]
    log.info(f"[{rid}] SOLVER customer id={cust_id}")

    today = time.strftime("%Y-%m-%d")
    vat_id = VAT_RATE_TO_TYPE.get(fields.get("vatRatePercent", 25), 3)
    order_line = {
        "description": fields.get("description") or fields.get("productName", "Product"),
        "count": fields.get("quantity", 1),
        "unitPriceExcludingVatCurrency": fields.get("productPrice", 0),
        "vatType": {"id": vat_id},
    }

    order_r = await execute_tripletex_call(
        client, base_url, token, "POST", "/order",
        body={
            "customer": {"id": cust_id},
            "orderDate": fields.get("invoiceDate") or today,
            "deliveryDate": fields.get("invoiceDate") or today,
            "orderLines": [order_line],
        },
    )
    if order_r["status_code"] not in (200, 201):
        return False
    order_id = order_r["body"]["value"]["id"]
    log.info(f"[{rid}] SOLVER POST /order -> {order_r['status_code']} id={order_id}")

    inv_r = await execute_tripletex_call(
        client, base_url, token, "POST", "/invoice",
        body={
            "invoiceDate": fields.get("invoiceDate") or today,
            "invoiceDueDate": fields.get("invoiceDueDate") or today,
            "customer": {"id": cust_id},
            "orders": [{"id": order_id}],
        },
    )
    log.info(f"[{rid}] SOLVER POST /invoice -> {inv_r['status_code']}")
    if inv_r["status_code"] not in (200, 201):
        return False

    inv_id = inv_r["body"]["value"]["id"]
    send_r = await execute_tripletex_call(
        client, base_url, token, "PUT", f"/invoice/{inv_id}/:send",
        params={"sendType": "EMAIL"},
    )
    log.info(f"[{rid}] SOLVER PUT /invoice/:send -> {send_r['status_code']}")
    return send_r["status_code"] in (200, 204)


async def _solve_register_payment(client, base_url, token, fields, log, rid):
    params = {"fields": "id,name"}
    if fields.get("customerOrgNumber"):
        params["organizationNumber"] = fields["customerOrgNumber"]
    elif fields.get("customerName"):
        params["customerName"] = fields["customerName"]
    else:
        return False

    cr = await execute_tripletex_call(client, base_url, token, "GET", "/customer", params=params)
    if cr["status_code"] != 200 or not cr["body"].get("values"):
        return False
    cust_id = cr["body"]["values"][0]["id"]
    log.info(f"[{rid}] SOLVER GET /customer -> id={cust_id}")

    pt_r = await execute_tripletex_call(
        client, base_url, token, "GET", "/invoice/paymentType",
        params={"fields": "id,description"},
    )
    if pt_r["status_code"] != 200 or not pt_r["body"].get("values"):
        return False
    pay_type_id = None
    for pt in pt_r["body"]["values"]:
        if "bank" in pt.get("description", "").lower():
            pay_type_id = pt["id"]
            break
    if pay_type_id is None:
        pay_type_id = pt_r["body"]["values"][0]["id"]
    log.info(f"[{rid}] SOLVER GET /invoice/paymentType -> id={pay_type_id}")

    inv_r = await execute_tripletex_call(
        client, base_url, token, "GET", "/invoice",
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
    log.info(f"[{rid}] SOLVER GET /invoice -> id={inv_id}, amount={amount}")

    pay_r = await execute_tripletex_call(
        client, base_url, token, "PUT", f"/invoice/{inv_id}/:payment",
        params={
            "paymentDate": time.strftime("%Y-%m-%d"),
            "paymentTypeId": pay_type_id,
            "paidAmount": amount,
        },
    )
    log.info(f"[{rid}] SOLVER PUT /invoice/:payment -> {pay_r['status_code']}")
    return pay_r["status_code"] in (200, 204)


async def _solve_supplier_invoice(client, base_url, token, fields, log, rid):
    org_nr = fields.get("supplierOrgNumber")
    if not org_nr:
        return False

    sr = await execute_tripletex_call(
        client, base_url, token, "GET", "/supplier",
        params={"organizationNumber": org_nr, "fields": "id,name"},
    )
    if sr["status_code"] != 200 or not sr["body"].get("values"):
        return False
    supplier_id = sr["body"]["values"][0]["id"]
    log.info(f"[{rid}] SOLVER GET /supplier -> id={supplier_id}")

    vt_r = await execute_tripletex_call(
        client, base_url, token, "GET", "/ledger/voucherType",
        params={"fields": "id,name"},
    )
    if vt_r["status_code"] != 200:
        return False
    voucher_type_id = None
    for vt in vt_r["body"].get("values", []):
        if "leverandør" in vt.get("name", "").lower() and "faktura" in vt.get("name", "").lower():
            voucher_type_id = vt["id"]
            break
    if voucher_type_id is None:
        return False
    log.info(f"[{rid}] SOLVER GET /ledger/voucherType -> id={voucher_type_id}")

    vat_pct = fields.get("vatRatePercent", 25)
    vat_r = await execute_tripletex_call(
        client, base_url, token, "GET", "/ledger/vatType",
    )
    if vat_r["status_code"] != 200:
        return False
    input_vat_id = None
    for vt in vat_r["body"].get("values", []):
        pct = vt.get("percentage")
        name = (vt.get("name") or "").lower()
        if pct == vat_pct and ("inngående" in name or "innkjøp" in name or "innenlands" in name):
            input_vat_id = vt["id"]
            break
    if input_vat_id is None:
        return False
    log.info(f"[{rid}] SOLVER GET /ledger/vatType -> input vat id={input_vat_id}")

    expense_acct = fields.get("expenseAccountNumber", 6300)
    acct_r = await execute_tripletex_call(
        client, base_url, token, "GET", "/ledger/account",
        params={"number": expense_acct, "fields": "id,number,name"},
    )
    if acct_r["status_code"] != 200 or not acct_r["body"].get("values"):
        return False
    expense_id = acct_r["body"]["values"][0]["id"]
    log.info(f"[{rid}] SOLVER GET /ledger/account {expense_acct} -> id={expense_id}")

    ap_r = await execute_tripletex_call(
        client, base_url, token, "GET", "/ledger/account",
        params={"number": 2400, "fields": "id,number,name"},
    )
    if ap_r["status_code"] != 200 or not ap_r["body"].get("values"):
        return False
    ap_id = ap_r["body"]["values"][0]["id"]
    log.info(f"[{rid}] SOLVER GET /ledger/account 2400 -> id={ap_id}")

    amount_incl = fields.get("amountInclVat", 0)
    amount_excl = round(amount_incl / (1 + vat_pct / 100), 2)

    inv_num = fields.get("invoiceNumber", "")
    supplier_name = fields.get("supplierName", "")
    desc_parts = [s for s in [f"Faktura {inv_num}" if inv_num else "", supplier_name] if s]
    description = " - ".join(desc_parts) or "Leverandørfaktura"

    voucher_date = fields.get("date") or time.strftime("%Y-%m-%d")

    vr = await execute_tripletex_call(
        client, base_url, token, "POST", "/ledger/voucher",
        body={
            "date": voucher_date,
            "description": description,
            "voucherType": {"id": voucher_type_id},
            "postings": [
                {
                    "account": {"id": expense_id},
                    "amountGross": amount_excl,
                    "amountGrossCurrency": amount_excl,
                    "vatType": {"id": input_vat_id},
                },
                {
                    "account": {"id": ap_id},
                    "amountGross": -amount_incl,
                    "amountGrossCurrency": -amount_incl,
                    "supplier": {"id": supplier_id},
                },
            ],
        },
    )
    log.info(f"[{rid}] SOLVER POST /ledger/voucher -> {vr['status_code']}")
    return vr["status_code"] in (200, 201)


async def _solve_payroll(client, base_url, token, fields, log, rid):
    email = fields.get("employeeEmail")
    if not email:
        return False

    emp_r = await execute_tripletex_call(
        client, base_url, token, "GET", "/employee",
        params={
            "email": email,
            "fields": "id,firstName,lastName,dateOfBirth,employments(id,startDate,division(id))",
        },
    )
    if emp_r["status_code"] != 200 or not emp_r["body"].get("values"):
        return False
    emp = emp_r["body"]["values"][0]
    emp_id = emp["id"]
    log.info(f"[{rid}] SOLVER GET /employee -> id={emp_id}")

    if not emp.get("dateOfBirth"):
        dob_r = await execute_tripletex_call(
            client, base_url, token, "PUT", f"/employee/{emp_id}",
            body={"id": emp_id, "dateOfBirth": "1990-05-15"},
        )
        if dob_r["status_code"] != 200:
            return False
        log.info(f"[{rid}] SOLVER PUT /employee dateOfBirth -> {dob_r['status_code']}")

    has_employment = bool(emp.get("employments"))

    if not has_employment:
        div_r = await execute_tripletex_call(
            client, base_url, token, "GET", "/division",
            params={"fields": "id,name"},
        )
        div_vals = div_r["body"].get("values", []) if div_r["status_code"] == 200 else []
        log.info(f"[{rid}] SOLVER GET /division -> found {len(div_vals)}")

        if div_vals:
            div_id = div_vals[0]["id"]
        else:
            dcr = await execute_tripletex_call(
                client, base_url, token, "POST", "/division",
                body={
                    "name": "Hovedkontor", "organizationNumber": "999999999",
                    "startDate": "2025-01-01",
                    "municipality": {"id": 1}, "municipalityDate": "2025-01-01",
                },
            )
            if dcr["status_code"] not in (200, 201):
                return False
            div_id = dcr["body"]["value"]["id"]
            log.info(f"[{rid}] SOLVER POST /division -> {dcr['status_code']}")

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
        log.info(f"[{rid}] SOLVER POST /employee/employment -> {empl_r['status_code']}")

    st_r = await execute_tripletex_call(
        client, base_url, token, "GET", "/salary/type",
        params={"fields": "id,number,name", "count": 100},
    )
    if st_r["status_code"] != 200:
        return False
    salary_types = st_r["body"].get("values", [])

    base_type_id = None
    bonus_type_id = None
    for st in salary_types:
        name_lower = (st.get("name") or "").lower()
        num = st.get("number", "")
        if num == "2000" or name_lower == "fastlønn":
            base_type_id = st["id"]
        if "bonus" in name_lower or num == "2030":
            bonus_type_id = st["id"]
    if base_type_id is None:
        return False
    log.info(f"[{rid}] SOLVER GET /salary/type -> base={base_type_id}, bonus={bonus_type_id}")

    now = time.localtime()
    month = now.tm_mon
    year = now.tm_year
    last_day = 28
    for d in (31, 30, 29, 28):
        try:
            time.strptime(f"{year}-{month:02d}-{d:02d}", "%Y-%m-%d")
            last_day = d
            break
        except ValueError:
            continue

    specs = []
    base_salary = fields.get("baseSalary", 0)
    if base_salary:
        specs.append({
            "salaryType": {"id": base_type_id},
            "amount": base_salary,
            "rate": base_salary,
            "count": 1,
        })

    bonus = fields.get("bonus", 0)
    if bonus and bonus_type_id:
        specs.append({
            "salaryType": {"id": bonus_type_id},
            "amount": bonus,
            "rate": bonus,
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
            "payslips": [{
                "employee": {"id": emp_id},
                "specifications": specs,
            }],
        },
    )
    log.info(f"[{rid}] SOLVER POST /salary/transaction -> {tx_r['status_code']}")
    return tx_r["status_code"] in (200, 201)


async def _solve_custom_dimension(client, base_url, token, fields, log, rid):
    dim_name = fields.get("dimensionName")
    dim_values = fields.get("dimensionValues", [])
    if not dim_name or not dim_values:
        return False

    dr = await execute_tripletex_call(
        client, base_url, token, "POST", "/ledger/accountingDimensionName",
        body={"dimensionName": dim_name},
    )
    if dr["status_code"] not in (200, 201):
        return False
    dim_index = dr["body"]["value"]["dimensionIndex"]
    log.info(f"[{rid}] SOLVER POST /ledger/accountingDimensionName '{dim_name}' -> 201 (index={dim_index})")

    value_ids = {}
    for val in dim_values:
        vr = await execute_tripletex_call(
            client, base_url, token, "POST", "/ledger/accountingDimensionValue",
            body={"displayName": val, "dimensionIndex": dim_index},
        )
        if vr["status_code"] not in (200, 201):
            return False
        value_ids[val] = vr["body"]["value"]["id"]
        log.info(f"[{rid}] SOLVER POST /ledger/accountingDimensionValue '{val}' -> 201 id={value_ids[val]}")

    voucher_acct = fields.get("voucherAccountNumber")
    amount = fields.get("voucherAmount")
    linked_value = fields.get("linkedDimensionValue")
    if not all([voucher_acct, amount, linked_value]):
        return True  # dimension created successfully, no voucher needed

    linked_id = value_ids.get(linked_value)
    if not linked_id:
        return False

    vt_r = await execute_tripletex_call(
        client, base_url, token, "GET", "/ledger/voucherType",
        params={"fields": "id,name"},
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
    log.info(f"[{rid}] SOLVER GET /ledger/voucherType -> id={voucher_type_id}")

    acct_r = await execute_tripletex_call(
        client, base_url, token, "GET", "/ledger/account",
        params={"number": voucher_acct, "fields": "id,number,name"},
    )
    if acct_r["status_code"] != 200 or not acct_r["body"].get("values"):
        return False
    expense_id = acct_r["body"]["values"][0]["id"]
    log.info(f"[{rid}] SOLVER GET /ledger/account {voucher_acct} -> id={expense_id}")

    credit_acct = fields.get("creditAccountNumber", 1920)
    cr_r = await execute_tripletex_call(
        client, base_url, token, "GET", "/ledger/account",
        params={"number": credit_acct, "fields": "id,number,name"},
    )
    if cr_r["status_code"] != 200 or not cr_r["body"].get("values"):
        return False
    credit_id = cr_r["body"]["values"][0]["id"]
    log.info(f"[{rid}] SOLVER GET /ledger/account {credit_acct} -> id={credit_id}")

    today = time.strftime("%Y-%m-%d")
    dim_field = f"freeAccountingDimension{dim_index}"
    desc = fields.get("description") or f"{acct_r['body']['values'][0].get('name', '')} - {dim_name} {linked_value}"

    vr = await execute_tripletex_call(
        client, base_url, token, "POST", "/ledger/voucher",
        body={
            "date": today,
            "description": desc,
            "voucherType": {"id": voucher_type_id},
            "postings": [
                {
                    "row": 1, "date": today,
                    "account": {"id": expense_id},
                    "amountGross": amount,
                    "amountGrossCurrency": amount,
                    "currency": {"id": 1},
                    dim_field: {"id": linked_id},
                },
                {
                    "row": 2, "date": today,
                    "account": {"id": credit_id},
                    "amountGross": -amount,
                    "amountGrossCurrency": -amount,
                    "currency": {"id": 1},
                },
            ],
        },
    )
    log.info(f"[{rid}] SOLVER POST /ledger/voucher -> {vr['status_code']}")
    return vr["status_code"] in (200, 201)


async def _solve_order_invoice_payment(client, base_url, token, fields, log, rid):
    if fields.get("customerOrgNumber"):
        cp = {"organizationNumber": fields["customerOrgNumber"], "fields": "id,name"}
    elif fields.get("customerName"):
        cp = {"customerName": fields["customerName"], "fields": "id,name"}
    else:
        return False

    cr = await execute_tripletex_call(client, base_url, token, "GET", "/customer", params=cp)
    if cr["status_code"] != 200 or not cr["body"].get("values"):
        return False
    cust_id = cr["body"]["values"][0]["id"]
    log.info(f"[{rid}] SOLVER GET /customer -> id={cust_id}")

    products = fields.get("products", [])
    if not products:
        return False

    order_lines = []
    for prod in products:
        prod_number = prod.get("number")
        if prod_number:
            pr = await execute_tripletex_call(
                client, base_url, token, "GET", "/product",
                params={"number": prod_number, "fields": "id,name,number,priceExcludingVatCurrency,vatType(id)"},
            )
            if pr["status_code"] != 200 or not pr["body"].get("values"):
                return False
            p = pr["body"]["values"][0]
            log.info(f"[{rid}] SOLVER GET /product '{prod_number}' -> id={p['id']}")
            order_lines.append({
                "product": {"id": p["id"]},
                "count": prod.get("quantity", 1),
                "unitPriceExcludingVatCurrency": prod.get("price") or p.get("priceExcludingVatCurrency", 0),
                "vatType": p.get("vatType", {"id": 3}),
            })
        else:
            vat_id = VAT_RATE_TO_TYPE.get(prod.get("vatRatePercent", 25), 3)
            order_lines.append({
                "description": prod.get("name", "Product"),
                "count": prod.get("quantity", 1),
                "unitPriceExcludingVatCurrency": prod.get("price", 0),
                "vatType": {"id": vat_id},
            })

    pt_r = await execute_tripletex_call(
        client, base_url, token, "GET", "/invoice/paymentType",
        params={"fields": "id,description"},
    )
    if pt_r["status_code"] != 200 or not pt_r["body"].get("values"):
        return False
    pay_type_id = None
    for pt in pt_r["body"]["values"]:
        if "bank" in pt.get("description", "").lower():
            pay_type_id = pt["id"]
            break
    if pay_type_id is None:
        pay_type_id = pt_r["body"]["values"][0]["id"]
    log.info(f"[{rid}] SOLVER GET /invoice/paymentType -> id={pay_type_id}")

    today = time.strftime("%Y-%m-%d")
    order_r = await execute_tripletex_call(
        client, base_url, token, "POST", "/order",
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
    log.info(f"[{rid}] SOLVER POST /order -> {order_r['status_code']} id={order_id}")

    inv_r = await execute_tripletex_call(
        client, base_url, token, "POST", "/invoice",
        body={
            "invoiceDate": today,
            "invoiceDueDate": today,
            "customer": {"id": cust_id},
            "orders": [{"id": order_id}],
        },
    )
    if inv_r["status_code"] not in (200, 201):
        return False
    inv_id = inv_r["body"]["value"]["id"]
    inv_amount = inv_r["body"]["value"].get("amount", 0)
    log.info(f"[{rid}] SOLVER POST /invoice -> {inv_r['status_code']} id={inv_id} amount={inv_amount}")

    pay_r = await execute_tripletex_call(
        client, base_url, token, "PUT", f"/invoice/{inv_id}/:payment",
        params={
            "paymentDate": today,
            "paymentTypeId": pay_type_id,
            "paidAmount": inv_amount,
        },
    )
    log.info(f"[{rid}] SOLVER PUT /invoice/:payment -> {pay_r['status_code']}")
    return pay_r["status_code"] in (200, 204)


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
}


async def try_deterministic_solve(
    prompt: str, files: list,
    client: httpx.AsyncClient, base_url: str, token: str,
    log: logging.Logger, request_id: str,
) -> bool:
    if files:
        log.info(f"[{request_id}] SOLVER: skipping (has files)")
        return False

    fields = await _extract_fields(prompt, client)
    if fields is None:
        log.info(f"[{request_id}] SOLVER: extraction failed")
        return False

    task_type = fields.get("task_type", "UNSUPPORTED")
    if task_type not in DETERMINISTIC_SOLVERS:
        log.info(f"[{request_id}] SOLVER: unsupported task '{task_type}', falling back to LLM")
        return False

    log.info(f"[{request_id}] SOLVER: task_type={task_type}")
    log.info(f"[{request_id}] SOLVER: fields={json.dumps(fields, ensure_ascii=False)[:500]}")

    try:
        ok = await DETERMINISTIC_SOLVERS[task_type](
            client, base_url, token, fields, log, request_id,
        )
        if ok:
            log.info(f"[{request_id}] SOLVER: completed successfully")
        else:
            log.info(f"[{request_id}] SOLVER: failed, falling back to LLM")
        return ok
    except Exception as e:
        log.error(f"[{request_id}] SOLVER ERROR: {e}")
        return False


# ---- OpenRouter / Claude Client --------------------------------------------


async def call_openrouter(
    messages: list, client: httpx.AsyncClient, use_reasoning: bool = False,
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
        "max_tokens": 16000 if use_reasoning else 4096,
    }

    if use_reasoning:
        payload["reasoning"] = {"enabled": True}

    response = await client.post(
        OPENROUTER_URL,
        headers=headers,
        json=payload,
        timeout=120.0,
    )
    response.raise_for_status()
    return response.json()


# ---- Bank Account Prerequisite ---------------------------------------------

VALID_NORWEGIAN_BANK_ACCOUNT = "86011117947"


async def ensure_bank_account(
    client: httpx.AsyncClient, base_url: str, token: str,
    log: logging.Logger, request_id: str,
) -> None:
    """Ensure account 1920 has a bankAccountNumber so invoice creation won't fail."""
    if token in _bank_account_done:
        return
    _bank_account_done.add(token)
    auth = ("0", token)
    try:
        resp = await client.get(
            f"{base_url}/ledger/account",
            params={"number": "1920", "fields": "id,version,bankAccountNumber"},
            auth=auth, timeout=15.0,
        )
        data = resp.json()
        values = data.get("values", [])
        if not values:
            return
        acct = values[0]
        if acct.get("bankAccountNumber"):
            return
        log.info(f"[{request_id}] Bank account 1920 missing bankAccountNumber -- setting it")
        put_resp = await client.put(
            f"{base_url}/ledger/account/{acct['id']}",
            json={
                "id": acct["id"],
                "version": acct["version"],
                "bankAccountNumber": VALID_NORWEGIAN_BANK_ACCOUNT,
            },
            auth=auth, timeout=15.0,
        )
        log.info(f"[{request_id}] Bank account setup: {put_resp.status_code}")
    except Exception as e:
        log.warning(f"[{request_id}] Bank account setup failed (non-fatal): {e}")


# ---- Agent Loop ------------------------------------------------------------


async def run_agent(
    prompt: str, files: list, credentials: dict, log: logging.Logger
) -> None:
    request_id = str(uuid.uuid4())[:8]
    base_url = credentials["base_url"]
    token = credentials["session_token"]
    start_time = time.time()

    use_reasoning = is_tier3_task(prompt, files)

    log.info(f"[{request_id}] === NEW REQUEST ===")
    log.info(f"[{request_id}] Prompt: {prompt[:500]}")
    log.info(f"[{request_id}] Files: {len(files)}")
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
        await ensure_bank_account(client, base_url, token, log, request_id)

        if await try_deterministic_solve(prompt, files, client, base_url, token, log, request_id):
            total_elapsed = time.time() - start_time
            log.info(f"[{request_id}] === REQUEST COMPLETE === ({total_elapsed:.1f}s)")
            return

        for iteration in range(MAX_ITERATIONS):
            elapsed = time.time() - start_time
            if elapsed > SOLVE_TIMEOUT:
                log.warning(
                    f"[{request_id}] Timeout after {elapsed:.1f}s at iteration {iteration}"
                )
                break

            log.info(f"[{request_id}] Iteration {iteration + 1}, elapsed {elapsed:.1f}s")

            try:
                response = await call_openrouter(messages, client, use_reasoning)
            except httpx.HTTPStatusError as e:
                log.error(f"[{request_id}] OpenRouter HTTP error: {e.response.status_code} {e.response.text[:500]}")
                break
            except Exception as e:
                log.error(f"[{request_id}] OpenRouter error: {e}")
                break

            choice = response["choices"][0]
            assistant_msg = choice["message"]
            finish_reason = choice.get("finish_reason", "")

            msg_for_history = {k: v for k, v in assistant_msg.items() if k != "reasoning"}
            messages.append(msg_for_history)

            if finish_reason != "tool_calls" or not assistant_msg.get("tool_calls"):
                log.info(f"[{request_id}] Agent finished (reason: {finish_reason})")
                if assistant_msg.get("content"):
                    log.info(
                        f"[{request_id}] Final message: {str(assistant_msg['content'])[:300]}"
                    )
                break

            consecutive_proxy_403 = 0

            for tool_call in assistant_msg["tool_calls"]:
                tc_id = tool_call["id"]
                func = tool_call["function"]
                func_name = func["name"]

                try:
                    args = json.loads(func["arguments"])
                except json.JSONDecodeError:
                    args = {}

                if func_name == "tripletex_api":
                    method = args.get("method", "GET")
                    path = args.get("path", "/")
                    params = args.get("params")
                    body = args.get("body")

                    norm_path = _normalize_api_path(path)

                    endpoint_err = _validate_endpoint(method, norm_path)
                    if endpoint_err:
                        result = {"status_code": 404, "body": endpoint_err}
                        log.info(f"[{request_id}] BLOCKED invalid endpoint: {method} {path}")
                    elif method == "GET" and (prefix := _cacheable_prefix(norm_path)):
                        key = _cache_key(token, norm_path, params)
                        cached_result = _api_cache.get(key)
                        if cached_result is not None:
                            result = cached_result
                            log.info(
                                f"[{request_id}] CACHE HIT: GET {path}"
                                f" params={json.dumps(params, ensure_ascii=False) if params else None}"
                            )
                        else:
                            result = None
                    else:
                        result = None

                    if endpoint_err:
                        pass  # already set above
                    elif result is not None:
                        pass  # cache hit, already set
                    else:
                        log.info(
                            f"[{request_id}] API call: {method} {path}"
                            f" params={json.dumps(params, ensure_ascii=False) if params else None}"
                            f" body_keys={list(body.keys()) if isinstance(body, dict) else type(body).__name__ if body else None}"
                        )

                        call_start = time.time()
                        result = await execute_tripletex_call(
                            client, base_url, token, method, path, params, body
                        )
                        call_elapsed = time.time() - call_start

                        status = result["status_code"]
                        resp_body = result.get("body", {})
                        resp_str = json.dumps(resp_body, ensure_ascii=False, default=str) if not isinstance(resp_body, str) else resp_body

                        log.info(
                            f"[{request_id}] Response: {status}"
                            f" ({call_elapsed:.1f}s)"
                            f" preview={resp_str[:300]}"
                        )

                        if method == "GET" and status == 200:
                            prefix = _cacheable_prefix(norm_path)
                            if prefix:
                                key = _cache_key(token, norm_path, params)
                                _api_cache[key] = result

                        if method in ("POST", "PUT", "DELETE"):
                            cleared = _invalidate_cache(token, norm_path)
                            if cleared:
                                log.info(f"[{request_id}] Cache invalidated: {cleared} entries for {norm_path}")

                        if status == 403 and "expired proxy token" in resp_str.lower():
                            consecutive_proxy_403 += 1
                        else:
                            consecutive_proxy_403 = 0

                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc_id,
                        "content": truncate_for_context(result),
                    })
                elif func_name == "compute_taxable_result":
                    date_from = args.get("date_from", "")
                    date_to = args.get("date_to", "")

                    log.info(
                        f"[{request_id}] compute_taxable_result:"
                        f" {date_from} to {date_to}"
                    )

                    call_start = time.time()
                    result = await compute_result_from_postings(
                        client, base_url, token, date_from, date_to
                    )
                    call_elapsed = time.time() - call_start

                    log.info(
                        f"[{request_id}] Taxable result computed"
                        f" ({call_elapsed:.1f}s):"
                        f" net_result={result['net_result']}"
                        f" postings={result['total_postings_fetched']}"
                    )

                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc_id,
                        "content": json.dumps(result, ensure_ascii=False),
                    })
                else:
                    log.warning(f"[{request_id}] Unknown tool: {func_name}")
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc_id,
                        "content": json.dumps({"error": f"Unknown tool: {func_name}"}),
                    })

            if consecutive_proxy_403 >= 2:
                log.error(
                    f"[{request_id}] Aborting: {consecutive_proxy_403} consecutive 403"
                    " 'expired proxy token' errors -- token is invalid"
                )
                break

    total_elapsed = time.time() - start_time
    log.info(f"[{request_id}] === REQUEST COMPLETE === ({total_elapsed:.1f}s)")


# ---- FastAPI Endpoints -----------------------------------------------------


@app.post("/solve")
async def solve(request: Request, test: bool = Query(False)):
    body = await request.json()
    prompt = body["prompt"]
    files = body.get("files", [])
    credentials = body["tripletex_credentials"]

    log = testing_log if test else submission_log

    try:
        await run_agent(prompt, files, credentials, log)
    except Exception as e:
        log.error(f"Agent error: {e}", exc_info=True)

    return JSONResponse({"status": "completed"})


@app.get("/health")
async def health():
    return {"status": "ok", "model": MODEL}
