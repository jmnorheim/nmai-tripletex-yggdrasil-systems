"""Test deterministic solvers against the sandbox API."""
import asyncio
import json
import os
import sys
import time

import httpx
from dotenv import load_dotenv

load_dotenv()

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
BASE_URL = os.getenv("TRIPLETEX_BASE")
TOKEN = os.getenv("SESSION_TOKEN")
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

sys.path.insert(0, os.path.dirname(__file__))
from agent import (
    _extract_fields,
    execute_tripletex_call,
    DETERMINISTIC_SOLVERS,
)

import logging

log = logging.getLogger("test_solvers")
log.setLevel(logging.DEBUG)
log.addHandler(logging.StreamHandler(sys.stdout))

TEST_PROMPTS = [
    (
        "CREATE_DEPARTMENTS",
        'Opprett to avdelinger i Tripletex: "TestSalg" og "TestSupport".',
    ),
    (
        "CREATE_CUSTOMER",
        "Opprett en kunde: TestKunde AS, org.nr. 987654321, e-post test@testkunde.no, telefon 22334455.",
    ),
    (
        "CREATE_SUPPLIER",
        "Registrer en ny leverandør: TestLeverandør AS, org.nr. 112233445, epost faktura@testlev.no.",
    ),
    (
        "CREATE_PRODUCT",
        'Opprett et produkt "TestKonsulenttime" med pris 1500 kr eks. mva. (25% mva).',
    ),
    (
        "CREATE_EMPLOYEE",
        "Opprett en ansatt: Test Testesen, født 15.03.1990, e-post test.testesen@example.com, startdato 01.06.2026.",
    ),
]


async def test_extraction():
    print("\n" + "=" * 60)
    print("PHASE 1: Testing field extraction (LLM only, no API calls)")
    print("=" * 60 + "\n")

    results = {}
    async with httpx.AsyncClient() as client:
        for expected_type, prompt in TEST_PROMPTS:
            t0 = time.time()
            fields = await _extract_fields(prompt, client)
            elapsed = time.time() - t0

            if fields is None:
                print(f"FAIL [{expected_type}] extraction returned None ({elapsed:.1f}s)")
                print(f"  Prompt: {prompt}")
                results[expected_type] = False
                continue

            actual_type = fields.get("task_type", "???")
            match = actual_type == expected_type
            status = "OK" if match else "MISMATCH"

            print(f"{status} [{expected_type}] -> {actual_type} ({elapsed:.1f}s)")
            print(f"  Fields: {json.dumps(fields, ensure_ascii=False)}")
            results[expected_type] = match

    print(f"\nExtraction: {sum(results.values())}/{len(results)} correct\n")
    return results


async def test_solver_against_sandbox():
    print("\n" + "=" * 60)
    print("PHASE 2: Testing one solver against the sandbox")
    print("=" * 60 + "\n")

    prompt = 'Opprett to avdelinger i Tripletex: "SolverTest-A" og "SolverTest-B".'
    rid = "SOLVER-TEST"

    async with httpx.AsyncClient() as client:
        print(f"Extracting fields from: {prompt}")
        t0 = time.time()
        fields = await _extract_fields(prompt, client)
        extract_time = time.time() - t0
        print(f"  Extraction ({extract_time:.1f}s): {json.dumps(fields, ensure_ascii=False)}")

        if fields is None or fields.get("task_type") != "CREATE_DEPARTMENTS":
            print("  FAIL: wrong extraction")
            return False

        solver = DETERMINISTIC_SOLVERS["CREATE_DEPARTMENTS"]
        t1 = time.time()
        ok = await solver(client, BASE_URL, TOKEN, fields, log, rid)
        solve_time = time.time() - t1
        total = time.time() - t0

        print(f"\n  Solver result: {'SUCCESS' if ok else 'FAILED'}")
        print(f"  Extract: {extract_time:.1f}s | Solve: {solve_time:.1f}s | Total: {total:.1f}s")
        print(f"  (Compare: LLM agent loop for same task ~25s)")
        return ok


async def test_employee_solver():
    print("\n" + "=" * 60)
    print("PHASE 3: Testing employee solver against sandbox")
    print("=" * 60 + "\n")

    prompt = "Opprett en ansatt: SolverTest Person, født 22.08.1995, e-post solver.test@example.com, startdato 01.09.2026."
    rid = "EMP-TEST"

    async with httpx.AsyncClient() as client:
        print(f"Extracting fields from: {prompt}")
        t0 = time.time()
        fields = await _extract_fields(prompt, client)
        extract_time = time.time() - t0
        print(f"  Extraction ({extract_time:.1f}s): {json.dumps(fields, ensure_ascii=False)}")

        if fields is None or fields.get("task_type") != "CREATE_EMPLOYEE":
            print("  FAIL: wrong extraction")
            return False

        solver = DETERMINISTIC_SOLVERS["CREATE_EMPLOYEE"]
        t1 = time.time()
        ok = await solver(client, BASE_URL, TOKEN, fields, log, rid)
        solve_time = time.time() - t1
        total = time.time() - t0

        print(f"\n  Solver result: {'SUCCESS' if ok else 'FAILED'}")
        print(f"  Extract: {extract_time:.1f}s | Solve: {solve_time:.1f}s | Total: {total:.1f}s")
        print(f"  (Compare: LLM agent loop for same task ~50s)")
        return ok


async def main():
    if not all([OPENROUTER_API_KEY, BASE_URL, TOKEN]):
        print("ERROR: Missing .env credentials")
        return

    print(f"Sandbox: {BASE_URL}")
    print(f"Token: {TOKEN[:20]}...")

    await test_extraction()
    await test_solver_against_sandbox()
    await test_employee_solver()

    print("\n" + "=" * 60)
    print("All tests complete!")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
