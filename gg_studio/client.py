"""Core API client — direct HTTP calls to Google AI Studio endpoints.

Uses AI Studio's internal RPC endpoints with cookie-based authentication.
No API key required — authenticates via Google account session cookies.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import re
import time
import uuid
from pathlib import Path
from typing import Any, Optional

import aiohttp

try:
    from aiohttp_socks import ProxyConnector
except ImportError:
    ProxyConnector = None  # type: ignore

from .auth import get_auth_headers
from .models import (
    ASPECT_RATIO_MAP,
    MODEL_CONFIG,
    Account,
    AspectRatio,
    GenerationType,
    ModelType,
)

logger = logging.getLogger("gg_studio.client")

AISTUDIO_URL = "https://aistudio.google.com"
ALKALI_URL = "https://alkali-pa.clients6.google.com"

POLL_INTERVAL = 5.0
POLL_TIMEOUT = 600.0

_cached_key: str = ""


def _get_aistudio_key() -> str:
    """Get AI Studio's public client key via AISTUDIO_API_KEY env var.

    Find it in AI Studio's page source (search for 'AIzaSy' in the JS).
    """
    global _cached_key
    import os
    key = os.environ.get("AISTUDIO_API_KEY", "") or _cached_key
    if key:
        _cached_key = key
    return key


class APIError(Exception):
    def __init__(self, status: int, message: str, body: Any = None):
        self.status = status
        self.body = body
        super().__init__(f"HTTP {status}: {message}")


def _sapisidhash(origin: str, sapisid: str) -> str:
    """Generate SAPISIDHASH for Google API authentication."""
    timestamp = str(int(time.time()))
    raw = f"{timestamp} {sapisid} {origin}"
    digest = hashlib.sha1(raw.encode()).hexdigest()
    return f"SAPISIDHASH {timestamp}_{digest}"


class GoogleAIClient:
    """API client bound to a single account using cookie auth via AI Studio."""

    def __init__(self, account: Account, timeout: float = 120.0):
        self.account = account
        self.timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: Optional[aiohttp.ClientSession] = None

    def _get_headers(self) -> dict[str, str]:
        headers = get_auth_headers(self.account)
        sapisid = self.account.cookies.get("SAPISID", "")
        if sapisid:
            headers["Authorization"] = _sapisidhash(AISTUDIO_URL, sapisid)
        headers["X-Goog-Authuser"] = "0"
        return headers

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            connector = None
            if self.account.proxy and ProxyConnector:
                connector = ProxyConnector.from_url(self.account.proxy)
            self._session = aiohttp.ClientSession(
                connector=connector,
                timeout=self.timeout,
            )
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    async def _request(
        self,
        method: str,
        url: str,
        *,
        json_data: dict | None = None,
        data: str | bytes | None = None,
        params: dict | None = None,
        headers: dict | None = None,
    ) -> dict:
        session = await self._get_session()
        req_headers = headers or self._get_headers()
        async with session.request(
            method, url, json=json_data, data=data, params=params,
            headers=req_headers,
        ) as resp:
            body = await resp.text()
            if resp.status >= 400:
                raise APIError(resp.status, body[:500], body)
            try:
                return json.loads(body)
            except json.JSONDecodeError:
                if body.startswith(")]}'"): 
                    cleaned = body.split("\n", 1)[-1].strip()
                    return json.loads(cleaned)
                return {"raw": body}

    # ------------------------------------------------------------------
    # Image generation
    # ------------------------------------------------------------------

    async def generate_image(
        self,
        prompt: str,
        model: ModelType = ModelType.IMAGEN_4,
        aspect_ratio: AspectRatio = AspectRatio.LANDSCAPE_16_9,
        num_images: int = 1,
        reference_images: list[str] | None = None,
    ) -> list[bytes]:
        config = MODEL_CONFIG[model]
        dims = ASPECT_RATIO_MAP[aspect_ratio]

        if config["endpoint"] == "generate_content":
            return await self._generate_via_gemini(
                prompt, config, dims, num_images, reference_images
            )

        return await self._generate_via_imagen(
            prompt, config, dims, num_images, reference_images
        )

    async def _generate_via_imagen(
        self,
        prompt: str,
        config: dict,
        dims: dict,
        num_images: int,
        reference_images: list[str] | None,
    ) -> list[bytes]:
        model_id = config["api_model_id"]
        url = f"{ALKALI_URL}/v1beta/{model_id}:predict"

        instances = [{"prompt": prompt}]

        if reference_images and config.get("supports_reference"):
            for ref_path in reference_images:
                img_bytes = Path(ref_path).read_bytes()
                b64 = base64.b64encode(img_bytes).decode()
                instances[0].setdefault("referenceImages", []).append(
                    {
                        "referenceImage": {"bytesBase64Encoded": b64},
                        "referenceType": "STYLE",
                    }
                )

        payload = {
            "instances": instances,
            "parameters": {
                "sampleCount": num_images,
                "aspectRatio": f"{dims['width']}:{dims['height']}",
                "outputOptions": {"mimeType": "image/png"},
            },
        }

        headers = self._get_headers()
        sapisid = self.account.cookies.get("SAPISID", "")
        if sapisid:
            headers["Authorization"] = _sapisidhash(ALKALI_URL, sapisid)
        headers["X-Goog-Api-Key"] = _get_aistudio_key()

        data = await self._request("POST", url, json_data=payload, headers=headers)
        results: list[bytes] = []
        for pred in data.get("predictions", []):
            b64_data = pred.get("bytesBase64Encoded", "")
            if b64_data:
                results.append(base64.b64decode(b64_data))
        return results

    async def _generate_via_gemini(
        self,
        prompt: str,
        config: dict,
        dims: dict,
        num_images: int,
        reference_images: list[str] | None,
    ) -> list[bytes]:
        model_id = config["api_model_id"]
        url = f"{ALKALI_URL}/v1beta/{model_id}:generateContent"

        parts: list[dict] = []

        if reference_images and config.get("supports_reference"):
            for ref_path in reference_images:
                img_bytes = Path(ref_path).read_bytes()
                b64 = base64.b64encode(img_bytes).decode()
                parts.append(
                    {
                        "inlineData": {
                            "mimeType": "image/png",
                            "data": b64,
                        }
                    }
                )

        parts.append({"text": prompt})

        payload = {
            "contents": [{"parts": parts}],
            "generationConfig": {
                "responseModalities": ["IMAGE"],
                "imageGenerationConfig": {
                    "numberOfImages": num_images,
                    "aspectRatio": f"{dims['width']}:{dims['height']}",
                },
            },
        }

        headers = self._get_headers()
        sapisid = self.account.cookies.get("SAPISID", "")
        if sapisid:
            headers["Authorization"] = _sapisidhash(ALKALI_URL, sapisid)
        headers["X-Goog-Api-Key"] = _get_aistudio_key()

        data = await self._request("POST", url, json_data=payload, headers=headers)
        results: list[bytes] = []
        for candidate in data.get("candidates", []):
            for part in candidate.get("content", {}).get("parts", []):
                inline = part.get("inlineData", {})
                b64_data = inline.get("data", "")
                if b64_data:
                    results.append(base64.b64decode(b64_data))
        return results

    # ------------------------------------------------------------------
    # Video generation
    # ------------------------------------------------------------------

    async def generate_video(
        self,
        prompt: str,
        model: ModelType = ModelType.VEO_3,
        aspect_ratio: AspectRatio = AspectRatio.LANDSCAPE_16_9,
        duration_seconds: int = 8,
        reference_image: str | None = None,
    ) -> bytes:
        config = MODEL_CONFIG[model]
        model_id = config["api_model_id"]
        url = f"{ALKALI_URL}/v1beta/{model_id}:predictLongRunning"

        dims = ASPECT_RATIO_MAP[aspect_ratio]

        payload: dict[str, Any] = {
            "instances": [{"prompt": prompt}],
            "parameters": {
                "aspectRatio": f"{dims['width']}:{dims['height']}",
                "durationSeconds": duration_seconds,
                "sampleCount": 1,
                "outputOptions": {"mimeType": "video/mp4"},
            },
        }

        if reference_image and config.get("supports_reference"):
            img_bytes = Path(reference_image).read_bytes()
            b64 = base64.b64encode(img_bytes).decode()
            payload["instances"][0]["referenceImages"] = [
                {
                    "referenceImage": {"bytesBase64Encoded": b64},
                    "referenceType": "FIRST_FRAME",
                }
            ]

        headers = self._get_headers()
        sapisid = self.account.cookies.get("SAPISID", "")
        if sapisid:
            headers["Authorization"] = _sapisidhash(ALKALI_URL, sapisid)
        headers["X-Goog-Api-Key"] = _get_aistudio_key()

        data = await self._request("POST", url, json_data=payload, headers=headers)
        operation_name = data.get("name")
        if not operation_name:
            raise APIError(500, "No operation name returned", data)

        return await self._poll_operation(operation_name)

    async def _poll_operation(self, operation_name: str) -> bytes:
        url = f"{ALKALI_URL}/v1beta/{operation_name}"
        start = time.monotonic()

        headers = self._get_headers()
        sapisid = self.account.cookies.get("SAPISID", "")
        if sapisid:
            headers["Authorization"] = _sapisidhash(ALKALI_URL, sapisid)
        headers["X-Goog-Api-Key"] = _get_aistudio_key()

        while time.monotonic() - start < POLL_TIMEOUT:
            data = await self._request("GET", url, headers=headers)

            if data.get("done"):
                response = data.get("response", {})
                for pred in response.get("predictions", []):
                    b64_data = pred.get("bytesBase64Encoded", "")
                    if b64_data:
                        return base64.b64decode(b64_data)

                videos = response.get("generateVideoResponse", {}).get("videos", [])
                for vid in videos:
                    b64_data = vid.get("bytesBase64Encoded", "")
                    if b64_data:
                        return base64.b64decode(b64_data)

                error = data.get("error", {})
                if error:
                    raise APIError(
                        error.get("code", 500),
                        error.get("message", "Operation failed"),
                    )
                raise APIError(500, "Operation done but no data", data)

            if data.get("error"):
                err = data["error"]
                raise APIError(err.get("code", 500), err.get("message", ""))

            logger.info("Polling %s... (%.0fs)", operation_name, time.monotonic() - start)
            await asyncio.sleep(POLL_INTERVAL)

        raise APIError(408, f"Operation timed out after {POLL_TIMEOUT}s")

    # ------------------------------------------------------------------
    # AI Studio batchexecute (alternative path)
    # ------------------------------------------------------------------

    async def generate_image_batchexecute(
        self,
        prompt: str,
        model: ModelType = ModelType.IMAGEN_4,
        aspect_ratio: AspectRatio = AspectRatio.LANDSCAPE_16_9,
        num_images: int = 4,
    ) -> list[bytes]:
        """Use AI Studio's batchexecute RPC (the exact same call the browser makes)."""
        config = MODEL_CONFIG[model]
        model_id = config["api_model_id"]
        dims = ASPECT_RATIO_MAP[aspect_ratio]
        ratio_str = f"{dims['width']}:{dims['height']}"

        inner_request = json.dumps([
            [prompt, None, None, None, None, None, None, None],
            [model_id.replace("models/", ""), num_images, ratio_str],
            None, None, None, None, None, None, None, None, None, None,
        ])

        rpc_id = "bwAWof"
        f_req = json.dumps([[[rpc_id, inner_request, None, "generic"]]])

        url = f"{AISTUDIO_URL}/_/MakerSuiteUi/data/batchexecute"

        headers = self._get_headers()
        headers["Content-Type"] = "application/x-www-form-urlencoded;charset=utf-8"

        form_data = f"f.req={aiohttp.helpers.quote(f_req, safe='')}"

        session = await self._get_session()
        async with session.post(url, data=form_data, headers=headers) as resp:
            body = await resp.text()
            if resp.status >= 400:
                raise APIError(resp.status, body[:500], body)

        results: list[bytes] = []
        for line in body.split("\n"):
            line = line.strip()
            if not line or not line.startswith("["):
                continue
            try:
                outer = json.loads(line)
                for item in outer:
                    if isinstance(item, list) and len(item) >= 3:
                        inner_data = item[2]
                        if isinstance(inner_data, str):
                            parsed = json.loads(inner_data)
                            images = self._extract_images_from_batch(parsed)
                            results.extend(images)
            except (json.JSONDecodeError, IndexError, TypeError):
                continue

        if not results:
            raise APIError(500, "No images in batchexecute response")
        return results

    def _extract_images_from_batch(self, data: Any) -> list[bytes]:
        results: list[bytes] = []
        if isinstance(data, list):
            for item in data:
                if isinstance(item, str) and len(item) > 200:
                    try:
                        decoded = base64.b64decode(item)
                        if decoded[:4] in (b"\x89PNG", b"\xff\xd8\xff\xe0", b"\xff\xd8\xff\xe1"):
                            results.append(decoded)
                    except Exception:
                        pass
                elif isinstance(item, list):
                    results.extend(self._extract_images_from_batch(item))
        return results
