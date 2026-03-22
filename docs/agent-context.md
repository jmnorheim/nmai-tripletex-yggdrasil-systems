# Tripletex AI Accounting Agent — Quick Context

## What This Is

A competition agent that receives accounting task prompts (in 7 languages) via a `/solve` HTTP endpoint, uses an LLM to interpret them, and completes them by calling the Tripletex v2 REST API. Scored on correctness (field-by-field checks) and efficiency (fewer API calls = higher score).

## Repo Layout

```
tripletex-nmai/
├── agent.py              # Entire agent — FastAPI app, solvers, LLM loop, API executor
├── api_reference.md      # Abridged Tripletex API ref (injected into system prompt)
├── task_reference.md     # Known task types + optimal API call sequences
├── api_reference_full.json  # Full endpoint list (3400+ lines)
├── api_comparison.md     # Endpoint comparison notes
├── openapi.json          # Official Tripletex OpenAPI spec
├── requirements.txt      # Python deps: fastapi, httpx, pdfplumber, etc.
├── submit.py             # Submission helper script
├── run_3_submissions.py  # Batch submission runner
├── discord_notify.py     # Discord notification helper
├── test_solvers.py       # Solver test harness
├── .env                  # OPENROUTER_API_KEY + sandbox credentials
├── docs/                 # Competition docs (overview, scoring, sandbox, etc.)
│   └── tripletex-api-guide.md  # Tripletex API usage guide
└── logs/                 # Runtime logs (submissions.log, testing.log)
```

## Architecture (single file: `agent.py`, ~4100 lines)

### Request Flow

```
POST /solve {prompt, files[], tripletex_credentials}
  │
  ├─ process_files()                # PDF→text, images→base64, text→UTF-8
  │
  ├─ try_deterministic_solve()      # Fast path: 1 LLM call → fixed API sequence
  │    ├─ If files attached → skip (fall through to LLM loop)
  │    ├─ _extract_fields()         # LLM classifies task + extracts structured fields
  │    ├─ If task_type in DETERMINISTIC_SOLVERS:
  │    │    ├─ ensure_bank_account()  # Only for tasks needing bank (conditional)
  │    │    └─ Run solver function    # Fixed API calls, no LLM loop (~5-15s)
  │    └─ If unsupported or failed → fall through to LLM loop
  │
  ├─ ensure_bank_account()          # Pre-set bank acct 1920 (before LLM fallback)
  │
  └─ Agent Loop (max 25 iterations, 180s timeout)
       ├─ call_openrouter()         # Claude Sonnet via OpenRouter (reasoning for Tier 3)
       ├─ If tool_calls:
       │    ├─ tripletex_api()      # HTTP to Tripletex with Basic Auth
       │    │    └─ Response cache for static lookups (vatType, accounts, etc.)
       │    └─ compute_taxable_result()  # Server-side posting aggregation
       └─ If no tool_calls → done
```

### Deterministic Solvers

22 task types bypass the LLM loop entirely. One LLM call (Claude Sonnet, fast) extracts fields, then a hardcoded function executes the exact API sequence with parallel calls where possible:

| Solver | Task | Tier |
|--------|------|------|
| `CREATE_DEPARTMENTS` | Create departments | T1 |
| `CREATE_CUSTOMER` | Create customer | T1 |
| `CREATE_SUPPLIER` | Create supplier | T1 |
| `CREATE_PRODUCT` | Create product | T1 |
| `CREATE_EMPLOYEE` | Create employee + employment | T1 |
| `CREDIT_NOTE` | Credit note for existing invoice | T1 |
| `CREATE_PROJECT` | Create project with PM/customer | T1 |
| `SIMPLE_INVOICE` | Create + send invoice | T1 |
| `REGISTER_PAYMENT` | Pay an existing invoice | T1 |
| `REGISTER_SUPPLIER_INVOICE` | Supplier invoice voucher | T2 |
| `PAYROLL_RUN` | Salary transaction | T2 |
| `CUSTOM_DIMENSION` | Dimension + linked voucher | T2 |
| `ORDER_INVOICE_PAYMENT` | Order → invoice → payment | T2 |
| `REVERSE_PAYMENT` | Reverse a bank payment | T2 |
| `TRAVEL_EXPENSE` | Travel with costs + per diem | T2 |
| `MULTI_VAT_INVOICE` | Invoice with mixed VAT rates | T2 |
| `FIXED_PRICE_PROJECT` | Fixed-price project + invoice | T2 |
| `TIME_TRACKING` | Timesheet entries + invoice | T2 |
| `FOREIGN_CURRENCY_INVOICE` | Foreign currency + agio (new invoice) | T2 |
| `FOREIGN_CURRENCY_PAYMENT` | Payment on existing foreign invoice | T2 |
| `COST_ANALYSIS` | Per-account cost breakdown | T3 |
| `LEDGER_CORRECTION` | Ledger error correction via voucher | T3 |

Files attached → solvers skipped (fall through to LLM loop).
Solver failure → extracted fields passed as context hint to LLM loop.

### Key Components

| Component | What it does |
|-----------|-------------|
| `SYSTEM_PROMPT` | Inlines `api_reference.md` + `task_reference.md` + critical rules |
| `SOLVER_EXTRACTION_PROMPT` | Schema for extracting structured fields from prompts |
| `execute_tripletex_call()` | HTTP to Tripletex proxy with Basic Auth `(0, token)` |
| `call_openrouter()` | Chat completion to Claude Sonnet via OpenRouter |
| `_api_cache` | Caches static GET responses (vatType, accounts, voucherType, etc.) |
| `is_tier3_task()` | Keyword detection → enables extended reasoning for complex tasks |
| `compute_result_from_postings()` | Fetches all postings and aggregates by account group |

### Config

- **Model:** `anthropic/claude-sonnet-4` via OpenRouter (both LLM loop and solver extraction; LLM loop model overridable via `AGENT_MODEL` env var)
- **Max iterations:** 25
- **Timeout:** 180s
- **Response truncation:** 3000 chars (compressed: only essential fields kept)

## Task Structure

30 task types across 3 tiers (multiplier affects score):

- **Tier 1 (×1):** Single-entity CRUD — employee, customer, product, supplier, department, simple invoice, project, register payment
- **Tier 2 (×2):** Multi-step workflows — multi-VAT invoice, order→invoice→payment, payroll, travel expense, credit note, fixed-price project, time tracking, dimensions, reverse payment, project lifecycle
- **Tier 3 (×3):** Complex accounting — month-end closing, year-end closing, expense from receipt/PDF, bank reconciliation, error correction, cost analysis

Each task has 56 variants (7 languages × 8 datasets). Fresh sandbox per submission.

## Scoring

- Field-by-field correctness checks (e.g. 10 points for employee fields)
- Tier multiplier applied to base score
- Efficiency bonus for minimal API calls and zero 4xx errors
- Best score per task kept on rolling leaderboard
- Range: 0.0 (failed) to 6.0 (perfect Tier 3 + best efficiency)

## How to Run

```bash
cd tripletex-nmai
pip install -r requirements.txt
# Set OPENROUTER_API_KEY in .env
uvicorn agent:app --host 0.0.0.0 --port 8000
```

Expose via HTTPS (e.g. ngrok), then submit URL at https://app.ainm.no/submit/tripletex.
