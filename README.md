# GG Studio — Google Flow & Veo API Launcher

Direct API-based image and video generation using Google AI models.
No browser required. Pure API calls with OAuth2 + cookie authentication.

## Features

- **Pure API** — No hidden browser, no Selenium, no Playwright
- **OAuth2 auth** — Auto-acquires OAuth2 tokens from session cookies
- **Multi-account** — Load multiple Google accounts from `accounts.json`
- **Batch processing** — Queue hundreds of prompts with concurrent workers
- **Rate limiting** — Configurable RPM per model type (image/video)
- **Models** — Imagen 4, Imagen 3, Narwhal Flash, Gemini 3 Pro, Gemini 3.1 Flash, Veo 3, Veo 2, Veo 3.1 Lite
- **Proxy/VPN** — Built-in SOCKS5/HTTP proxy support per account
- **Aspect ratios** — 16:9, 9:16, 1:1, 4:3, 3:4
- **Auto-retry** — Configurable retry with exponential backoff
- **CLI + Server** — Run as CLI tool or HTTP API server

## Quick Start

```bash
pip install -r requirements.txt

# 1. Configure accounts (cookies)
cp accounts.example.json accounts.json
# Edit accounts.json with your Google cookies

# 2. One-time OAuth2 setup
python setup_oauth.py

# 3. Generate!
python -m gg_studio generate --prompt "a cat in space" --model imagen4 --ratio 16:9

# Batch from file
python -m gg_studio batch --input prompts.txt --model narwhal --workers 30

# HTTP API server
python -m gg_studio serve --port 8080
```

## Account Setup

### 1. Export cookies

Extract cookies from your logged-in Google account (with Gemini Advanced / Ultra subscription).

Use a browser extension like "EditThisCookie" or "Cookie-Editor" to export cookies for `aistudio.google.com`.

Required cookies: `__Secure-1PSID`, `__Secure-1PSIDTS`

Important cookies for auth: `SAPISID`, `__Secure-3PAPISID`, `SID`, `HSID`, `SSID`

### 2. OAuth2 setup (required for MakerSuiteService)

The MakerSuiteService gRPC API (used for Imagen 4, Veo 3, etc.) requires OAuth2 access tokens. Run the setup once:

```bash
python setup_oauth.py
```

This opens a browser URL for Google sign-in. After authorizing, paste the code back. The refresh token is saved to `.oauth_token.json` for automatic renewal.

Alternatively, the tool will try to auto-acquire tokens from your cookies (silent OAuth2 flow).

### 3. Diagnostics

```bash
python test_debug.py    # Test all auth strategies
python test_local.py    # Test actual image/video generation
```

## License

Private use only.
