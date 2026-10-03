#!/usr/bin/env python3
"""Diagnostic test — run locally to debug auth issues.

Tests multiple auth strategies to find what works:
  1. Public API with API key (list models)
  2. OAuth2 token from AI Studio page (extract embedded token)
  3. OAuth2 authorization code flow (silent, via cookies)
  4. MakerSuiteService with OAuth2 Bearer token
  5. MakerSuiteService with cookies + SAPISIDHASH (legacy)
  6. Public API image generation (check quota)
"""

import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))


def load_cookies():
    with open("accounts.json") as f:
        data = json.load(f)

    if isinstance(data, list):
        entry = data[0]
    elif "accounts" in data:
        entry = data["accounts"][0]
    elif "url" in data and "cookies" in data:
        entry = {"cookies": data["cookies"]}
    else:
        entry = data

    cookies = entry.get("cookies", {})
    if isinstance(cookies, list) and cookies and isinstance(cookies[0], dict):
        cookies = {c["name"]: c["value"] for c in cookies}
    return cookies


def load_api_key():
    key = os.environ.get("AISTUDIO_API_KEY", "").strip()
    if key:
        return key
    env_path = Path(".env")
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line.startswith("AISTUDIO_API_KEY="):
                return line.split("=", 1)[1].strip()
    return ""


def sapisidhash(sapisid, origin="https://aistudio.google.com"):
    ts = int(time.time())
    h = hashlib.sha1(f"{ts} {sapisid} {origin}".encode()).hexdigest()
    return f"SAPISIDHASH {ts}_{h}"


def test_request(url, headers, data=None, label=""):
    print(f"\n--- {label} ---")
    print(f"URL: {url[:120]}")
    try:
        if data is not None:
            req = urllib.request.Request(url, data=json.dumps(data).encode(), method="POST")
        else:
            req = urllib.request.Request(url)
        for k, v in headers.items():
            if v:
                req.add_header(k, v)

        resp = urllib.request.urlopen(req, timeout=15)
        body = resp.read().decode()
        print(f"OK! Status: {resp.status}")
        print(f"Body: {body[:300]}")
        return True
    except urllib.error.HTTPError as e:
        print(f"HTTP {e.code}: {e.reason}")
        body = e.read().decode()[:400]
        print(f"Body: {body}")
        return False
    except Exception as e:
        print(f"Error: {e}")
        return False


