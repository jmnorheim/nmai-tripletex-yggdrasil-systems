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
├── openapi.json          # Official Tripletex OpenAPI spec
├── requirements.txt      # Python deps: fastapi, httpx, pdfplumber, etc.
├── .env                  # OPENROUTER_API_KEY + sandbox credentials
├── docs/                 # Competition docs (overview, scoring, sandbox, etc.)
└── logs/                 # Runtime logs (submissions.log, testing.log)
```

## Architecture (single file: `agent.py`, ~1280 lines)

### Request Flow

```
POST /solve {prompt, files[], tripletex_credentials}
  │
  ├─ ensure_bank_account()          # Pre-set bank acct 1920 so invoices work
  │
  ├─ try_deterministic_solve()      # Fast path: 1 LLM call → fixed API sequence
  │    ├─ _extract_fields()         # LLM classifies task + extracts structured fields
  │    ├─ If task_type in DETERMINISTIC_SOLVERS:
  │    │    └─ Run solver function  # Fixed API calls, no LLM loop (~5-15s)
  │    └─ If unsupported or failed → fall through to LLM loop
  │
  ├─ process_files()                # PDF→text, images→base64, text→UTF-8
  │
  └─ Agent Loop (max 30 iterations, 270s timeout)
       ├─ call_openrouter()         # Claude Opus via OpenRouter (reasoning for Tier 3)
       ├─ If tool_calls:
       │    ├─ tripletex_api()      # HTTP to Tripletex with Basic Auth
       │    │    └─ Response cache for static lookups (vatType, accounts, etc.)
       │    └─ compute_taxable_result()  # Server-side posting aggregation
       └─ If no tool_calls → done
```

### Deterministic Solvers

9 task types bypass the LLM loop entirely. One LLM call extracts fields, then a hardcoded function executes the exact API sequence:

| Solver | Task | Typical Time |
|--------|------|-------------|
| `CREATE_DEPARTMENTS` | Create departments | ~3s |
| `CREATE_CUSTOMER` | Create customer | ~3s |
| `CREATE_SUPPLIER` | Create supplier | ~3s |
| `CREATE_PRODUCT` | Create product | ~3s |
| `CREATE_EMPLOYEE` | Create employee + employment | ~8s |
| `CREDIT_NOTE` | Credit note for existing invoice | ~10s |
| `CREATE_PROJECT` | Create project with PM/customer | ~8s |
| `SIMPLE_INVOICE` | Create + send invoice | ~10s |
| `REGISTER_PAYMENT` | Pay an existing invoice | ~10s |

Files attached → solvers skipped (fall through to LLM loop).

### Key Components

| Component | What it does |
|-----------|-------------|
| `SYSTEM_PROMPT` | Inlines `api_reference.md` + `task_reference.md` + critical rules |
| `SOLVER_EXTRACTION_PROMPT` | Schema for extracting structured fields from prompts |
| `execute_tripletex_call()` | HTTP to Tripletex proxy with Basic Auth `(0, token)` |
| `call_openrouter()` | Chat completion to Claude Opus via OpenRouter |
| `_api_cache` | Caches static GET responses (vatType, accounts, voucherType, etc.) |
| `is_tier3_task()` | Keyword detection → enables extended reasoning for complex tasks |
| `compute_result_from_postings()` | Fetches all postings and aggregates by account group |

### Config

- **Model:** `anthropic/claude-opus-4-6` via OpenRouter (used for both LLM loop and solver extraction)
- **Max iterations:** 30
- **Timeout:** 270s (5-min deadline minus 30s buffer)
- **Response truncation:** 8000 chars

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
