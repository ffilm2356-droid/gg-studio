#!/usr/bin/env python3
"""Diagnostic test — run locally to debug auth issues."""

import hashlib
import json
import os
import ssl
import time
import urllib.request
from pathlib import Path


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
    print(f"URL: {url[:100]}")
    try:
        if data is not None:
            req = urllib.request.Request(url, data=json.dumps(data).encode(), method="POST")
        else:
            req = urllib.request.Request(url)
        for k, v in headers.items():
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


def main():
    print("=== GG Studio Auth Diagnostic ===\n")

    # Load cookies
    cookies = load_cookies()
    print(f"Cookies loaded: {len(cookies)}")
    important = ["SAPISID", "__Secure-1PSID", "__Secure-1PSIDTS", "SID", "HSID", "SSID",
                  "__Secure-3PSID", "__Secure-3PAPISID"]
    for name in important:
        val = cookies.get(name, "")
        status = f"{len(val)} chars" if val else "MISSING"
        print(f"  {name}: {status}")

    # Load API key
    api_key = load_api_key()
    print(f"\nAPI key: {'found (' + str(len(api_key)) + ' chars)' if api_key else 'MISSING'}")

    sapisid = cookies.get("SAPISID", "")
    cookie_str = "; ".join(f"{k}={v}" for k, v in cookies.items())

    # Test 1: Public Gemini API — list models (just verifies API key works)
    if api_key:
        test_request(
            f"https://generativelanguage.googleapis.com/v1beta/models?key={api_key}&pageSize=5",
            {"Content-Type": "application/json"},
            label="Test 1: Public API - list models (API key)",
        )

    # Test 2: MakerSuiteService on clients6.google.com with cookies only
    grpc_url = (
        "https://alkalimakersuite-pa.clients6.google.com"
        "/$rpc/google.internal.alkali.applications.makersuite.v1.MakerSuiteService"
        "/CheckUserStatus"
    )
    headers_cookies_only = {
        "Content-Type": "application/json",
        "Cookie": cookie_str,
        "Authorization": sapisidhash(sapisid) if sapisid else "",
        "Origin": "https://aistudio.google.com",
        "Referer": "https://aistudio.google.com/",
        "X-Goog-Authuser": "0",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0.0.0 Safari/537.36"
        ),
    }
    test_request(grpc_url, headers_cookies_only, data={},
                 label="Test 2: MakerSuiteService - cookies + SAPISIDHASH (no API key)")

    # Test 3: MakerSuiteService with cookies + API key
    if api_key:
        headers_full = {**headers_cookies_only, "X-Goog-Api-Key": api_key}
        test_request(grpc_url, headers_full, data={},
                     label="Test 3: MakerSuiteService - cookies + SAPISIDHASH + API key")

    # Test 4: Try public API for image gen (to check quota)
    if api_key:
        img_url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-image:generateContent?key={api_key}"
        img_body = {
            "contents": [{"parts": [{"text": "Generate a simple red circle on white background"}]}],
            "generationConfig": {"responseModalities": ["IMAGE", "TEXT"]},
        }
        result = test_request(img_url, {"Content-Type": "application/json"}, data=img_body,
                              label="Test 4: Public API - image gen (gemini-2.5-flash-image)")
        if result:
            print("\n*** IMAGE GENERATION WORKS ON PUBLIC API! ***")

    print("\n=== Diagnostic complete ===")


if __name__ == "__main__":
    main()
