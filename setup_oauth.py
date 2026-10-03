#!/usr/bin/env python3
"""One-time OAuth2 setup for GG Studio.

Run this once to authorize the tool to use Google AI Studio's API.
It will open a URL — sign in with the same Google account you use
for AI Studio, then paste the authorization code back here.

The refresh token is saved to .oauth_token.json for automatic renewal.
"""

import json
import sys
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path

CLIENT_ID = (
    "764086051850-6qr4p6gpi6hn506pt8ejuq83di341hur"
    ".apps.googleusercontent.com"
)
CLIENT_SECRET = "d-FL95Q19q7MQmFpd7hHD0Ty"
SCOPES = (
    "https://www.googleapis.com/auth/cloud-platform "
    "https://www.googleapis.com/auth/generative-language"
)


def main():
    print("=== GG Studio OAuth2 Setup ===\n")

    params = urllib.parse.urlencode({
        "client_id": CLIENT_ID,
        "scope": SCOPES,
        "response_type": "code",
        "redirect_uri": "urn:ietf:wg:oauth:2.0:oob",
        "access_type": "offline",
    })
    auth_url = f"https://accounts.google.com/o/oauth2/auth?{params}"

    print("Opening browser for Google sign-in...")
    print(f"If it doesn't open, visit this URL manually:\n")
    print(f"  {auth_url}\n")

    try:
        webbrowser.open(auth_url)
    except Exception:
        pass

    print("Sign in with the Google account you use for AI Studio.")
    print("After authorizing, you'll see an authorization code.\n")

    code = input("Paste the authorization code here: ").strip()
    if not code:
        print("No code entered. Aborting.")
        sys.exit(1)

    print("\nExchanging code for tokens...")

    data = urllib.parse.urlencode({
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
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
    except Exception as e:
        print(f"\nFailed: {e}")
        sys.exit(1)

    access_token = result.get("access_token", "")
    refresh_token = result.get("refresh_token", "")

    if not access_token:
        print(f"\nNo access token in response: {result}")
        sys.exit(1)

    print(f"\nAccess token obtained ({len(access_token)} chars)")

    if refresh_token:
        cache = {"refresh_token": refresh_token}
        Path(".oauth_token.json").write_text(json.dumps(cache))
        print(f"Refresh token saved to .oauth_token.json")
        print("(The tool will auto-refresh tokens from now on)")
    else:
        print("WARNING: No refresh token — you may need to re-run this later")

    # Quick validation
    print("\nValidating token with MakerSuiteService...")
    grpc_url = (
        "https://alkalimakersuite-pa.clients6.google.com"
        "/$rpc/google.internal.alkali.applications.makersuite.v1.MakerSuiteService"
        "/CheckUserStatus"
    )
    try:
        req = urllib.request.Request(
            grpc_url, data=b"{}", method="POST"
        )
        req.add_header("Authorization", f"Bearer {access_token}")
        req.add_header("Content-Type", "application/json")
        req.add_header("X-Goog-Authuser", "0")

        resp = urllib.request.urlopen(req, timeout=15)
        body = resp.read().decode()
        print(f"CheckUserStatus: OK! ({len(body)} bytes)")
        print(f"Response: {body[:200]}")
        print("\n=== Setup complete! Token is working. ===")
    except Exception as e:
        print(f"Validation failed: {e}")
        print("The token was obtained but may not have the right permissions.")
        print("Try running test_debug.py to see more details.")


if __name__ == "__main__":
    main()
