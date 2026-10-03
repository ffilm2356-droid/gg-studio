# GG Studio — Google Flow & Veo API Launcher

Direct API-based image and video generation using Google AI models.
No browser required. Pure API calls with cookie authentication.

## Features

- **Pure API** — No hidden browser, no Selenium, no Playwright
- **Multi-account** — Load multiple Google accounts from `accounts.json`
- **Batch processing** — Queue hundreds of prompts with concurrent workers
- **Rate limiting** — Configurable RPM per model type (image/video)
- **Models** — Narwhal (Flash), Gemini 3.0 Pro, Imagen 4.0, Imagen 3, Veo
- **Proxy/VPN** — Built-in SOCKS5/HTTP proxy support per account
- **Aspect ratios** — 16:9, 9:16, 1:1, 4:3, 3:4
- **Auto-retry** — Configurable retry on failure
- **CLI + Server** — Run as CLI tool or HTTP API server

## Quick Start

```bash
pip install -r requirements.txt

# Configure accounts
cp accounts.example.json accounts.json
# Edit accounts.json with your Google cookies

# CLI - single generation
python -m gg_studio generate --prompt "a cat in space" --model imagen4 --ratio 16:9

# CLI - batch from file
python -m gg_studio batch --input prompts.txt --model narwhal --workers 30

# HTTP API server
python -m gg_studio serve --port 8080
```

## Account Setup

Extract cookies from your logged-in Google account (with Gemini Advanced / Ultra subscription).

Use a browser extension like "EditThisCookie" or "Cookie-Editor" to export cookies for `aistudio.google.com`.

Required cookies: `__Secure-1PSID`, `__Secure-1PSIDTS`, `__Secure-1PSIDCC`

## License

Private use only.
