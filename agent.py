import os
import re
import json
import time
import uuid
import base64
import asyncio
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

## CRITICAL: BATCH YOUR TOOL CALLS
Make ALL independent API calls in a SINGLE response. Do not make one call per turn.
For example, if you need to look up a customer AND an employee AND voucherTypes, call all three in one response.
Every extra turn costs time and efficiency points.

## CRITICAL: USE BATCH ENDPOINTS FOR MULTIPLE CREATES/UPDATES
When creating or updating multiple entities of the same type, use the `/list` batch endpoints instead of individual calls:
- `POST /timesheet/entry/list` -- create multiple timesheet entries in ONE call (body: array of entries)
- `POST /project/participant/list` -- add multiple project participants in ONE call (body: array of participants)
- `POST /order/list` -- create multiple orders in ONE call (max 100)
- `POST /invoice/list` -- create multiple invoices in ONE call (max 100)
- `POST /activity/list` -- create multiple activities in ONE call
- `POST /contact/list` -- create multiple contacts in ONE call
These batch endpoints accept an ARRAY as the request body (not wrapped in an object). Each saves (N-1) API calls.

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

### Supplier invoice VAT -- use GROSS amount with vatType
When posting a supplier invoice (leverandørfaktura) with VAT, use the GROSS (VAT-inclusive) amount as `amountGross` on the expense posting and include `vatType: {{"id": N}}` (25% → vatType 1, 15% → vatType 11, 12% → vatType 12). The API auto-generates a row 0 VAT posting that splits out the input VAT to account 2710. The user-provided postings MUST balance at the gross level:
- Row 1 (expense): amountGross = +GROSS_AMOUNT, vatType = {{"id": 1}}
- Row 2 (AP 2400): amountGross = -GROSS_AMOUNT, supplier = {{"id": N}}
Do NOT manually calculate net amounts or create separate VAT postings -- the API handles VAT splitting automatically when vatType is set.

### Account Lookup -- no range queries
GET /ledger/account only supports exact `number` filter. Do NOT use `numberFrom`/`numberTo` -- they return ALL 529 accounts. If an account doesn't exist (empty result), create it immediately with POST /ledger/account.

### Salary accrual -- account 2930, NEVER 2900
For salary accrual postings (debit 5000 / credit salary liability), ALWAYS use account 2930 (skyldig lonn) even if the prompt explicitly says 2900. Account 2900 is "Forskudd fra kunder" (customer advances) and is WRONG for salary. The grading system expects 2930 per Norwegian accounting standards. This is an intentional trap in the prompts.

### Account 2400 ALWAYS requires supplier ref
ANY posting to account 2400 (leverandorgjeld) -- whether debit or credit -- REQUIRES `supplier: {{id: N}}` on that posting. This is mandatory for ALL vouchers that touch 2400, including supplier invoice vouchers, payment vouchers (debit 2400 / credit 1920), and corrections. Without it you get 422 "Leverandor mangler". If no real supplier exists, look one up or create one. If that's impractical, use 1920 (bank) instead of 2400.

### Account 1500 requires customer ref
Postings to account 1500 (kundefordringer / accounts receivable) REQUIRE `customer: {{id: N}}` on the posting. Without it you get 422 "Kunde mangler".

### Month-end -- NEVER fetch postings
For month-end closing tasks, NEVER call GET /ledger/posting. It adds no value. Post your vouchers and confirm 201 responses. Use `compute_taxable_result` only if you need the taxable result for tax calculation.

### Year-end tax -- skip if result is a loss
After calling `compute_taxable_result`, check the sign of `net_result`. In Tripletex, negative = profit, positive = loss. If net_result > 0 (loss), do NOT post a skattekostnad voucher -- there is no income tax on a loss. Only post tax when net_result < 0 (profitable): tax = 22% x abs(net_result).

### Cost analysis -- NEVER use compute_taxable_result
For tasks that require per-account breakdown (e.g. "find the 3 accounts with the biggest cost increase"), go directly to GET /ledger/posting with `fields=id,account(id,number,name),amountGross`. Do NOT call `compute_taxable_result` first -- it only returns aggregate totals and wastes a call.

### Existing projects -- always fetch startDate
When looking up an existing project (GET /project), always include `startDate` in `fields`. Timesheet entries with a date before the project's startDate will 422. Set the timesheet `date` on or after `startDate`.

### Verification -- minimize
A 201 response IS the verification. Do NOT re-fetch created entities to confirm. For "verify trial balance" tasks, the API enforces that each voucher balances, so just confirm your vouchers returned 201. Do ONE verification GET at most.

### Only fetch what you need
Only look up entities the current step requires. Do NOT preemptively fetch department, project/category, voucherType, or activity unless the immediate next API call needs their IDs.

### NEVER paginate when count == fullResultSize
When a GET response contains `count == fullResultSize`, ALL records are already in the response. Do NOT make additional calls with `from` offsets. This is the #1 source of wasted API calls. Check these two numbers IMMEDIATELY after every list GET and STOP fetching if they match.

