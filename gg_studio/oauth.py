"""OAuth2 token acquisition from Google session cookies.

The MakerSuiteService gRPC API requires OAuth2 access tokens —
cookies + SAPISIDHASH alone are rejected. This module converts
the user's browser cookies into a usable access token.

Strategies (tried in order):
  1. Extract embedded token from AI Studio page HTML
  2. OAuth2 authorization code flow via accounts.google.com
  3. Token refresh using a cached refresh_token
"""

from __future__ import annotations

import json
import logging
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Optional

logger = logging.getLogger("gg_studio.oauth")

_GCLOUD_CLIENT_ID = (
    "764086051850-6qr4p6gpi6hn506pt8ejuq83di341hur"
    ".apps.googleusercontent.com"
)
_GCLOUD_CLIENT_SECRET = "d-FL95Q19q7MQmFpd7hHD0Ty"

_SCOPES = (
    "https://www.googleapis.com/auth/cloud-platform "
    "https://www.googleapis.com/auth/generative-language"
)

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

_TOKEN_CACHE = Path(".oauth_token.json")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def __init__(self):
        self.redirects: list[str] = []

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        self.redirects.append(newurl)
        return None


class OAuthTokenManager:
    def __init__(self, cookies: dict[str, str]):
        self.cookies = cookies
        self._access_token: Optional[str] = None
        self._refresh_token: Optional[str] = None
        self._expiry: float = 0
        self._load_cache()

    @property
    def _cookie_str(self) -> str:
        return "; ".join(f"{k}={v}" for k, v in self.cookies.items())

    def get_token(self) -> Optional[str]:
        if self._access_token and time.time() < self._expiry:
            return self._access_token

        if self._refresh_token:
            try:
                return self._refresh()
            except Exception as e:
                logger.debug("Refresh failed: %s", e)

        for strategy in [self._from_aistudio_page, self._oauth2_code_flow]:
            try:
                token = strategy()
                if token:
                    return token
            except Exception as e:
                logger.debug("%s failed: %s", strategy.__name__, e)

        return None

    def _from_aistudio_page(self) -> Optional[str]:
        """Fetch AI Studio page and look for an embedded access token."""
        req = urllib.request.Request("https://aistudio.google.com/")
        req.add_header("Cookie", self._cookie_str)
        req.add_header("User-Agent", _UA)

        resp = urllib.request.urlopen(req, timeout=30)
        html = resp.read().decode("utf-8", errors="replace")

        patterns = [
            r'"(ya29\.[a-zA-Z0-9_.-]{20,})"',
            r"'(ya29\.[a-zA-Z0-9_.-]{20,})'",
        ]
        for pat in patterns:
            m = re.search(pat, html)
            if m:
                token = m.group(1)
                self._set_token(token, expires_in=3000)
                logger.info("Extracted token from AI Studio page")
                return token

        logger.debug("No token in page (%d bytes)", len(html))
        return None

    def _oauth2_code_flow(self) -> Optional[str]:
        """Silent OAuth2 authorization code flow using session cookies."""
        params = urllib.parse.urlencode({
            "client_id": _GCLOUD_CLIENT_ID,
            "scope": _SCOPES,
            "response_type": "code",
            "redirect_uri": "urn:ietf:wg:oauth:2.0:oob",
            "prompt": "none",
            "access_type": "offline",
        })
        url = f"https://accounts.google.com/o/oauth2/auth?{params}"

        handler = _NoRedirect()
        handler.redirects = []
        opener = urllib.request.build_opener(handler)

        req = urllib.request.Request(url)
        req.add_header("Cookie", self._cookie_str)
        req.add_header("User-Agent", _UA)

        try:
            resp = opener.open(req, timeout=30)
            html = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            if e.code in (301, 302, 303, 307, 308):
                location = e.headers.get("Location", "")
                parsed = urllib.parse.urlparse(location)
                qs = urllib.parse.parse_qs(parsed.query)
                if "code" in qs:
                    return self._exchange_code(qs["code"][0])
                frag = urllib.parse.parse_qs(parsed.fragment)
                if "access_token" in frag:
                    token = frag["access_token"][0]
                    self._set_token(token, expires_in=3600)
                    return token
                req2 = urllib.request.Request(location)
                req2.add_header("Cookie", self._cookie_str)
                req2.add_header("User-Agent", _UA)
                resp = urllib.request.urlopen(req2, timeout=30)
                html = resp.read().decode("utf-8", errors="replace")
            else:
                raise

        for redirect_url in handler.redirects:
            parsed = urllib.parse.urlparse(redirect_url)
            qs = urllib.parse.parse_qs(parsed.query)
            if "code" in qs:
                return self._exchange_code(qs["code"][0])
            frag = urllib.parse.parse_qs(parsed.fragment)
            if "access_token" in frag:
                token = frag["access_token"][0]
                self._set_token(token, expires_in=3600)
                return token

        code_match = (
            re.search(r">(\s*4/[a-zA-Z0-9_-]+)\s*<", html)
            or re.search(r'value="(4/[a-zA-Z0-9_-]+)"', html)
            or re.search(r"(4/[a-zA-Z0-9_-]{20,})", html)
        )
        if code_match:
            return self._exchange_code(code_match.group(1).strip())

        if "approval_code" in html or "success" in html.lower():
            logger.debug("Auth page loaded but no code found")

        return None

    def _exchange_code(self, code: str) -> str:
        data = urllib.parse.urlencode({
            "client_id": _GCLOUD_CLIENT_ID,
            "client_secret": _GCLOUD_CLIENT_SECRET,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": "urn:ietf:wg:oauth:2.0:oob",
        }).encode()

        req = urllib.request.Request(
            "https://oauth2.googleapis.com/token", data=data, method="POST"
        )
        req.add_header("Content-Type", "application/x-www-form-urlencoded")

        resp = urllib.request.urlopen(req, timeout=15)
        result = json.loads(resp.read().decode())

        self._set_token(
            result["access_token"],
            expires_in=result.get("expires_in", 3600),
        )
        if "refresh_token" in result:
            self._refresh_token = result["refresh_token"]
            self._save_cache()

        return self._access_token  # type: ignore[return-value]

    def _refresh(self) -> str:
        data = urllib.parse.urlencode({
            "client_id": _GCLOUD_CLIENT_ID,
            "client_secret": _GCLOUD_CLIENT_SECRET,
            "refresh_token": self._refresh_token,
            "grant_type": "refresh_token",
        }).encode()

        req = urllib.request.Request(
            "https://oauth2.googleapis.com/token", data=data, method="POST"
        )
        req.add_header("Content-Type", "application/x-www-form-urlencoded")

        resp = urllib.request.urlopen(req, timeout=15)
        result = json.loads(resp.read().decode())

        self._set_token(
            result["access_token"],
            expires_in=result.get("expires_in", 3600),
        )
        return self._access_token  # type: ignore[return-value]

    def _set_token(self, token: str, expires_in: int = 3600):
        self._access_token = token
        self._expiry = time.time() + expires_in - 60

    def _save_cache(self):
        if not self._refresh_token:
            return
        try:
            _TOKEN_CACHE.write_text(json.dumps({
                "refresh_token": self._refresh_token,
            }))
        except OSError:
            pass

    def _load_cache(self):
        try:
            if _TOKEN_CACHE.exists():
                data = json.loads(_TOKEN_CACHE.read_text())
                self._refresh_token = data.get("refresh_token")
        except (OSError, json.JSONDecodeError):
            pass


def get_oauth2_token_interactive(cookies: dict[str, str]) -> str:
    """Interactive OAuth2 flow — opens a URL for user to authorize.

    Use this when the silent flow fails (first-time setup).
    Returns the access token.
    """
    params = urllib.parse.urlencode({
        "client_id": _GCLOUD_CLIENT_ID,
        "scope": _SCOPES,
        "response_type": "code",
        "redirect_uri": "urn:ietf:wg:oauth:2.0:oob",
        "access_type": "offline",
    })
    url = f"https://accounts.google.com/o/oauth2/auth?{params}"

    print("\n=== One-time OAuth2 Setup ===")
    print("Open this URL in your browser (where you're signed into Google):")
    print()
    print(f"  {url}")
    print()
    code = input("Paste the authorization code here: ").strip()

    mgr = OAuthTokenManager(cookies)
    return mgr._exchange_code(code)
