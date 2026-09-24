"""Telegram delivery for the daily drift report.

Credentials deliberately live OUTSIDE this repository: Github_Repo_Push is public,
so the token is read from the runtime data directory's sibling `.env`
(`~/.hermes/profiles/orchestrator/github_repo_push/.env`) or from the environment.

Transport mirrors Alpaca_Paper_Trader/telegram_notifier.py, which is the path
proven in production on this machine. If `requests` fails (this Mac has a TLS
interception issue in some contexts), we fall back to curl rather than ever
disabling certificate verification.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Optional

API_BASE = "https://api.telegram.org"


def _load_env_file(path: Path) -> dict[str, str]:
    """Parse a KEY=VALUE .env file. Missing file yields an empty mapping."""
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip("'\"")
    return values


def resolve_credentials(data_dir: Path) -> tuple[Optional[str], Optional[str]]:
    """Find (token, chat_id). Environment wins; the .env beside data_dir is the store."""
    env_file = _load_env_file(data_dir.parent / ".env")
    token = (
        os.getenv("GHRP_TELEGRAM_BOT_TOKEN")
        or env_file.get("GHRP_TELEGRAM_BOT_TOKEN")
        or env_file.get("TELEGRAM_BOT_TOKEN")
    )
    chat = (
        os.getenv("GHRP_TELEGRAM_CHAT_ID")
        or env_file.get("GHRP_TELEGRAM_CHAT_ID")
        or env_file.get("TELEGRAM_CHAT_ID")
    )
    return token, chat


def send(text: str, data_dir: Path) -> bool:
    """Send one message. Returns False (quietly) when unconfigured."""
    token, chat = resolve_credentials(data_dir)
    if not (token and chat):
        return False

    url = f"{API_BASE}/bot{token}/sendMessage"
    payload = {"chat_id": chat, "text": text, "disable_web_page_preview": True}

    try:
        import requests

        response = requests.post(url, json=payload, timeout=15)
        if response.status_code == 200:
            return True
        print(f"[telegram] API error {response.status_code}: {response.text[:200]}")
    except Exception as e:
        print(f"[telegram] requests failed ({e}); falling back to curl")

    try:
        result = subprocess.run(
            ["curl", "-sS", "-X", "POST", url,
             "-H", "Content-Type: application/json",
             "-d", json.dumps(payload)],
            capture_output=True, text=True, timeout=20,
        )
        return result.returncode == 0 and '"ok":true' in result.stdout
    except Exception as e:
        print(f"[telegram] curl fallback failed: {e}")
        return False


def format_reconcile(report) -> str:
    """Render a ReconcileReport as plain text."""
    if report.error:
        return f"Registry check: {report.error}"
    if report.is_clean:
        return "Registry matches your GitHub account — nothing orphaned, nothing unregistered."

    lines = ["Registry check:"]
    if report.orphaned:
        lines.append("  Gone from GitHub but still in ghrp's list:")
        lines += [f"    - {name}" for name in report.orphaned]
    if report.unregistered:
        lines.append("  On GitHub but not in ghrp's list:")
        lines += [f"    - {name}" for name in report.unregistered]
    return "\n".join(lines)


def format_report(entries, pushed_only: bool = False) -> str:
    """Render drift entries as a plain-text Telegram message."""
    if not entries:
        return "ghrp drift: no repos registered."

    attention = [e for e in entries if e.needs_attention]
    pushed = [e for e in entries if e.pushed]
    lines = [f"ghrp drift — {len(entries)} repos"]

    if pushed:
        lines.append("")
        lines.append("Pushed:")
        lines += [f"  {e.repo}: {e.summary()}" for e in pushed]

    noteworthy = [e for e in attention if not e.pushed]
    if noteworthy:
        lines.append("")
        lines.append("Needs you:")
        lines += [f"  {e.repo}: {e.summary()}" for e in noteworthy]

    if not pushed and not noteworthy:
        lines.append("")
        lines.append("All clean — nothing to push, nothing uncommitted.")

    clean = len(entries) - len(attention) - len(pushed)
    lines.append("")
    lines.append(f"{len(pushed)} pushed · {len(noteworthy)} need attention · {max(clean, 0)} clean")
    return "\n".join(lines)