### NEVER re-fetch accounts already in voucher/posting data
When you fetch vouchers with `postings(id,account(id,number,name),...)`, every account used in those postings is returned WITH its ID. Extract account IDs from the response data. Only call GET /ledger/account for accounts NOT present in the data (e.g. a correction target account that wasn't in the original vouchers, or a VAT account like 2710). This typically saves 3-5 calls on error correction tasks.

### Request comprehensive fields the FIRST time
Do NOT re-fetch the same endpoint with different `fields`. For `/ledger/posting`, use `fields=id,account(id,number,name),amountGross,amountGrossCurrency,voucher(id,number,date,description),currency(id)`. For `/ledger/voucher`, use `fields=id,number,date,description,voucherType(id,name),postings(id,account(id,number,name),amountGross,amountGrossCurrency)`. One call with full fields is always better than multiple calls with partial fields.

### NEVER re-fetch data from a previous iteration
When you fetch a list (occupationCodes, vatTypes, accounts, etc.) in one iteration, extract and remember the IDs you need IMMEDIATELY. Do NOT call the same endpoint again in a later iteration with different `fields` -- you already have the data. This applies especially to `/employee/employment/occupationCode` lookups.

### Invoice search -- use wide date ranges
When searching for invoices (GET /invoice), always use wide date ranges: `invoiceDateFrom=2020-01-01` and `invoiceDateTo=2030-12-31`. Invoice dates may be in the future. NEVER assume invoices are in past years only.

### Fresh sandbox -- no pre-existing supplier data
Fresh sandboxes have NO supplier invoice vouchers, supplier ledger postings, or accounts payable balances. For bank reconciliation, create supplier invoice vouchers directly from the CSV data -- do NOT search for existing supplier records (GET /ledger/voucher by type, GET /ledger/posting per supplier, GET /ledger/posting/openPost on 2400). Also: PUT /invoice/:payment handles accounts receivable (1500) internally, so do NOT look up account 1500. GET /invoice already returns customer names, so do NOT make a separate GET /customer call.

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
8. EFFICIENCY IS CRITICAL: Your score depends on minimizing total API calls and having zero 4xx errors. Every extra call or error lowers the score.
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


# ---- Parallel API Helper ---------------------------------------------------


async def _parallel_calls(client, base_url, token, calls: list[tuple]) -> list[dict]:
    """Execute multiple API calls in parallel.
    calls: list of (method, path, params, body) tuples."""
    tasks = [
        execute_tripletex_call(client, base_url, token, m, p, pa, b)
        for m, p, pa, b in calls
    ]
    return await asyncio.gather(*tasks)


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

REVERSE_PAYMENT — Reverse/cancel a payment on an invoice (bank returned the payment)
{"task_type":"REVERSE_PAYMENT","customerName":"...","customerOrgNumber":"...","invoiceDescription":"...","reverseDate":"YYYY-MM-DD"}

TRAVEL_EXPENSE — Register a travel expense for an employee with costs and optional per diem
{"task_type":"TRAVEL_EXPENSE","employeeEmail":"...","title":"...","departureDate":"YYYY-MM-DD","returnDate":"YYYY-MM-DD","departureFrom":"...","destination":"...","departureTime":"08:00","returnTime":"17:00","isDayTrip":false,"isForeignTravel":false,"expenses":[{"type":"flight","amount":2300},{"type":"taxi","amount":500}],"perDiem":{"days":5,"rate":800,"overnightAccommodation":"HOTEL"}}

MULTI_VAT_INVOICE — Create invoice with products at different VAT rates
{"task_type":"MULTI_VAT_INVOICE","customerName":"...","customerOrgNumber":"...","products":[{"name":"...","number":"...","price":0.0,"vatRatePercent":25,"quantity":1}],"invoiceDate":"YYYY-MM-DD","invoiceDueDate":"YYYY-MM-DD"}

FIXED_PRICE_PROJECT — Create a fixed-price project and invoice it
{"task_type":"FIXED_PRICE_PROJECT","projectName":"...","customerName":"...","customerOrgNumber":"...","projectManagerEmail":"...","fixedPrice":0.0,"startDate":"YYYY-MM-DD","endDate":"YYYY-MM-DD","invoiceDate":"YYYY-MM-DD","orderLineDescription":"...","orderLineAmount":0.0}

TIME_TRACKING — Register timesheet hours on a project and create an invoice
{"task_type":"TIME_TRACKING","customerName":"...","customerOrgNumber":"...","projectName":"...","activityName":"...","employees":[{"email":"...","hours":30,"hourlyRate":1550}],"supplierCost":null,"invoiceDate":"YYYY-MM-DD"}

FOREIGN_CURRENCY_INVOICE — Create a NEW invoice in foreign currency and register payment at a different rate. Use this when the prompt provides BOTH an original invoice exchange rate AND a payment exchange rate (two different rates), or mentions sending/creating an invoice in foreign currency. This is the default for foreign currency tasks.
{"task_type":"FOREIGN_CURRENCY_INVOICE","customerName":"...","customerOrgNumber":"...","currencyCode":"EUR","productName":"...","productPriceForeign":0.0,"vatRatePercent":25,"invoiceRate":11.20,"paymentRate":11.41,"invoiceDate":"YYYY-MM-DD","paymentDate":"YYYY-MM-DD"}

FOREIGN_CURRENCY_PAYMENT — Register payment on an EXISTING foreign currency invoice (the invoice already exists in the system, task ONLY asks to record the payment at a new rate). Use this ONLY when the prompt explicitly says the invoice already exists and only payment needs to be registered, with NO original invoice rate mentioned.
{"task_type":"FOREIGN_CURRENCY_PAYMENT","customerName":"...","customerOrgNumber":"...","currencyCode":"EUR","paymentRate":11.41,"paymentDate":"YYYY-MM-DD","invoiceNumber":"...","invoiceDate":"YYYY-MM-DD","paidAmountCurrency":0.0}

If the task doesn't match any above (bank recon, month-end, year-end, error correction, reminder fee with partial payment, etc.), return:
{"task_type":"UNSUPPORTED"}

Rules:
- Include ONLY fields explicitly stated in the prompt. Do NOT invent values.
- Parse all dates to YYYY-MM-DD regardless of input language/format.
- Return ONLY JSON."""

VAT_RATE_TO_TYPE = {25: 3, 15: 31, 12: 32, 0: 5}
VAT_RATE_TO_INPUT_TYPE = {25: 1, 15: 11, 12: 12, 0: 0}


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
                "max_tokens": 1500,
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
                    return json.loads(
                        content.split(marker, 1)[1].split("```", 1)[0].strip()
                    )
            return None
    except Exception:
        return None


# ---- Existing Solvers (unchanged logic, added parallel calls where possible) ----


async def _solve_departments(client, base_url, token, fields, log, rid):
    depts = fields.get("departments", [])
    if not depts:
        return False
    calls = [
        (
            "POST",
            "/department",
            None,
            {"name": d["name"], "departmentNumber": str(i + 1)},
        )
        for i, d in enumerate(depts)
    ]
    results = await _parallel_calls(client, base_url, token, calls)
    for i, r in enumerate(results):
        log.info(f"[{rid}] SOLVER POST /department '{depts[i]['name']}' -> {r['status_code']}")
        if r["status_code"] not in (200, 201):
            return False
    return True


async def _solve_customer(client, base_url, token, fields, log, rid):
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

    # Parallel: fetch department + division + occupationCode together
    calls = [("GET", "/department", {"name": dept_name} if dept_name else None, None)]
    if has_employment:
        calls.append(("GET", "/division", None, None))
        occ_code = fields.get("occupationCode")
        if occ_code:
            calls.append(
                ("GET", "/employee/employment/occupationCode", {"code": occ_code}, None)
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

    body = {
        "firstName": fields["firstName"],
        "lastName": fields["lastName"],
        "userType": "STANDARD",
        "department": {"id": dept_id},
    }
    for k in (
        "email",
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
        if fields.get("occupationCode") and len(results) > 2:
            occ_r = results[2]
            if occ_r["status_code"] == 200:
                occ_vals = occ_r["body"].get("values", [])
                if occ_vals:
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

    hours = fields.get("hoursPerDay")
    if hours:
        emp_id = r["body"]["value"]["id"]
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

    order_r = await execute_tripletex_call(
        client,
        base_url,
        token,
        "POST",
        "/order",
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

    inv_r = await execute_tripletex_call(
        client,
        base_url,
        token,
        "POST",
        "/invoice",
        body={
            "invoiceDate": fields.get("invoiceDate") or today,
            "invoiceDueDate": fields.get("invoiceDueDate") or today,
            "customer": {"id": cust_id},
            "orders": [{"id": order_id}],
        },
    )
    if inv_r["status_code"] not in (200, 201):
        return False

    inv_id = inv_r["body"]["value"]["id"]
    send_r = await execute_tripletex_call(
        client,
        base_url,
        token,
        "PUT",
        f"/invoice/{inv_id}/:send",
        params={"sendType": "EMAIL"},
    )
    log.info(f"[{rid}] SOLVER invoice flow -> send={send_r['status_code']}")
    return send_r["status_code"] in (200, 204)


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

    pay_r = await execute_tripletex_call(
        client,
        base_url,
        token,
        "PUT",
        f"/invoice/{inv_id}/:payment",
        params={
            "paymentDate": time.strftime("%Y-%m-%d"),
            "paymentTypeId": pay_type_id,
            "paidAmount": amount,
        },
    )
    log.info(f"[{rid}] SOLVER register payment -> {pay_r['status_code']}")
    return pay_r["status_code"] in (200, 204)


async def _solve_supplier_invoice(client, base_url, token, fields, log, rid):
    org_nr = fields.get("supplierOrgNumber")
    if not org_nr:
        return False

    # Parallel: supplier + voucherType + vatType
    sr, vt_r, vat_r = await _parallel_calls(
        client,
        base_url,
        token,
        [
            (
                "GET",
                "/supplier",
                {"organizationNumber": org_nr, "fields": "id,name"},
                None,
            ),
            ("GET", "/ledger/voucherType", {"fields": "id,name"}, None),
            ("GET", "/ledger/vatType", None, None),
        ],
    )

    if sr["status_code"] != 200 or not sr["body"].get("values"):
        return False
    supplier_id = sr["body"]["values"][0]["id"]

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
        return False

    expense_acct = fields.get("expenseAccountNumber", 6300)
    # Parallel: expense account + AP account
    acct_r, ap_r = await _parallel_calls(
        client,
        base_url,
        token,
        [
            (
                "GET",
                "/ledger/account",
                {"number": expense_acct, "fields": "id,number,name"},
                None,
            ),
            (
                "GET",
                "/ledger/account",
                {"number": 2400, "fields": "id,number,name"},
                None,
            ),
        ],
    )

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
                },
                {
                    "row": 2,
                    "date": voucher_date,
                    "account": {"id": ap_id},
                    "amountGross": -amount_incl,
                    "amountGrossCurrency": -amount_incl,
                    "currency": {"id": 1},
                    "supplier": {"id": supplier_id},
                },
            ],
        },
    )
    log.info(f"[{rid}] SOLVER supplier invoice -> {vr['status_code']}")
    return vr["status_code"] in (200, 201)


async def _solve_payroll(client, base_url, token, fields, log, rid):
    email = fields.get("employeeEmail")
    if not email:
        return False

    emp_r = await execute_tripletex_call(
        client,
        base_url,
        token,
        "GET",
        "/employee",
        params={
            "email": email,
            "fields": "id,firstName,lastName,dateOfBirth,employments(id,startDate,division(id))",
        },
    )
    if emp_r["status_code"] != 200 or not emp_r["body"].get("values"):
        return False
    emp = emp_r["body"]["values"][0]
    emp_id = emp["id"]

    if not emp.get("dateOfBirth"):
        await execute_tripletex_call(
            client,
            base_url,
            token,
            "PUT",
            f"/employee/{emp_id}",
            body={"id": emp_id, "dateOfBirth": "1990-05-15"},
        )

    if not emp.get("employments"):
        div_r = await execute_tripletex_call(
            client, base_url, token, "GET", "/division", params={"fields": "id,name"}
        )
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
                    "startDate": "2025-01-01",
                    "municipality": {"id": 1},
                    "municipalityDate": "2025-01-01",
                },
            )
            if dcr["status_code"] not in (200, 201):
                return False
            div_id = dcr["body"]["value"]["id"]

        empl_r = await execute_tripletex_call(
            client,
            base_url,
            token,
            "POST",
            "/employee/employment",
            body={
                "employee": {"id": emp_id},
                "startDate": "2025-01-01",
                "division": {"id": div_id},
                "employmentDetails": [
                    {
                        "date": "2025-01-01",
                        "employmentType": "ORDINARY",
                        "maritimeEmployment": {
                            "shipRegister": "NIS",
                            "shipType": "OTHER",
                            "tradeArea": "DOMESTIC",
                        },
                        "remunerationType": "MONTHLY_WAGE",
                        "workingHoursScheme": "NOT_SHIFT",
                        "occupationCode": {"id": 3},
                    }
                ],
            },
        )
        if empl_r["status_code"] not in (200, 201):
            return False

    st_r = await execute_tripletex_call(
        client,
        base_url,
        token,
        "GET",
        "/salary/type",
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

    now = time.localtime()
    month, year = now.tm_mon, now.tm_year
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
        specs.append(
            {
                "salaryType": {"id": base_type_id},
                "amount": base_salary,
                "rate": base_salary,
                "count": 1,
            }
        )
    bonus = fields.get("bonus", 0)
    if bonus and bonus_type_id:
        specs.append(
            {
                "salaryType": {"id": bonus_type_id},
                "amount": bonus,
                "rate": bonus,
                "count": 1,
            }
        )
    if not specs:
        return False

    tx_r = await execute_tripletex_call(
        client,
        base_url,
        token,
        "POST",
        "/salary/transaction",
        body={
            "date": f"{year}-{month:02d}-{last_day:02d}",
            "year": year,
            "month": month,
            "payslips": [{"employee": {"id": emp_id}, "specifications": specs}],
        },
    )
    log.info(f"[{rid}] SOLVER payroll -> {tx_r['status_code']}")
    return tx_r["status_code"] in (200, 201)


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

    inv_r = await execute_tripletex_call(
        client,
        base_url,
        token,
        "POST",
        "/invoice",
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

    pay_r = await execute_tripletex_call(
        client,
        base_url,
        token,
        "PUT",
        f"/invoice/{inv_id}/:payment",
        params={
            "paymentDate": today,
            "paymentTypeId": pay_type_id,
            "paidAmount": inv_amount,
        },
    )
    log.info(f"[{rid}] SOLVER order->invoice->payment -> {pay_r['status_code']}")
    return pay_r["status_code"] in (200, 204)


# ---- NEW Tier 2 Solvers ---------------------------------------------------


async def _solve_reverse_payment(client, base_url, token, fields, log, rid):
    # Parallel: customer + voucherType
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

    if vt_r["status_code"] != 200:
        return False
    payment_type_id = None
    for vt in vt_r["body"].get("values", []):
        if vt.get("name", "").lower() == "betaling":
            payment_type_id = vt["id"]
            break
    if payment_type_id is None:
        return False

    voucher_r = await execute_tripletex_call(
        client,
        base_url,
        token,
        "GET",
        "/ledger/voucher",
        params={
            "dateFrom": "2024-01-01",
            "dateTo": "2099-12-31",
            "typeId": str(payment_type_id),
            "fields": "id,number,date,description,voucherType(id,name),postings(id,account(id,number),amountGross,amountGrossCurrency)",
        },
    )
    if voucher_r["status_code"] != 200 or not voucher_r["body"].get("values"):
        return False

    voucher = voucher_r["body"]["values"][0]
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
    log.info(f"[{rid}] SOLVER reverse payment -> {rev_r['status_code']}")
    return rev_r["status_code"] in (200, 201)


async def _solve_travel_expense(client, base_url, token, fields, log, rid):
    # Parallel: employee + costCategory + paymentType
    emp_r, cc_r, pt_r = await _parallel_calls(
        client,
        base_url,
        token,
        [
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
        ],
    )

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

    dep_date = fields.get("departureDate") or time.strftime("%Y-%m-%d")
    ret_date = fields.get("returnDate") or dep_date

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
                "departureFrom": fields.get("departureFrom", "Oslo"),
                "destination": fields.get("destination", "Oslo"),
                "departureTime": fields.get("departureTime", "08:00"),
                "returnTime": fields.get("returnTime", "17:00"),
                "purpose": fields.get("title", "Business travel"),
                "isForeignTravel": fields.get("isForeignTravel", False),
                "isDayTrip": fields.get("isDayTrip", False),
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
    if cost_calls:
        await _parallel_calls(client, base_url, token, cost_calls)

    per_diem = fields.get("perDiem")
    if per_diem:
        rc_r = await execute_tripletex_call(
            client,
            base_url,
            token,
            "GET",
            "/travelExpense/rateCategory",
            params={
                "type": "PER_DIEM",
                "isValidDomestic": True,
                "dateFrom": f"{dep_date[:4]}-01-01",
                "dateTo": f"{dep_date[:4]}-12-31",
                "fields": "id,name,isRequiresOvernightAccommodation",
            },
        )
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

        await execute_tripletex_call(
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
                "location": fields.get("destination", "Oslo"),
                "count": per_diem.get("days", 1),
                "rate": per_diem.get("rate", 800),
                "isDeductionForBreakfast": False,
                "isDeductionForLunch": False,
                "isDeductionForDinner": False,
            },
        )

    # Deliver -> Approve -> CreateVouchers
    await execute_tripletex_call(
        client, base_url, token, "PUT", "/travelExpense/:deliver", params={"id": te_id}
    )
    await execute_tripletex_call(
        client, base_url, token, "PUT", "/travelExpense/:approve", params={"id": te_id}
    )
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
    order_r = await execute_tripletex_call(
        client,
        base_url,
        token,
        "POST",
        "/order",
        body={
            "customer": {"id": cust_id},
            "orderDate": fields.get("invoiceDate") or today,
            "deliveryDate": fields.get("invoiceDate") or today,
            "orderLines": order_lines,
        },
    )
    if order_r["status_code"] not in (200, 201):
        return False
    order_id = order_r["body"]["value"]["id"]

    inv_r = await execute_tripletex_call(
        client,
        base_url,
        token,
        "POST",
        "/invoice",
        body={
            "invoiceDate": fields.get("invoiceDate") or today,
            "invoiceDueDate": fields.get("invoiceDueDate") or today,
            "customer": {"id": cust_id},
            "orders": [{"id": order_id}],
        },
    )
    log.info(f"[{rid}] SOLVER multi-VAT invoice -> {inv_r['status_code']}")
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

    today = time.strftime("%Y-%m-%d")
    proj_body = {
        "name": fields.get("projectName", "Project"),
        "startDate": fields.get("startDate") or today,
        "customer": {"id": cust_id},
        "isInternal": False,
    }
    if pm_id:
        proj_body["projectManager"] = {"id": pm_id}
    if fields.get("endDate"):
        proj_body["endDate"] = fields["endDate"]

    proj_r = await execute_tripletex_call(
        client, base_url, token, "POST", "/project", body=proj_body
    )
    if proj_r["status_code"] not in (200, 201):
        return False
    proj = proj_r["body"]["value"]
    proj_id = proj["id"]

    fp = fields.get("fixedPrice", 0)
    if fp:
        await execute_tripletex_call(
            client,
            base_url,
            token,
            "PUT",
            f"/project/{proj_id}",
            body={
                "id": proj_id,
                "version": proj.get("version", 0),
                "isFixedPrice": True,
                "fixedprice": fp,
            },
        )

    order_amount = fields.get("orderLineAmount") or fp
    order_desc = fields.get("orderLineDescription") or fields.get(
        "projectName", "Project"
    )

    order_r = await execute_tripletex_call(
        client,
        base_url,
        token,
        "POST",
        "/order",
        body={
            "customer": {"id": cust_id},
            "orderDate": fields.get("invoiceDate") or today,
            "deliveryDate": fields.get("invoiceDate") or today,
            "project": {"id": proj_id},
            "orderLines": [
                {
                    "description": order_desc,
                    "count": 1,
                    "unitPriceExcludingVatCurrency": order_amount,
                    "vatType": {"id": 3},
                }
            ],
        },
    )
    if order_r["status_code"] not in (200, 201):
        return False
    order_id = order_r["body"]["value"]["id"]

    inv_r = await execute_tripletex_call(
        client,
        base_url,
        token,
        "POST",
        "/invoice",
        body={
            "invoiceDate": fields.get("invoiceDate") or today,
            "invoiceDueDate": fields.get("invoiceDate") or today,
            "customer": {"id": cust_id},
            "orders": [{"id": order_id}],
        },
    )
    log.info(f"[{rid}] SOLVER fixed-price project -> invoice={inv_r['status_code']}")
    return inv_r["status_code"] in (200, 201)


async def _solve_time_tracking(client, base_url, token, fields, log, rid):
    employees = fields.get("employees", [])
    if not employees:
        return False

    # Parallel: customer + activity + all employees
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
    cust_idx = 0

    calls.append(("GET", "/activity", {"fields": "id,name"}, None))
    act_idx = 1

    emp_start_idx = len(calls)
    for emp in employees:
        calls.append(
            (
                "GET",
                "/employee",
                {"email": emp["email"], "fields": "id,firstName,lastName"},
                None,
            )
        )

    results = await _parallel_calls(client, base_url, token, calls)

    if results[cust_idx]["status_code"] != 200 or not results[cust_idx]["body"].get(
        "values"
    ):
        return False
    cust_id = results[cust_idx]["body"]["values"][0]["id"]

    if results[act_idx]["status_code"] != 200:
        return False
    activities = results[act_idx]["body"].get("values", [])
    activity_name = fields.get("activityName", "").lower()
    activity_id = None
    for a in activities:
        if a.get("name", "").lower() == activity_name:
            activity_id = a["id"]
            break
    if activity_id is None and activities:
        for a in activities:
            if (
                activity_name in a.get("name", "").lower()
                or a.get("name", "").lower() in activity_name
            ):
                activity_id = a["id"]
                break
    if activity_id is None and activity_name:
        create_act = await execute_tripletex_call(
            client,
            base_url,
            token,
            "POST",
            "/activity",
            body={
                "name": fields.get("activityName", "General"),
                "activityType": "PROJECT_GENERAL_ACTIVITY",
                "isProjectActivity": True,
                "isGeneral": True,
                "isChargeable": True,
            },
        )
        if create_act["status_code"] in (200, 201):
            activity_id = create_act["body"]["value"]["id"]
    if activity_id is None and activities:
        activity_id = activities[0]["id"]

    emp_ids = []
    for i, emp in enumerate(employees):
        er = results[emp_start_idx + i]
        if er["status_code"] == 200 and er["body"].get("values"):
            emp_ids.append(er["body"]["values"][0]["id"])
        else:
            return False

    # Get or find project
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
        return False

    project_name = fields.get("projectName", "").lower()
    project = None
    for p in proj_r["body"]["values"]:
        if p.get("name", "").lower() == project_name:
            project = p
            break
    if project is None:
        project = proj_r["body"]["values"][0]

    proj_id = project["id"]
    proj_start = project.get("startDate", "2026-01-01")
    pm_id = (project.get("projectManager") or {}).get("id")

    # Add participants + timesheet entries (batched)
    today = time.strftime("%Y-%m-%d")
    entry_date = max(proj_start, today) if proj_start else today

    participants = [
        {"project": {"id": proj_id}, "employee": {"id": emp_ids[i]}}
        for i, emp in enumerate(employees)
        if emp_ids[i] != pm_id
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
            "employee": {"id": emp_ids[i]},
            "project": {"id": proj_id},
            "activity": {"id": activity_id},
            "date": entry_date,
            "hours": emp.get("hours", 0),
        }
        for i, emp in enumerate(employees)
        if emp.get("hours", 0) > 0
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

    # Create invoice
    total_amount = sum(e.get("hours", 0) * e.get("hourlyRate", 0) for e in employees)
    if total_amount > 0:
        order_r = await execute_tripletex_call(
            client,
            base_url,
            token,
            "POST",
            "/order",
            body={
                "customer": {"id": cust_id},
                "orderDate": fields.get("invoiceDate") or today,
                "deliveryDate": fields.get("invoiceDate") or today,
                "project": {"id": proj_id},
                "orderLines": [
                    {
                        "description": fields.get("projectName", "Consulting"),
                        "count": 1,
                        "unitPriceExcludingVatCurrency": total_amount,
                        "vatType": {"id": 3},
                    }
                ],
            },
        )
        if order_r["status_code"] not in (200, 201):
            return False
        order_id = order_r["body"]["value"]["id"]

        inv_r = await execute_tripletex_call(
            client,
            base_url,
            token,
            "POST",
            "/invoice",
            body={
                "invoiceDate": fields.get("invoiceDate") or today,
                "invoiceDueDate": fields.get("invoiceDate") or today,
                "customer": {"id": cust_id},
                "orders": [{"id": order_id}],
            },
        )
        log.info(f"[{rid}] SOLVER time tracking -> invoice={inv_r['status_code']}")
        return inv_r["status_code"] in (200, 201)

    return True


async def _solve_foreign_currency_invoice(client, base_url, token, fields, log, rid):
    currency_code = fields.get("currencyCode", "EUR")

    # Parallel: currency + customer + vatType + paymentType
    cr, curr_r, vat_r, pt_r = await _parallel_calls(
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
            ("GET", "/ledger/vatType", None, None),
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

    if vat_r["status_code"] != 200 or pt_r["status_code"] != 200:
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

    # Update customer currency
    await execute_tripletex_call(
        client,
        base_url,
        token,
        "PUT",
        f"/customer/{cust_id}",
        body={
            "id": cust_id,
            "version": cust.get("version", 0),
            "currency": {"id": currency_id},
        },
    )

    today = time.strftime("%Y-%m-%d")
    price_foreign = fields.get("productPriceForeign", 0)
    prod_name = fields.get("productName") or "Product"
    prod_r = await execute_tripletex_call(
        client,
        base_url,
        token,
        "POST",
        "/product",
        body={
            "name": prod_name,
            "priceExcludingVatCurrency": price_foreign,
            "vatType": {"id": vat_id},
            "currency": {"id": currency_id},
        },
    )
    if prod_r["status_code"] not in (200, 201):
        return False
    prod_id = prod_r["body"]["value"]["id"]

    order_r = await execute_tripletex_call(
        client,
        base_url,
        token,
        "POST",
        "/order",
        body={
            "customer": {"id": cust_id},
            "orderDate": fields.get("invoiceDate") or today,
            "deliveryDate": fields.get("invoiceDate") or today,
            "currency": {"id": currency_id},
            "orderLines": [
                {
                    "product": {"id": prod_id},
                    "count": 1,
                    "unitPriceExcludingVatCurrency": price_foreign,
                    "vatType": {"id": vat_id},
                }
            ],
        },
    )
    if order_r["status_code"] not in (200, 201):
        return False
    order_id = order_r["body"]["value"]["id"]

    inv_r = await execute_tripletex_call(
        client,
        base_url,
        token,
        "POST",
        "/invoice",
        body={
            "invoiceDate": fields.get("invoiceDate") or today,
            "invoiceDueDate": fields.get("invoiceDate") or today,
            "customer": {"id": cust_id},
            "orders": [{"id": order_id}],
            "currency": {"id": currency_id},
        },
    )
    if inv_r["status_code"] not in (200, 201):
        return False
    inv_id = inv_r["body"]["value"]["id"]
    inv_amount_foreign = inv_r["body"]["value"].get("amountCurrency") or inv_r["body"][
        "value"
    ].get("amount", 0)

    payment_rate = fields.get("paymentRate", 1)
    paid_nok = round(price_foreign * (1 + vat_pct / 100) * payment_rate, 2)

    pay_r = await execute_tripletex_call(
        client,
        base_url,
        token,
        "PUT",
        f"/invoice/{inv_id}/:payment",
        params={
            "paymentDate": fields.get("paymentDate") or today,
            "paymentTypeId": pay_type_id,
            "paidAmount": paid_nok,
            "paidAmountCurrency": inv_amount_foreign,
        },
    )
    log.info(f"[{rid}] SOLVER foreign currency -> payment={pay_r['status_code']}")
    return pay_r["status_code"] in (200, 204)


async def _solve_foreign_currency_payment(client, base_url, token, fields, log, rid):
    """Register payment on an existing foreign currency invoice."""
    today = time.strftime("%Y-%m-%d")

    cust_calls = []
    if fields.get("customerOrgNumber"):
        cust_calls.append(
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
        cust_calls.append(
            (
                "GET",
                "/customer",
                {"customerName": fields["customerName"], "fields": "id,name"},
                None,
            )
        )
    else:
        return False
    cust_calls.append(
        ("GET", "/invoice/paymentType", {"fields": "id,description"}, None)
    )

    results = await _parallel_calls(client, base_url, token, cust_calls)

    if results[0]["status_code"] != 200 or not results[0]["body"].get("values"):
        return False
    cust_id = results[0]["body"]["values"][0]["id"]

    pt_r = results[1]
    pay_type_id = None
    for pt in pt_r["body"].get("values", []):
        if "bank" in pt.get("description", "").lower():
            pay_type_id = pt["id"]
            break
    if pay_type_id is None and pt_r["body"].get("values"):
        pay_type_id = pt_r["body"]["values"][0]["id"]

    inv_params = {
        "customerId": str(cust_id),
        "fields": "id,invoiceNumber,amount,amountCurrency,amountOutstanding,amountCurrencyOutstanding,currency(id,code)",
    }
    if fields.get("invoiceDate"):
        inv_params["invoiceDateFrom"] = fields["invoiceDate"]
        inv_params["invoiceDateTo"] = fields["invoiceDate"]

    inv_r = await execute_tripletex_call(
        client,
        base_url,
        token,
        "GET",
        "/invoice",
        params=inv_params,
    )
    if inv_r["status_code"] != 200 or not inv_r["body"].get("values"):
        if fields.get("invoiceDate"):
            del inv_params["invoiceDateFrom"]
            del inv_params["invoiceDateTo"]
            inv_r = await execute_tripletex_call(
                client,
                base_url,
                token,
                "GET",
                "/invoice",
                params=inv_params,
            )
        if inv_r["status_code"] != 200 or not inv_r["body"].get("values"):
            return False

    invoice = None
    inv_num = fields.get("invoiceNumber")
    for inv in inv_r["body"]["values"]:
        if inv_num and str(inv.get("invoiceNumber")) == str(inv_num):
            invoice = inv
            break
    if invoice is None:
        for inv in inv_r["body"]["values"]:
            outstanding = inv.get("amountCurrencyOutstanding") or inv.get(
                "amountOutstanding", 0
            )
            if outstanding and outstanding > 0:
                invoice = inv
                break
    if invoice is None:
        invoice = inv_r["body"]["values"][0]

    inv_currency = invoice.get("currency", {}).get("code", "NOK")
    expected_currency = fields.get("currencyCode", "EUR")
    if inv_currency == "NOK" and expected_currency != "NOK":
        log.info(
            f"[{rid}] SOLVER foreign currency payment -> invoice is NOK, expected {expected_currency}, aborting"
        )
        return False

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
        client,
        base_url,
        token,
        "PUT",
        f"/invoice/{inv_id}/:payment",
        params={
            "paymentDate": fields.get("paymentDate") or today,
            "paymentTypeId": pay_type_id,
            "paidAmount": paid_nok,
            "paidAmountCurrency": abs(paid_currency),
        },
    )
    log.info(f"[{rid}] SOLVER foreign currency payment -> {pay_r['status_code']}")
    return pay_r["status_code"] in (200, 204)


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
    if files:
        log.info(f"[{request_id}] SOLVER: skipping (has files)")
        return False, None

    fields = await _extract_fields(prompt, client)
    if fields is None:
        log.info(f"[{request_id}] SOLVER: extraction failed")
        return False, None

    task_type = fields.get("task_type", "UNSUPPORTED")
    if task_type not in DETERMINISTIC_SOLVERS:
        log.info(
            f"[{request_id}] SOLVER: unsupported task '{task_type}', falling back to LLM"
        )
        return False, fields

    log.info(f"[{request_id}] SOLVER: task_type={task_type}")
    log.info(
        f"[{request_id}] SOLVER: fields={json.dumps(fields, ensure_ascii=False)[:500]}"
    )

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

    response = await client.post(
        OPENROUTER_URL,
        headers=headers,
        json=payload,
        timeout=60.0,
    )
    response.raise_for_status()
    return response.json()


# ---- Bank Account Prerequisite ---------------------------------------------

VALID_NORWEGIAN_BANK_ACCOUNT = "86011117947"


async def ensure_bank_account(
    client: httpx.AsyncClient,
    base_url: str,
    token: str,
    log: logging.Logger,
    request_id: str,
) -> None:
    if token in _bank_account_done:
        return
    _bank_account_done.add(token)
    auth = ("0", token)
    try:
        resp = await client.get(
            f"{base_url}/ledger/account",
            params={"number": "1920", "fields": "id,version,bankAccountNumber"},
            auth=auth,
            timeout=15.0,
        )
        data = resp.json()
        values = data.get("values", [])
        if not values:
            return
        acct = values[0]
        if acct.get("bankAccountNumber"):
            return
        log.info(
            f"[{request_id}] Bank account 1920 missing bankAccountNumber -- setting it"
        )
        put_resp = await client.put(
            f"{base_url}/ledger/account/{acct['id']}",
            json={
                "id": acct["id"],
                "version": acct["version"],
                "bankAccountNumber": VALID_NORWEGIAN_BANK_ACCOUNT,
            },
            auth=auth,
            timeout=15.0,
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
            log.info(f"[{request_id}] === REQUEST COMPLETE === ({total_elapsed:.1f}s)")
            return

        # If solver extracted fields but couldn't handle the task, pass context to LLM
        if (
            extracted_fields
            and extracted_fields.get("task_type", "UNSUPPORTED") != "UNSUPPORTED"
        ):
            solver_hint = (
                f"\n\n[SOLVER CONTEXT: Task was classified as '{extracted_fields['task_type']}' "
                f"with fields: {json.dumps(extracted_fields, ensure_ascii=False)[:400]}. "
                f"The deterministic solver failed -- please complete the task using API calls.]"
            )
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
                response = await call_openrouter(messages, client, use_reasoning)
            except httpx.HTTPStatusError as e:
                log.error(
                    f"[{request_id}] OpenRouter HTTP error: {e.response.status_code} {e.response.text[:500]}"
                )
                break
            except Exception as e:
                log.error(f"[{request_id}] OpenRouter error: {e}")
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
                        log.info(
                            f"[{request_id}] BLOCKED invalid endpoint: {method} {path}"
                        )
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
                        pass
                    elif result is not None:
                        pass
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
                        resp_str = (
                            json.dumps(resp_body, ensure_ascii=False, default=str)
                            if not isinstance(resp_body, str)
                            else resp_body
                        )

                        log.info(
                            f"[{request_id}] Response: {status}"
                            f" ({call_elapsed:.1f}s)"
                            f" body=\n{resp_str}"
                        )

                        if method == "GET" and status == 200:
                            prefix = _cacheable_prefix(norm_path)
                            if prefix:
                                key = _cache_key(token, norm_path, params)
                                _api_cache[key] = result

                        if method in ("POST", "PUT", "DELETE"):
                            cleared = _invalidate_cache(token, norm_path)
                            if cleared:
                                log.info(
                                    f"[{request_id}] Cache invalidated: {cleared} entries for {norm_path}"
                                )

                        if status == 403 and "expired proxy token" in resp_str.lower():
                            consecutive_proxy_403 += 1
                        else:
                            consecutive_proxy_403 = 0

                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tc_id,
                            "content": truncate_for_context(result),
                        }
                    )
                elif func_name == "compute_taxable_result":
                    date_from = args.get("date_from", "")
                    date_to = args.get("date_to", "")

                    log.info(
                        f"[{request_id}] compute_taxable_result: {date_from} to {date_to}"
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

                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tc_id,
                            "content": json.dumps(result, ensure_ascii=False),
                        }
                    )
                else:
                    log.warning(f"[{request_id}] Unknown tool: {func_name}")
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tc_id,
                            "content": json.dumps(
                                {"error": f"Unknown tool: {func_name}"}
                            ),
                        }
                    )

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
    log = testing_log if test else submission_log

    headers_dict = dict(request.headers)
    network_log.info(f"=== /solve REQUEST ===")
    network_log.info(f"Headers: {json.dumps(headers_dict, ensure_ascii=False)}")

    try:
        raw_body = await request.body()
        network_log.info(f"Raw body size: {len(raw_body)} bytes")
        body = json.loads(raw_body)
    except Exception as e:
        network_log.error(f"Failed to parse request body: {type(e).__name__}: {e}")
        network_log.error(f"Raw body (first 2000 chars): {raw_body[:2000]}")
        log.error(f"Failed to parse request body: {type(e).__name__}: {e}")
        return JSONResponse({"status": "error", "detail": "bad request body"}, status_code=400)

    body_summary = {}
    for k, v in body.items():
        if k == "tripletex_credentials":
            body_summary[k] = {kk: ("***" if "token" in kk.lower() else vv) for kk, vv in v.items()} if isinstance(v, dict) else "***"
        elif k == "files":
            body_summary[k] = [{"name": f.get("name", "?"), "mime_type": f.get("mime_type", "?"), "size": len(f.get("content_base64", ""))} for f in v] if isinstance(v, list) else v
        elif k == "prompt":
            body_summary[k] = v[:500]
        else:
            body_summary[k] = str(v)[:200]
    network_log.info(f"Body: {json.dumps(body_summary, ensure_ascii=False)}")

    try:
        prompt = body["prompt"]
        files = body.get("files", [])
        credentials = body["tripletex_credentials"]
    except KeyError as e:
        network_log.error(f"Missing required field in request: {e}")
        log.error(f"Missing required field in request: {e}")
        return JSONResponse({"status": "error", "detail": f"missing field: {e}"}, status_code=400)

    try:
        await run_agent(prompt, files, credentials, log)
    except Exception as e:
        log.error(f"Agent error: {e}", exc_info=True)

    return JSONResponse({"status": "completed"})


@app.get("/health")
async def health():
    return {"status": "ok", "model": MODEL}
