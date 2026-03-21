"""
Discord webhook notifications for submission results.

Usage as a module:
    from discord_notify import notify, notify_result

    notify("Agent is starting up")
    notify_result(submission_dict)

Usage standalone:
    python discord_notify.py "Your message here"
"""

import json
import os
import sys
from datetime import datetime

import httpx
from dotenv import load_dotenv

load_dotenv()

WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL")


def notify(message: str, *, username: str = "Tripletex Bot") -> bool:
    """Send a plain text message to Discord. Returns True on success."""
    if not WEBHOOK_URL:
        return False
    try:
        resp = httpx.post(
            WEBHOOK_URL,
            json={"content": message, "username": username},
            timeout=10,
        )
        return resp.status_code == 204
    except Exception:
        return False


def notify_embed(
    title: str,
    description: str = "",
    color: int = 0x5865F2,
    fields: list[dict] | None = None,
    *,
    username: str = "Tripletex Bot",
) -> bool:
    """Send a rich embed message to Discord."""
    if not WEBHOOK_URL:
        return False

    embed = {"title": title, "color": color, "timestamp": datetime.utcnow().isoformat()}
    if description:
        embed["description"] = description
    if fields:
        embed["fields"] = fields

    try:
        resp = httpx.post(
            WEBHOOK_URL,
            json={"username": username, "embeds": [embed]},
            timeout=10,
        )
        return resp.status_code == 204
    except Exception:
        return False


def notify_submission_started(submission_num: int, total: int, endpoint: str) -> bool:
    return notify_embed(
        f"Submission {submission_num}/{total} started",
        f"Endpoint: `{endpoint}`",
        color=0xFEE75C,
    )


def notify_result(s: dict, *, submission_num: int | None = None, total: int | None = None) -> bool:
    """Post a formatted submission result to Discord."""
    status = s.get("status", "?")
    score_raw = s.get("score_raw")
    score_max = s.get("score_max")
    duration = s.get("duration_ms")
    fail_reason = s.get("fail_reason")
    feedback = s.get("feedback") or {}
    comment = feedback.get("comment", "")
    checks = feedback.get("checks", [])

    if score_raw is not None and score_max:
        pct = score_raw / score_max * 100
        score_str = f"{score_raw}/{score_max} ({pct:.0f}%)"
    else:
        score_str = "N/A"

    dur_str = f"{duration / 1000:.1f}s" if duration else "N/A"

    is_success = status == "completed" and score_raw is not None
    color = 0x57F287 if is_success else 0xED4245

    header = "Submission Result"
    if submission_num and total:
        header = f"Submission {submission_num}/{total} Result"

    fields = [
        {"name": "Status", "value": status, "inline": True},
        {"name": "Score", "value": score_str, "inline": True},
        {"name": "Duration", "value": dur_str, "inline": True},
    ]

    if fail_reason:
        fields.append({"name": "Fail Reason", "value": fail_reason, "inline": False})

    if comment:
        fields.append({"name": "Comment", "value": comment[:1024], "inline": False})

    if checks:
        passed = sum(1 for c in checks if "passed" in c.lower() or "OK" in c)
        failed = len(checks) - passed
        check_summary = f"{passed} passed, {failed} failed"
        fields.append({"name": "Checks", "value": check_summary, "inline": True})

        failing = [c for c in checks if "passed" not in c.lower() and "OK" not in c]
        if failing:
            detail = "\n".join(f"- {c}" for c in failing[:10])
            fields.append({"name": "Failing Checks", "value": detail[:1024], "inline": False})

    return notify_embed(header, color=color, fields=fields)


def notify_leaderboard(teams: list, *, top_n: int = 10) -> bool:
    """Post leaderboard standings to Discord."""
    lines = []
    for t in teams[:top_n]:
        rank = t.get("rank", "?")
        score = t.get("total_score", 0)
        name = t.get("team_name", "?")
        lines.append(f"**#{rank}** {name} — {score:.1f} pts")

    return notify_embed(
        "Leaderboard",
        "\n".join(lines),
        color=0x5865F2,
    )


if __name__ == "__main__":
    if len(sys.argv) > 1:
        msg = " ".join(sys.argv[1:])
        ok = notify(msg)
        print(f"Sent: {ok}")
    else:
        print('Usage: python discord_notify.py "your message"')
