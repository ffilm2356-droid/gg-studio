"""Cookie-based authentication and account management."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

from .models import Account

logger = logging.getLogger("gg_studio.auth")

REQUIRED_COOKIES = ["__Secure-1PSID", "__Secure-1PSIDTS"]

GOOGLE_DOMAINS = [
    ".google.com",
    ".aistudio.google.com",
    "aistudio.google.com",
    "generativelanguage.googleapis.com",
]


def load_accounts(path: str = "accounts.json") -> list[Account]:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(
            f"Account file not found: {path}. "
            f"Copy accounts.example.json to accounts.json and fill in your cookies."
        )

    with open(p) as f:
        data = json.load(f)

    accounts: list[Account] = []
    for i, entry in enumerate(data):
        name = entry.get("name", f"account-{i + 1}")
        cookies = entry.get("cookies", {})

        missing = [c for c in REQUIRED_COOKIES if c not in cookies or not cookies[c]]
        if missing:
            logger.warning(
                "Account '%s' missing required cookies: %s — skipping",
                name,
                ", ".join(missing),
            )
            continue

        accounts.append(
            Account(
                name=name,
                cookies=cookies,
                proxy=entry.get("proxy"),
            )
        )

    logger.info("Loaded %d accounts from %s", len(accounts), path)
    return accounts


def build_cookie_header(account: Account) -> str:
    return "; ".join(f"{k}={v}" for k, v in account.cookies.items())


def get_auth_headers(account: Account) -> dict[str, str]:
    return {
        "Cookie": build_cookie_header(account),
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0.0.0 Safari/537.36"
        ),
        "Origin": "https://aistudio.google.com",
        "Referer": "https://aistudio.google.com/",
        "X-Goog-Ext-353267353-Jspb": "",
        "Content-Type": "application/json",
    }


async def refresh_sid_token(account: Account, session) -> Optional[str]:
    """Attempt to refresh the 1PSIDTS token using the rotate endpoint."""
    try:
        headers = get_auth_headers(account)
        async with session.post(
            "https://accounts.google.com/RotateCookies",
            headers=headers,
            json={},
            timeout=10,
        ) as resp:
            if resp.status == 200:
                for cookie in resp.cookies.values():
                    if cookie.key in account.cookies:
                        account.cookies[cookie.key] = cookie.value
                logger.info("Refreshed tokens for account '%s'", account.name)
                return account.cookies.get("__Secure-1PSIDTS")
    except Exception as e:
        logger.debug("Token refresh failed for '%s': %s", account.name, e)
    return None
