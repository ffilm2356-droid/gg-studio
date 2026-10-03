"""Core API client — reverse-engineered AI Studio MakerSuiteService gRPC-web API.

Uses the same internal RPC endpoints as AI Studio's browser frontend.
Authentication: Google session cookies + SAPISIDHASH + X-Goog-Api-Key.

Endpoints:
  - GenerateImage (Imagen 4, Imagen 3, Narwhal)
  - GenerateContent (Gemini image gen via responseModalities=IMAGE)
  - GenerateVideo / GetGenerateVideoOperation (Veo 3, Veo 2)

Setup:
  1. Export cookies from aistudio.google.com (use a browser extension)
  2. Set AISTUDIO_API_KEY env var (find it in AI Studio page source: search 'AIzaSy')
     OR call client.auto_fetch_api_key() to get one via GenerateCloudApiKey
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import time
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

# AI Studio's internal gRPC-web endpoint
GRPC_BASE = (
    "https://alkalimakersuite-pa.clients6.google.com"
    "/$rpc/google.internal.alkali.applications.makersuite.v1.MakerSuiteService"
)
ORIGIN = "https://aistudio.google.com"

POLL_INTERVAL = 5.0
POLL_TIMEOUT = 600.0

_cached_api_key: str = ""


def _get_api_key() -> str:
    """Get AI Studio API key from env var AISTUDIO_API_KEY."""
    global _cached_api_key
    key = os.environ.get("AISTUDIO_API_KEY", "") or _cached_api_key
    if key:
        _cached_api_key = key
    return key


class APIError(Exception):
    def __init__(self, status: int, message: str, body: Any = None):
        self.status = status
        self.body = body
        super().__init__(f"HTTP {status}: {message}")


def _sapisidhash(sapisid: str, origin: str = ORIGIN) -> str:
    """Build SAPISIDHASH Authorization header value."""
    ts = int(time.time())
    digest = hashlib.sha1(f"{ts} {sapisid} {origin}".encode()).hexdigest()
    return f"SAPISIDHASH {ts}_{digest}"


class GoogleAIClient:
    """AI Studio client using MakerSuiteService gRPC-web API with cookie auth."""

    def __init__(self, account: Account, timeout: float = 120.0):
        self.account = account
        self.timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: Optional[aiohttp.ClientSession] = None

    def _build_headers(self) -> dict[str, str]:
        """Build request headers with SAPISIDHASH auth."""
        sapisid = self.account.cookies.get("SAPISID", "")
        cookie_str = "; ".join(f"{k}={v}" for k, v in self.account.cookies.items())
        headers = {
            "Authorization": _sapisidhash(sapisid) if sapisid else "",
            "X-Goog-Api-Key": _get_api_key(),
            "X-Goog-Authuser": "0",
            "Content-Type": "application/json",
            "Origin": ORIGIN,
            "Referer": f"{ORIGIN}/",
            "Cookie": cookie_str,
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
        }
        return {k: v for k, v in headers.items() if v}

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

    async def _rpc(self, method: str, body: dict) -> dict:
        """Call a MakerSuiteService gRPC-web method."""
        url = f"{GRPC_BASE}/{method}"
        session = await self._get_session()
        headers = self._build_headers()

        async with session.post(url, json=body, headers=headers) as resp:
            text = await resp.text()
            if resp.status >= 400:
                raise APIError(resp.status, text[:500], text)
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return {"raw": text}

    # ------------------------------------------------------------------
    # Auth & utility
    # ------------------------------------------------------------------

    async def generate_access_token(self) -> Optional[str]:
        """Get a fresh OAuth2 bearer token (ya29.xxx)."""
        resp = await self._rpc("GenerateAccessToken", {})
        return resp.get("accessToken")

    async def check_user_status(self) -> dict:
        """Return user account status and feature flags."""
        return await self._rpc("CheckUserStatus", {})

    async def list_models(self) -> list[dict]:
        """List all available models."""
        resp = await self._rpc("ListModels", {})
        return resp.get("models", [])

    async def auto_fetch_api_key(self) -> str:
        """Try to get or create an API key via the AI Studio API."""
        global _cached_api_key

        try:
            resp = await self._rpc("ListCloudApiKeys", {})
            keys = resp.get("apiKeys", [])
            if keys:
                key = keys[0].get("key", "") or keys[0].get("keyString", "")
                if key:
                    _cached_api_key = key
                    os.environ["AISTUDIO_API_KEY"] = key
                    logger.info("Auto-fetched API key from existing cloud keys")
                    return key
        except APIError:
            pass

        try:
            resp = await self._rpc("ListCloudProjects", {})
            projects = resp.get("projects", [])
            if projects:
                project_id = projects[0].get("projectId", "")
                if project_id:
                    resp = await self._rpc("GenerateCloudApiKey", {
                        "projectId": project_id,
                        "displayName": "gg-studio",
                    })
                    key = resp.get("key", "") or resp.get("keyString", "")
                    if key:
                        _cached_api_key = key
                        os.environ["AISTUDIO_API_KEY"] = key
                        logger.info("Generated new API key for project %s", project_id)
                        return key
        except APIError:
            pass

        raise APIError(
            401,
            "Could not auto-fetch API key. "
            "Set AISTUDIO_API_KEY env var manually "
            "(find it in AI Studio page source: search AIzaSy).",
        )

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
        """Generate images using the specified model."""
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
        """Generate images via MakerSuiteService/GenerateImage (Imagen models)."""
        model_id = config["api_model_id"]
        ratio_str = f"{dims['width']}:{dims['height']}"

        body: dict[str, Any] = {
            "model": model_id,
            "prompt": prompt,
            "sampleCount": num_images,
            "aspectRatio": ratio_str,
            "outputOptions": {"mimeType": "image/png"},
        }

        if reference_images and config.get("supports_reference"):
            ref_list = []
            for ref_path in reference_images:
                img_bytes = Path(ref_path).read_bytes()
                b64 = base64.b64encode(img_bytes).decode()
                ref_list.append({
                    "referenceImage": {"bytesBase64Encoded": b64},
                    "referenceType": "STYLE",
                })
            body["referenceImages"] = ref_list

        data = await self._rpc("GenerateImage", body)

        results: list[bytes] = []
        for pred in data.get("predictions", data.get("images", [])):
            b64_data = (
                pred.get("bytesBase64Encoded", "")
                or pred.get("data", "")
                or pred.get("image", "")
            )
            if b64_data:
                results.append(base64.b64decode(b64_data))

        if not results:
            raise APIError(500, "No images returned", data)
        return results

    async def _generate_via_gemini(
        self,
        prompt: str,
        config: dict,
        dims: dict,
        num_images: int,
        reference_images: list[str] | None,
    ) -> list[bytes]:
        """Generate images via MakerSuiteService/GenerateContent (Gemini models)."""
        model_id = config["api_model_id"]
        ratio_str = f"{dims['width']}:{dims['height']}"

        parts: list[dict] = []

        if reference_images and config.get("supports_reference"):
            for ref_path in reference_images:
                img_bytes = Path(ref_path).read_bytes()
                b64 = base64.b64encode(img_bytes).decode()
                parts.append({
                    "inlineData": {
                        "mimeType": "image/png",
                        "data": b64,
                    }
                })

        parts.append({"text": prompt})

        body = {
            "model": model_id,
            "contents": [{"role": "user", "parts": parts}],
            "generationConfig": {
                "responseModalities": ["IMAGE"],
                "imageGenerationConfig": {
                    "numberOfImages": num_images,
                    "aspectRatio": ratio_str,
                },
            },
        }

        data = await self._rpc("GenerateContent", body)

        results: list[bytes] = []
        for candidate in data.get("candidates", []):
            for part in candidate.get("content", {}).get("parts", []):
                inline = part.get("inlineData", {})
                b64_data = inline.get("data", "")
                if b64_data:
                    results.append(base64.b64decode(b64_data))

        if not results:
            raise APIError(500, "No images in GenerateContent response", data)
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
        """Generate a video via MakerSuiteService/GenerateVideo."""
        config = MODEL_CONFIG[model]
        model_id = config["api_model_id"]
        dims = ASPECT_RATIO_MAP[aspect_ratio]
        ratio_str = f"{dims['width']}:{dims['height']}"

        body: dict[str, Any] = {
            "model": model_id,
            "prompt": prompt,
            "aspectRatio": ratio_str,
            "durationSeconds": duration_seconds,
        }

        if reference_image and config.get("supports_reference"):
            img_bytes = Path(reference_image).read_bytes()
            b64 = base64.b64encode(img_bytes).decode()
            body["referenceImages"] = [{
                "referenceImage": {"bytesBase64Encoded": b64},
                "referenceType": "FIRST_FRAME",
            }]

        data = await self._rpc("GenerateVideo", body)

        operation_id = data.get("operationId", "") or data.get("name", "")
        if not operation_id:
            raise APIError(500, "No operation ID returned", data)

        logger.info("Video generation started: %s", operation_id)
        return await self._poll_video(operation_id)

    async def _poll_video(self, operation_id: str) -> bytes:
        """Poll GetGenerateVideoOperation until done."""
        start = time.monotonic()

        while time.monotonic() - start < POLL_TIMEOUT:
            data = await self._rpc("GetGenerateVideoOperation", {
                "operationId": operation_id,
            })

            if data.get("done"):
                video_uri = data.get("videoUri", "")
                if video_uri:
                    return await self._download_uri(video_uri)

                for pred in data.get("response", {}).get("predictions", []):
                    b64 = pred.get("bytesBase64Encoded", "")
                    if b64:
                        return base64.b64decode(b64)

                videos = data.get("videos", data.get("response", {}).get("videos", []))
                for vid in videos:
                    b64 = vid.get("bytesBase64Encoded", "")
                    if b64:
                        return base64.b64decode(b64)
                    uri = vid.get("uri", "") or vid.get("videoUri", "")
                    if uri:
                        return await self._download_uri(uri)

                error = data.get("error", {})
                if error:
                    raise APIError(
                        error.get("code", 500),
                        error.get("message", "Video generation failed"),
                    )
                raise APIError(500, "Operation done but no video data", data)

            if data.get("error"):
                err = data["error"]
                raise APIError(err.get("code", 500), err.get("message", ""))

            progress = data.get("metadata", {}).get("progressPercent", "?")
            elapsed = time.monotonic() - start
            logger.info("Video polling %s — %s%% (%.0fs)", operation_id, progress, elapsed)
            await asyncio.sleep(POLL_INTERVAL)

        raise APIError(408, f"Video generation timed out after {POLL_TIMEOUT}s")

    async def _download_uri(self, uri: str) -> bytes:
        """Download video from a Google-internal URI."""
        session = await self._get_session()
        headers = self._build_headers()
        async with session.get(uri, headers=headers) as resp:
            if resp.status >= 400:
                raise APIError(resp.status, f"Failed to download video from {uri}")
            return await resp.read()
