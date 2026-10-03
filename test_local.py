#!/usr/bin/env python3
"""Quick test script — run locally where AI Studio is reachable.

Usage:
    pip install aiohttp aiohttp-socks
    python test_local.py

This tests image and video generation through the public API + Vertex AI.
"""

import asyncio
import json
import sys
import os
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from gg_studio.models import Account, ModelType, AspectRatio
from gg_studio.client import GoogleAIClient


async def main():
    accounts_path = Path(__file__).parent / "accounts.json"
    if not accounts_path.exists():
        print("ERROR: accounts.json not found.")
        print("Create it with your Google cookies (see README).")
        return

    with open(accounts_path) as f:
        data = json.load(f)

    if isinstance(data, list):
        acct_data = data[0]
    elif "accounts" in data:
        acct_data = data["accounts"][0]
    elif "url" in data and "cookies" in data:
        cookie_list = data["cookies"]
        if isinstance(cookie_list, list) and cookie_list and isinstance(cookie_list[0], dict):
            cookies = {c["name"]: c["value"] for c in cookie_list}
        else:
            cookies = cookie_list
        acct_data = {"name": "main", "cookies": cookies}
    else:
        acct_data = data

    raw_cookies = acct_data.get("cookies", {})
    if isinstance(raw_cookies, list) and raw_cookies and isinstance(raw_cookies[0], dict):
        acct_data["cookies"] = {c["name"]: c["value"] for c in raw_cookies}

    account = Account(
        name=acct_data.get("name", "main"),
        cookies=acct_data["cookies"],
        proxy=acct_data.get("proxy"),
    )

    api_key = os.environ.get("AISTUDIO_API_KEY", "")
    env_path = Path(__file__).parent / ".env"
    if not api_key and env_path.exists():
        for line in env_path.read_text().splitlines():
            if line.startswith("AISTUDIO_API_KEY="):
                os.environ["AISTUDIO_API_KEY"] = line.split("=", 1)[1].strip()
                break

    client = GoogleAIClient(account)
    passed = 0
    failed = 0

    print("=== GG Studio API Test ===")
    print()

    # --- Step 0: OAuth2 token ---
    print("[0] Acquiring OAuth2 token...")
    try:
        token = await client.ensure_oauth_token()
        if token:
            print(f"    OK: Token acquired ({len(token)} chars)")
        else:
            print("    WARNING: No OAuth2 token — will use API key only")
            print("    Run 'python setup_oauth.py' for one-time setup")
    except Exception as e:
        print(f"    Token acquisition failed: {e}")
    print()

    # --- Step 1: GCP project detection (for Vertex AI) ---
    print("[1] Detecting GCP project for Vertex AI...")
    if client._oauth_token:
        try:
            project = await client._detect_gcp_project()
            print(f"    OK: Project = {project}")
            passed += 1
        except Exception as e:
            print(f"    FAILED: {e}")
            print("    Vertex AI fallback won't work — will rely on API key only")
            failed += 1
    else:
        print("    SKIPPED: No OAuth2 token")
    print()

    # --- Step 2: API connectivity ---
    print("[2] Checking API connectivity...")
    try:
        status = await client.check_user_status()
        print(f"    OK: {json.dumps(status, indent=2)[:300]}")
        passed += 1
    except Exception as e:
        print(f"    FAILED: {e}")
        print("    (Continuing anyway — generation might still work)")
        failed += 1
    print()

    # --- Step 3: Image generation (Imagen 4) ---
    print("[3] Generating image with Imagen 4...")
    try:
        images = await client.generate_image(
            prompt="A beautiful sunset over the ocean with golden clouds",
            model=ModelType.IMAGEN_4,
            aspect_ratio=AspectRatio.LANDSCAPE_16_9,
            num_images=1,
        )
        out_dir = Path("output")
        out_dir.mkdir(exist_ok=True)
        for i, img_data in enumerate(images):
            path = out_dir / f"test_imagen4_{i}.png"
            path.write_bytes(img_data)
            print(f"    Saved: {path} ({len(img_data)} bytes)")
        passed += 1
    except Exception as e:
        print(f"    FAILED: {e}")
        failed += 1
    print()

    # --- Step 4: Image generation (Gemini) ---
    print("[4] Generating image with Gemini 3 Pro...")
    try:
        images = await client.generate_image(
            prompt="A cute robot painting a picture in a sunny garden",
            model=ModelType.GEMINI_3_PRO,
            aspect_ratio=AspectRatio.SQUARE_1_1,
            num_images=1,
        )
        out_dir = Path("output")
        out_dir.mkdir(exist_ok=True)
        for i, img_data in enumerate(images):
            path = out_dir / f"test_gemini_pro_{i}.png"
            path.write_bytes(img_data)
            print(f"    Saved: {path} ({len(img_data)} bytes)")
        passed += 1
    except Exception as e:
        print(f"    FAILED: {e}")
        failed += 1
    print()

    # --- Step 5: Video generation (Veo 3) ---
    print("[5] Generating video with Veo 3 (takes 2-5 min)...")
    try:
        video_data = await client.generate_video(
            prompt="A drone shot flying over a tropical beach at sunset, cinematic",
            model=ModelType.VEO_3,
            aspect_ratio=AspectRatio.LANDSCAPE_16_9,
            duration_seconds=8,
        )
        out_dir = Path("output")
        out_dir.mkdir(exist_ok=True)
        path = out_dir / "test_veo3.mp4"
        path.write_bytes(video_data)
        print(f"    Saved: {path} ({len(video_data)} bytes)")
        passed += 1
    except Exception as e:
        print(f"    FAILED: {e}")
        failed += 1
    print()

    # --- Summary ---
    print("=" * 50)
    print(f"=== RESULTS: {passed} passed, {failed} failed ===")
    if failed > 0:
        print()
        if not client._oauth_token:
            print("TIP: Run 'python setup_oauth.py' for OAuth2 setup")
        if not os.environ.get("AISTUDIO_API_KEY"):
            print("TIP: Set AISTUDIO_API_KEY from aistudio.google.com/apikey")
        if client._gcp_project is None and client._oauth_token:
            print("TIP: Create a GCP project at console.cloud.google.com")
            print("     and enable the Vertex AI API for image/video generation")
    print("=" * 50)

    await client.close()


if __name__ == "__main__":
    asyncio.run(main())
