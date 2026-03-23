#!/usr/bin/env python3
"""Wait for current submission to finish, then run 3 sequential submissions."""
import httpx
import time
import os

API_BASE = "https://api.ainm.no"
TASK_ID = "cccccccc-cccc-cccc-cccc-cccccccccccc"
NGROK = "https://1db2-2a05-ec0-2000-113-b909-60c-2a79-a0e1.ngrok-free.app/solve"

client = httpx.Client(
    base_url=API_BASE,
    cookies={"token": JWT},
    headers={"Content-Type": "application/json", "Authorization": f"Bearer {JWT}"},
    timeout=30,
    follow_redirects=True,
)


def has_active():
    r = client.get("/tripletex/my/submissions")
    subs = r.json()
    return any(s["status"] in ("queued", "processing", "scoring") for s in subs)


def wait_done():
    while has_active():
        print(f'  [{time.strftime("%H:%M:%S")}] Still processing... waiting 10s', flush=True)
        time.sleep(10)
    print(f'  [{time.strftime("%H:%M:%S")}] No active submissions.', flush=True)


def submit_and_wait(n):
    print(f"\n=== Submission {n}/3 ===", flush=True)
    r = client.post(f"/tasks/{TASK_ID}/submissions", json={"endpoint_url": NGROK})
    data = r.json()
    sid = data.get("id", "?")
    print(f'  [{time.strftime("%H:%M:%S")}] Submitted! ID={sid} status={r.status_code}', flush=True)
    if "daily_submissions_used" in data:
        print(f'  Daily: {data["daily_submissions_used"]}/{data.get("daily_submissions_max", "?")}', flush=True)
    time.sleep(5)
    wait_done()

    r2 = client.get("/tripletex/my/submissions")
    subs = r2.json()
    for s in subs:
        if s.get("id") == sid:
            score = s.get("score_raw")
            mx = s.get("score_max")
            ns = s.get("normalized_score")
            dur = s.get("duration_ms")
            pct = f"{score/mx*100:.0f}%" if score is not None and mx else "N/A"
            dur_str = f" | {dur/1000:.1f}s" if dur else ""
            print(f"  Result: {score}/{mx} ({pct}) | normalized={ns}{dur_str}", flush=True)
            fb = s.get("feedback", {})
            if fb.get("comment"):
                print(f'  Comment: {fb["comment"]}', flush=True)
            for chk in fb.get("checks", []):
                tag = "PASS" if "passed" in chk.lower() else "FAIL"
                print(f"    [{tag}] {chk}", flush=True)
            break


print("Waiting for current submission to finish...", flush=True)
wait_done()

for i in range(1, 4):
    submit_and_wait(i)

print("\nAll 3 submissions complete!", flush=True)
