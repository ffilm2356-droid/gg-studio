"""OAuth2 token acquisition from Google session cookies.

The MakerSuiteService gRPC API requires OAuth2 access tokens —
cookies + SAPISIDHASH alone are rejected. This module converts
the user's browser cookies into a usable access token.

Strategies (tried in order):
  1. Token refresh using a cached refresh_token (.oauth_token.json)
  2. Extract embedded token from AI Studio page HTML
  3. OAuth2 authorization code flow via localhost redirect
"""

from __future__ import annotations

import json
import logging
import re
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import Optional

logger = logging.getLogger("gg_studio.oauth")

_GCLOUD_CLIENT_ID = (
    "764086051850-6qr4p6gpi6hn506pt8ejuq83di341hur"
    ".apps.googleusercontent.com"
)
_GCLOUD_CLIENT_SECRET = "d-FL95Q19q7MQmFpd7hHD0Ty"

_SCOPES = "https://www.googleapis.com/auth/cloud-platform"

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

_TOKEN_CACHE = Path(".oauth_token.json")


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def __init__(self):
        self.redirects: list[str] = []

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        self.redirects.append(newurl)
        return None


class _SilentCallbackHandler(BaseHTTPRequestHandler):
    auth_code: str | None = None

    def do_GET(self):
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        if "code" in qs:
            _SilentCallbackHandler.auth_code = qs["code"][0]
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(b"<html><body>OK</body></html>")

    def log_message(self, format, *args):
        pass


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
        """Silent OAuth2 authorization code flow using session cookies.

        Uses a localhost redirect URI. Starts a temporary local HTTP server
        to capture the authorization code from Google's redirect.
        """
        port = _find_free_port()
        redirect_uri = f"http://127.0.0.1:{port}"

        _SilentCallbackHandler.auth_code = None
        server = HTTPServer(("127.0.0.1", port), _SilentCallbackHandler)

        params = urllib.parse.urlencode({
            "client_id": _GCLOUD_CLIENT_ID,
            "scope": _SCOPES,
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "prompt": "none",
            "access_type": "offline",
        })
        url = f"https://accounts.google.com/o/oauth2/auth?{params}"

        handler = _NoRedirect()
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
                handler.redirects.append(location)
                html = ""
            else:
                server.server_close()
                raise

        for redirect_url in handler.redirects:
            parsed = urllib.parse.urlparse(redirect_url)
            qs = urllib.parse.parse_qs(parsed.query)
            if "code" in qs:
                server.server_close()
                return self._exchange_code(qs["code"][0], redirect_uri)
            if "error" in qs:
                logger.debug("OAuth2 error: %s", qs["error"][0])
                server.server_close()
                return None
            if redirect_url.startswith(redirect_uri):
                server_thread = threading.Thread(
                    target=server.handle_request, daemon=True
                )
                server_thread.start()
                try:
                    follow_req = urllib.request.Request(redirect_url)
                    urllib.request.urlopen(follow_req, timeout=10)
                except Exception:
                    pass
                server_thread.join(timeout=5)
                if _SilentCallbackHandler.auth_code:
                    server.server_close()
                    return self._exchange_code(
                        _SilentCallbackHandler.auth_code, redirect_uri
                    )

        server.server_close()
        return None

    def _exchange_code(self, code: str, redirect_uri: str = "http://127.0.0.1") -> str:
        data = urllib.parse.urlencode({
            "client_id": _GCLOUD_CLIENT_ID,
            "client_secret": _GCLOUD_CLIENT_SECRET,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
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
