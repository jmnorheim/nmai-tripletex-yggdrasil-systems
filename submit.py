#!/usr/bin/env python3
"""
Automated submission + network traffic inspector for NM i AI Tripletex.

Usage:
    python submit.py <ngrok_url>
    python submit.py <ngrok_url> --repeat 5 --delay 10
    python submit.py --poll-only
    python submit.py --leaderboard
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import httpx

import discord_notify as discord

API_BASE = "https://api.ainm.no"
TASK_ID = "cccccccc-cccc-cccc-cccc-cccccccccccc"

JWT_TOKEN = os.getenv(
    "AINM_TOKEN",
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiJlYWU3NzM5Ny04YTBmLTRlYjAtYjZmOS1lNGZlNGFiYWE0MTYiLCJlbWFpbCI6Imptbm9yaGVpbUBnbWFpbC5jb20iLCJpc19hZG1pbiI6ZmFsc2UsImV4cCI6MTc3NDY1MTQ5OX0.bNhH1FCBzdPM_DDJAsyERW8EhYjDzLonBk1XTmPy1h8",
)

LOG_DIR = Path(__file__).parent / "logs"
LOG_DIR.mkdir(exist_ok=True)
TRAFFIC_LOG = LOG_DIR / "network_traffic.log"


def _ts() -> str:
    return datetime.now().strftime("%H:%M:%S")


def _log(msg: str, *, also_print: bool = True):
    line = f"[{_ts()}] {msg}"
    with open(TRAFFIC_LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")
    if also_print:
        print(line)


def _log_separator():
    _log("=" * 90)


def _build_client() -> httpx.Client:
    return httpx.Client(
        base_url=API_BASE,
        cookies={"token": JWT_TOKEN},
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {JWT_TOKEN}",
        },
        timeout=30,
        follow_redirects=True,
    )


def _log_response(resp: httpx.Response, label: str):
    _log(f"--- {label} ---")
    _log(f"  URL:    {resp.request.method} {resp.request.url}")

    req_body = resp.request.content
    if req_body:
        try:
            _log(f"  REQ BODY: {json.dumps(json.loads(req_body), indent=2)}")
        except Exception:
            _log(f"  REQ BODY: {req_body[:500]}")

    _log(f"  STATUS: {resp.status_code} {resp.reason_phrase}")
    _log(f"  RESP HEADERS: {dict(resp.headers)}")

    try:
        body = resp.json()
        _log(f"  RESP BODY: {json.dumps(body, indent=2)}")
    except Exception:
        text = resp.text[:2000]
        _log(f"  RESP TEXT: {text}")

    return resp


def submit(client: httpx.Client, endpoint_url: str, api_key: str | None = None) -> dict | None:
    _log_separator()
    _log(f"SUBMITTING endpoint: {endpoint_url}")

    payload = {"endpoint_url": endpoint_url, "endpoint_api_key": api_key}
    resp = client.post(f"/tasks/{TASK_ID}/submissions", json=payload)
    _log_response(resp, "SUBMIT")

    if resp.status_code != 200 and resp.status_code != 201:
        _log(f"  *** SUBMISSION FAILED: {resp.status_code} ***")
        return None

    data = resp.json()
    if "daily_submissions_used" in data:
        _log(f"  Daily submissions: {data['daily_submissions_used']}/{data.get('daily_submissions_max', '?')}")
    return data


def poll_submissions(client: httpx.Client, *, wait_for_completion: bool = False, last_id: str | None = None) -> list:
    resp = client.get("/tripletex/my/submissions")
    _log_response(resp, "POLL SUBMISSIONS")

    if resp.status_code != 200:
        _log(f"  *** POLL FAILED ***")
        return []

    submissions = resp.json()
    active = [s for s in submissions if s.get("status") in ("queued", "processing", "scoring")]
    completed = [s for s in submissions if s.get("status") not in ("queued", "processing", "scoring")]

    _log(f"  Active: {len(active)}  |  Completed: {len(completed)}  |  Total: {len(submissions)}")

    if wait_for_completion and active:
        _log(f"  Waiting for {len(active)} active submission(s) to finish...")
        while True:
            time.sleep(5)
            resp2 = client.get("/tripletex/my/submissions")
            if resp2.status_code != 200:
                continue
            subs2 = resp2.json()
            still_active = [s for s in subs2 if s.get("status") in ("queued", "processing", "scoring")]
            if not still_active:
                _log(f"  All submissions finished!")
                _log_separator()
                _print_results(subs2, last_id=last_id)
                if last_id:
                    match = next((s for s in subs2 if s.get("id") == last_id), None)
                    if match:
                        discord.notify_result(match)
                return subs2
            _log(f"  ... still {len(still_active)} active, waiting 5s ...")

    _print_results(submissions, last_id=last_id)
    return submissions


def _print_results(submissions: list, *, last_id: str | None = None):
    _log_separator()
    _log("SUBMISSION RESULTS SUMMARY")
    _log_separator()

    for i, s in enumerate(submissions[:20]):
        sid = s.get("id", "?")
        status = s.get("status", "?")
        score_raw = s.get("score_raw")
        score_max = s.get("score_max")
        duration = s.get("duration_ms")
        fail_reason = s.get("fail_reason")
        feedback = s.get("feedback") or {}
        comment = feedback.get("comment", "")
        checks = feedback.get("checks", [])
        queued_at = s.get("queued_at", "")

        marker = " <<<< NEW" if last_id and sid == last_id else ""

        score_str = f"{score_raw}/{score_max}" if score_raw is not None else "N/A"
        pct = f" ({score_raw/score_max*100:.0f}%)" if score_raw is not None and score_max else ""
        dur_str = f" {duration/1000:.1f}s" if duration else ""

        _log(f"  [{i+1:2d}] {status:12s} | Score: {score_str:8s}{pct:6s} | {dur_str:6s} | {queued_at[:19]}{marker}")

        if fail_reason:
            _log(f"       FAIL REASON: {fail_reason}")

        if comment:
            _log(f"       COMMENT: {comment}")

        if checks:
            for chk in checks:
                tag = "OK" if "OK" in chk else "FAIL"
                _log(f"       [{tag:4s}] {chk}")

        # Log ALL fields for forensics
        extra_keys = set(s.keys()) - {"id", "status", "score_raw", "score_max", "duration_ms",
                                       "fail_reason", "feedback", "queued_at"}
        if extra_keys:
            extras = {k: s[k] for k in sorted(extra_keys)}
            _log(f"       EXTRA FIELDS: {json.dumps(extras, default=str)}")

    _log_separator()


def fetch_leaderboard(client: httpx.Client):
    _log_separator()
    _log("FETCHING LEADERBOARD")
    resp = client.get("/tripletex/leaderboard")
    _log_response(resp, "LEADERBOARD")

    if resp.status_code != 200:
        return

    teams = resp.json()
    _log(f"\n  {'Rank':>4s}  {'Score':>7s}  {'Tasks':>5s}  {'Subs':>5s}  Team")
    _log(f"  {'----':>4s}  {'-----':>7s}  {'-----':>5s}  {'----':>5s}  ----")
    for t in teams[:30]:
        _log(f"  {t.get('rank', '?'):>4}  {t.get('total_score', 0):>7.1f}  {t.get('tasks_touched', 0):>5}  {t.get('total_submissions', 0):>5}  {t.get('team_name', '?')}")
    _log_separator()
    discord.notify_leaderboard(teams)


def fetch_team(client: httpx.Client):
    resp = client.get("/teams/my")
    _log_response(resp, "MY TEAM")
    return resp.json() if resp.status_code == 200 else None


def fetch_sandbox(client: httpx.Client):
    resp = client.get("/tripletex/sandbox")
    _log_response(resp, "SANDBOX INFO")
    return resp.json() if resp.status_code == 200 else None


def probe_endpoints(client: httpx.Client):
    """Try various API endpoints to discover additional info."""
    _log_separator()
    _log("PROBING ADDITIONAL ENDPOINTS")

    endpoints = [
        ("GET", "/users/me"),
        ("GET", "/teams/my"),
        ("GET", "/tripletex/sandbox"),
        ("GET", "/tripletex/leaderboard"),
        ("GET", "/finals/status"),
    ]

    for method, path in endpoints:
        try:
            if method == "GET":
                resp = client.get(path)
            _log_response(resp, f"PROBE {method} {path}")
        except Exception as e:
            _log(f"  PROBE {method} {path} -> ERROR: {e}")

    _log_separator()


def main():
    parser = argparse.ArgumentParser(description="NM i AI Tripletex auto-submitter")
    parser.add_argument("endpoint_url", nargs="?", help="Your ngrok /solve endpoint URL")
    parser.add_argument("--api-key", default=None, help="Optional API key")
    parser.add_argument("--repeat", type=int, default=1, help="Number of submissions")
    parser.add_argument("--delay", type=int, default=15, help="Seconds between repeated submissions")
    parser.add_argument("--poll-only", action="store_true", help="Just poll existing submissions")
    parser.add_argument("--leaderboard", action="store_true", help="Fetch leaderboard")
    parser.add_argument("--probe", action="store_true", help="Probe all known endpoints")
    parser.add_argument("--wait", action="store_true", help="Wait for submission to complete before returning")
    args = parser.parse_args()

    _log_separator()
    _log(f"NM i AI Tripletex Submission Tool started")
    _log(f"Traffic log: {TRAFFIC_LOG}")
    _log_separator()

    client = _build_client()

    if args.probe:
        probe_endpoints(client)
        return

    if args.leaderboard:
        fetch_leaderboard(client)
        return

    if args.poll_only:
        poll_submissions(client)
        return

    if not args.endpoint_url:
        parser.error("endpoint_url is required unless using --poll-only, --leaderboard, or --probe")

    url = args.endpoint_url.rstrip("/")
    if not url.endswith("/solve"):
        url = url.rstrip("/") + "/solve"
        _log(f"  Auto-appended /solve -> {url}")

    discord.notify(f"Starting {args.repeat} submission(s) to `{url}`")

    for i in range(args.repeat):
        if i > 0:
            _log(f"  Waiting {args.delay}s before next submission...")
            time.sleep(args.delay)

        _log(f"\n  === Submission {i+1}/{args.repeat} ===")
        discord.notify_submission_started(i + 1, args.repeat, url)
        result = submit(client, url, args.api_key)

        if result and args.wait:
            last_id = result.get("id")
            time.sleep(3)
            poll_submissions(client, wait_for_completion=True, last_id=last_id)

    if not args.wait:
        _log("\nFinal poll of all submissions:")
        time.sleep(2)
        poll_submissions(client)

    _log(f"\nDone. Full traffic log at: {TRAFFIC_LOG}")


if __name__ == "__main__":
    main()