def try_extract_token_from_page(cookies):
    """Strategy 1: Fetch AI Studio page and look for embedded access token."""
    print("\n--- Test 2: Extract OAuth2 token from AI Studio page ---")
    cookie_str = "; ".join(f"{k}={v}" for k, v in cookies.items())
    try:
        req = urllib.request.Request("https://aistudio.google.com/")
        req.add_header("Cookie", cookie_str)
        req.add_header("User-Agent",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0.0.0 Safari/537.36")

        resp = urllib.request.urlopen(req, timeout=30)
        html = resp.read().decode("utf-8", errors="replace")
        print(f"Page loaded: {len(html)} bytes, status {resp.status}")

        for pat in [
            r'"(ya29\.[a-zA-Z0-9_.-]{20,})"',
            r"'(ya29\.[a-zA-Z0-9_.-]{20,})'",
        ]:
            m = re.search(pat, html)
            if m:
                token = m.group(1)
                print(f"FOUND token: ya29.***{token[-10:]} ({len(token)} chars)")
                return token

        print("No embedded token found in page HTML")
        if "accounts.google.com/ServiceLogin" in html:
            print("  Page redirected to login — cookies may be expired!")
        return None
    except Exception as e:
        print(f"Error: {e}")
        return None


def try_oauth2_code_flow(cookies):
    """Strategy 2: Silent OAuth2 code flow via accounts.google.com."""
    print("\n--- Test 3: OAuth2 silent auth code flow ---")
    cookie_str = "; ".join(f"{k}={v}" for k, v in cookies.items())

    client_id = (
        "764086051850-6qr4p6gpi6hn506pt8ejuq83di341hur"
        ".apps.googleusercontent.com"
    )
    params = urllib.parse.urlencode({
        "client_id": client_id,
        "scope": (
            "https://www.googleapis.com/auth/cloud-platform "
            "https://www.googleapis.com/auth/generative-language"
        ),
        "response_type": "code",
        "redirect_uri": "urn:ietf:wg:oauth:2.0:oob",
        "prompt": "none",
        "access_type": "offline",
    })
    url = f"https://accounts.google.com/o/oauth2/auth?{params}"

    class RedirectTracker(urllib.request.HTTPRedirectHandler):
        def __init__(self):
            self.redirects = []
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            self.redirects.append((code, newurl))
            print(f"  Redirect {code} -> {newurl[:100]}...")
            return None

    tracker = RedirectTracker()
    opener = urllib.request.build_opener(tracker)

    try:
        req = urllib.request.Request(url)
        req.add_header("Cookie", cookie_str)
        req.add_header("User-Agent",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0.0.0 Safari/537.36")

        resp = opener.open(req, timeout=30)
        html = resp.read().decode("utf-8", errors="replace")
        print(f"Response: {len(html)} bytes")

        # Look for auth code
        code_match = (
            re.search(r">(\s*4/[a-zA-Z0-9_-]+)\s*<", html)
            or re.search(r'value="(4/[a-zA-Z0-9_-]+)"', html)
            or re.search(r"(4/[a-zA-Z0-9_-]{20,})", html)
        )
        if code_match:
            code = code_match.group(1).strip()
            print(f"FOUND auth code: {code[:10]}...")
            return exchange_code_for_token(code, client_id)

        if "error" in html.lower():
            err_match = re.search(r'"error"\s*:\s*"([^"]+)"', html)
            if err_match:
                print(f"OAuth error: {err_match.group(1)}")
            elif "consent" in html.lower():
                print("Consent required — run interactive setup first")
        else:
            print(f"Page snippet: {html[:200]}")

    except urllib.error.HTTPError as e:
        if e.code in (301, 302, 303, 307, 308):
            location = e.headers.get("Location", "")
            print(f"  Final redirect: {location[:120]}")
            parsed = urllib.parse.urlparse(location)
            qs = urllib.parse.parse_qs(parsed.query)
            if "code" in qs:
                print(f"FOUND auth code in redirect!")
                return exchange_code_for_token(qs["code"][0], client_id)
            frag = urllib.parse.parse_qs(parsed.fragment)
            if "access_token" in frag:
                token = frag["access_token"][0]
                print(f"FOUND token in redirect ({len(token)} chars)")
                return token
        else:
            print(f"HTTP {e.code}: {e.reason}")
            print(f"Body: {e.read().decode()[:300]}")

    for code, rurl in tracker.redirects:
        parsed = urllib.parse.urlparse(rurl)
        qs = urllib.parse.parse_qs(parsed.query)
        if "code" in qs:
            print(f"FOUND auth code in redirect chain!")
            return exchange_code_for_token(qs["code"][0], client_id)

    print("No auth code obtained")
    return None


def exchange_code_for_token(code, client_id):
    """Exchange authorization code for access token."""
    print(f"  Exchanging code for token...")
    client_secret = "d-FL95Q19q7MQmFpd7hHD0Ty"
    data = urllib.parse.urlencode({
        "client_id": client_id,
        "client_secret": client_secret,
        "code": code,
        "grant_type": "authorization_code",
        "redirect_uri": "urn:ietf:wg:oauth:2.0:oob",
    }).encode()

    req = urllib.request.Request(
        "https://oauth2.googleapis.com/token", data=data, method="POST"
    )
    req.add_header("Content-Type", "application/x-www-form-urlencoded")

    try:
        resp = urllib.request.urlopen(req, timeout=15)
        result = json.loads(resp.read().decode())
        token = result.get("access_token", "")
        print(f"  Token obtained ({len(token)} chars)")
        if "refresh_token" in result:
            print(f"  Refresh token also obtained")
        return token
    except urllib.error.HTTPError as e:
        print(f"  Token exchange failed: HTTP {e.code}")
        print(f"  {e.read().decode()[:300]}")
        return None


def main():
    print("=== GG Studio Auth Diagnostic v2 ===\n")

    cookies = load_cookies()
    print(f"Cookies loaded: {len(cookies)}")
    important = ["SAPISID", "__Secure-1PSID", "__Secure-1PSIDTS", "SID", "HSID", "SSID",
                  "__Secure-3PSID", "__Secure-3PAPISID"]
    for name in important:
        val = cookies.get(name, "")
        status = f"{len(val)} chars" if val else "MISSING"
        print(f"  {name}: {status}")

    api_key = load_api_key()
    print(f"\nAPI key: {'found (' + str(len(api_key)) + ' chars)' if api_key else 'MISSING'}")

    sapisid = cookies.get("SAPISID", "") or cookies.get("__Secure-3PAPISID", "")
    cookie_str = "; ".join(f"{k}={v}" for k, v in cookies.items())

    # Test 1: Public API key validation
    if api_key:
        test_request(
            f"https://generativelanguage.googleapis.com/v1beta/models?key={api_key}&pageSize=3",
            {"Content-Type": "application/json"},
            label="Test 1: Public API - list models (API key)",
        )

    # Test 2: Extract token from AI Studio page
    oauth_token = try_extract_token_from_page(cookies)

    # Test 3: OAuth2 silent code flow
    if not oauth_token:
        oauth_token = try_oauth2_code_flow(cookies)

    # Test 4: Use OAuth2 token with MakerSuiteService
    grpc_url = (
        "https://alkalimakersuite-pa.clients6.google.com"
        "/$rpc/google.internal.alkali.applications.makersuite.v1.MakerSuiteService"
        "/CheckUserStatus"
    )

    if oauth_token:
        test_request(
            grpc_url,
            {
                "Authorization": f"Bearer {oauth_token}",
                "Content-Type": "application/json",
                "X-Goog-Authuser": "0",
            },
            data={},
            label="Test 4: MakerSuiteService with OAuth2 Bearer token",
        )
    else:
        print("\n--- Test 4: SKIPPED (no OAuth2 token obtained) ---")

    # Test 5: Legacy cookies + SAPISIDHASH (for comparison)
    headers_legacy = {
        "Content-Type": "application/json",
        "Cookie": cookie_str,
        "Authorization": sapisidhash(sapisid) if sapisid else "",
        "Origin": "https://aistudio.google.com",
        "Referer": "https://aistudio.google.com/",
        "X-Goog-Authuser": "0",
        "X-Goog-Ext-353267353-Jspb": "",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0.0.0 Safari/537.36"
        ),
    }
    test_request(grpc_url, headers_legacy, data={},
                 label="Test 5: MakerSuiteService - cookies + SAPISIDHASH (legacy)")

    # Test 6: Public API image gen (quota check)
    if api_key:
        img_url = (
            "https://generativelanguage.googleapis.com/v1beta/"
            f"models/gemini-2.5-flash-image:generateContent?key={api_key}"
        )
        img_body = {
            "contents": [{"parts": [{"text": "Generate a red circle"}]}],
            "generationConfig": {"responseModalities": ["IMAGE", "TEXT"]},
        }
        test_request(img_url, {"Content-Type": "application/json"}, data=img_body,
                     label="Test 6: Public API - image gen quota check")

    # Summary
    print("\n" + "=" * 50)
    print("=== SUMMARY ===")
    if oauth_token:
        print("OAuth2 token: ACQUIRED")
        print("  -> MakerSuiteService should work with Bearer auth")
    else:
        print("OAuth2 token: NOT ACQUIRED")
        print("  -> Run one-time setup: python setup_oauth.py")
    print("=" * 50)


if __name__ == "__main__":
    main()
