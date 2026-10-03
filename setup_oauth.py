#!/usr/bin/env python3
"""One-time OAuth2 setup for GG Studio.

Run this once to authorize the tool to use Google AI Studio's API.
It will open a browser window — sign in with the same Google account
you use for AI Studio, then authorize the app.

The refresh token is saved to .oauth_token.json for automatic renewal.
"""

import json
import os
import socket
import sys
import threading
import urllib.parse
import urllib.request
import webbrowser
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

CLIENT_ID = (
    "764086051850-6qr4p6gpi6hn506pt8ejuq83di341hur"
    ".apps.googleusercontent.com"
)
CLIENT_SECRET = "d-FL95Q19q7MQmFpd7hHD0Ty"
SCOPES = "https://www.googleapis.com/auth/cloud-platform"


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _OAuthCallbackHandler(BaseHTTPRequestHandler):
    auth_code: str | None = None
    error: str | None = None

    def do_GET(self):
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        if "code" in qs:
            _OAuthCallbackHandler.auth_code = qs["code"][0]
            self._respond("Authorization successful! You can close this tab.")
        elif "error" in qs:
            _OAuthCallbackHandler.error = qs.get("error", ["unknown"])[0]
            self._respond(f"Authorization failed: {_OAuthCallbackHandler.error}")
        else:
            self._respond("No authorization code received.")

    def _respond(self, message: str):
        body = f"<html><body><h2>{message}</h2><p>Return to the terminal.</p></body></html>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(body.encode())

    def log_message(self, format, *args):
        pass


def main():
    print("=== GG Studio OAuth2 Setup ===\n")

    port = _find_free_port()
    redirect_uri = f"http://127.0.0.1:{port}"

    params = urllib.parse.urlencode({
        "client_id": CLIENT_ID,
        "scope": SCOPES,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "access_type": "offline",
        "prompt": "consent",
    })
    auth_url = f"https://accounts.google.com/o/oauth2/auth?{params}"

    server = HTTPServer(("127.0.0.1", port), _OAuthCallbackHandler)
    server_thread = threading.Thread(target=server.handle_request, daemon=True)
    server_thread.start()

    print("Opening browser for Google sign-in...")
    print(f"If it doesn't open, visit this URL manually:\n")
    print(f"  {auth_url}\n")

    try:
        webbrowser.open(auth_url)
    except Exception:
        pass

    print("Sign in with the Google account you use for AI Studio.")
    print("Waiting for authorization...\n")

    server_thread.join(timeout=300)
    server.server_close()

    code = _OAuthCallbackHandler.auth_code
    error = _OAuthCallbackHandler.error

    if error:
        print(f"Authorization failed: {error}")
        sys.exit(1)

    if not code:
        print("Timed out waiting for authorization (5 minutes).")
        print(f"You can also visit the URL manually:\n  {auth_url}")
        sys.exit(1)

    print("Authorization code received! Exchanging for tokens...")

    data = urllib.parse.urlencode({
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "code": code,
        "grant_type": "authorization_code",
        "redirect_uri": redirect_uri,
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
        print("Refresh token saved to .oauth_token.json")
        print("(The tool will auto-refresh tokens from now on)")
    else:
        print("WARNING: No refresh token — you may need to re-run this later")

    print("\nValidating token...")

    # Test 1: GCP project detection (cloud-platform scope)
    rm_url = "https://cloudresourcemanager.googleapis.com/v1/projects?pageSize=5"
    try:
        req = urllib.request.Request(rm_url)
        req.add_header("Authorization", f"Bearer {access_token}")
        resp = urllib.request.urlopen(req, timeout=15)
        body = resp.read().decode()
        data = json.loads(body)
        projects = data.get("projects", [])
        if projects:
            names = [p.get("projectId", "") for p in projects[:3]]
            print(f"GCP projects found: {', '.join(names)}")
            gcp_project = None
            for p in projects:
                if "generative" in p.get("projectId", "").lower():
                    gcp_project = p["projectId"]
                    break
            if not gcp_project:
                gcp_project = projects[0].get("projectId", "")
            print(f"Will use project: {gcp_project} (for Vertex AI)")
        else:
            print("WARNING: No GCP projects found.")
            print("Create one at console.cloud.google.com for Vertex AI support.")
    except Exception as e:
        print(f"GCP project check failed: {e}")

    # Test 2: API key listing (if available)
    api_key = os.environ.get("AISTUDIO_API_KEY", "") or os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        env_path = Path(".env")
        if env_path.exists():
            for line in env_path.read_text().splitlines():
                if line.startswith("AISTUDIO_API_KEY="):
                    api_key = line.split("=", 1)[1].strip()
                    break
    if api_key:
        try:
            list_url = f"https://generativelanguage.googleapis.com/v1beta/models?key={api_key}&pageSize=3"
            req = urllib.request.Request(list_url)
            resp = urllib.request.urlopen(req, timeout=15)
            body = resp.read().decode()
            data = json.loads(body)
            models = [m.get("displayName", m.get("name", "")) for m in data.get("models", [])]
            print(f"API key OK! Models: {', '.join(models[:3])}...")
        except Exception as e:
            print(f"API key check failed: {e}")

    print(f"\n=== Setup complete! ===")
    print("Auth strategy: API key for listing + Vertex AI (OAuth2) for generation")
    print("Run 'python test_local.py' to test image/video generation.")


if __name__ == "__main__":
    main()
