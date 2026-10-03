"""Core API client — dual-backend for Google AI Studio.

Backend 1 (default): Public Gemini API at generativelanguage.googleapis.com
  - Works everywhere, requires API key
  - Set AISTUDIO_API_KEY or GEMINI_API_KEY env var

Backend 2 (fallback/local): MakerSuiteService gRPC-web at
  alkalimakersuite-pa.clients6.google.com
  - Cookie-based auth (SAPISIDHASH), same as browser
  - Only works when the host is reachable (local machine, VPN)

The client auto-detects which backend to use based on what's configured.
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

GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta"

GRPC_BASE = (
    "https://alkalimakersuite-pa.clients6.google.com"
    "/$rpc/google.internal.alkali.applications.makersuite.v1.MakerSuiteService"
)
ORIGIN = "https://aistudio.google.com"

POLL_INTERVAL = 5.0
POLL_TIMEOUT = 600.0

_cached_api_key: str = ""


def _get_api_key() -> str:
    global _cached_api_key
    key = (
        os.environ.get("AISTUDIO_API_KEY", "")
        or os.environ.get("GEMINI_API_KEY", "")
        or _cached_api_key
    )
    if key:
        _cached_api_key = key
    return key


class APIError(Exception):
    def __init__(self, status: int, message: str, body: Any = None):
        self.status = status
        self.body = body
        super().__init__(f"HTTP {status}: {message}")


def _sapisidhash(sapisid: str, origin: str = ORIGIN) -> str:
    ts = int(time.time())
    digest = hashlib.sha1(f"{ts} {sapisid} {origin}".encode()).hexdigest()
    return f"SAPISIDHASH {ts}_{digest}"


class GoogleAIClient:
    """Dual-backend AI Studio client.

    Prefers the public Gemini API when an API key is available.
    Falls back to MakerSuiteService gRPC-web when cookies are provided
    and the internal endpoint is reachable.
    """

    def __init__(
        self,
        account: Account,
        timeout: float = 120.0,
        force_backend: str | None = None,
    ):
        self.account = account
        self.timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: Optional[aiohttp.ClientSession] = None
        self._force_backend = force_backend  # "public" or "grpc"

    @property
    def _use_public_api(self) -> bool:
        if self._force_backend == "grpc":
            return False
        if self._force_backend == "public":
            return True
        return bool(_get_api_key())

    # ------------------------------------------------------------------
    # Headers
    # ------------------------------------------------------------------

    def _build_grpc_headers(self) -> dict[str, str]:
        sapisid = self.account.cookies.get("SAPISID", "")
        if not sapisid:
            sapisid = self.account.cookies.get("__Secure-3PAPISID", "")
        cookie_str = "; ".join(f"{k}={v}" for k, v in self.account.cookies.items())
        headers = {
            "Authorization": _sapisidhash(sapisid) if sapisid else "",
            "X-Goog-Authuser": "0",
            "Content-Type": "application/json",
            "Origin": ORIGIN,
            "Referer": f"{ORIGIN}/",
            "Cookie": cookie_str,
            "X-Goog-Ext-353267353-Jspb": "",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
        }
        return {k: v for k, v in headers.items() if v}

    # ------------------------------------------------------------------
    # Session
    # ------------------------------------------------------------------

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

    # ------------------------------------------------------------------
    # Low-level request methods
    # ------------------------------------------------------------------

    async def _rpc(self, method: str, body: dict) -> dict:
        """Call MakerSuiteService gRPC-web method (cookie auth)."""
        url = f"{GRPC_BASE}/{method}"
        session = await self._get_session()
        headers = self._build_grpc_headers()

        async with session.post(url, json=body, headers=headers) as resp:
            text = await resp.text()
            if resp.status >= 400:
                raise APIError(resp.status, text[:500], text)
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return {"raw": text}

    async def _public_api(
        self, model: str, method: str, body: dict
    ) -> dict:
        """Call public Gemini API (API key auth)."""
        key = _get_api_key()
        if not key:
            raise APIError(401, "No API key set. Set GEMINI_API_KEY or AISTUDIO_API_KEY env var.")

        url = f"{GEMINI_API_BASE}/{model}:{method}?key={key}"
        session = await self._get_session()
        headers = {"Content-Type": "application/json"}

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
        resp = await self._rpc("GenerateAccessToken", {})
        return resp.get("accessToken")

    async def check_user_status(self) -> dict:
        if self._use_public_api:
            return await self._public_api("models", "list", {})
        return await self._rpc("CheckUserStatus", {})

    async def list_models(self) -> list[dict]:
        if self._use_public_api:
            key = _get_api_key()
            session = await self._get_session()
            url = f"{GEMINI_API_BASE}/models?key={key}"
            async with session.get(url) as resp:
                text = await resp.text()
                if resp.status >= 400:
                    raise APIError(resp.status, text[:500])
                data = json.loads(text)
                return data.get("models", [])
        resp = await self._rpc("ListModels", {})
        return resp.get("models", [])

    async def auto_fetch_api_key(self) -> str:
        global _cached_api_key

        existing = _get_api_key()
        if existing:
            return existing

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
            "Set AISTUDIO_API_KEY or GEMINI_API_KEY env var manually.",
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
        config = MODEL_CONFIG[model]
        dims = ASPECT_RATIO_MAP[aspect_ratio]

        if self._use_public_api:
            if config["endpoint"] == "generate_content":
                return await self._public_generate_content_image(
                    prompt, config, dims, num_images, reference_images
                )
            return await self._public_generate_image(
                prompt, config, dims, num_images, reference_images
            )

        if config["endpoint"] == "generate_content":
            return await self._generate_via_gemini(
                prompt, config, dims, num_images, reference_images
            )
        return await self._generate_via_imagen(
            prompt, config, dims, num_images, reference_images
        )

    # --- Public API image generation ---

    async def _public_generate_image(
        self,
        prompt: str,
        config: dict,
        dims: dict,
        num_images: int,
        reference_images: list[str] | None,
    ) -> list[bytes]:
        """Generate images via public Gemini API (Imagen models)."""
        model_id = config.get("public_model_id") or config["api_model_id"]
        ratio_str = f"{dims['width']}:{dims['height']}"

        body: dict[str, Any] = {
            "instances": [{"prompt": prompt}],
            "parameters": {
                "sampleCount": num_images,
                "aspectRatio": ratio_str,
            },
        }

        if reference_images and config.get("supports_reference"):
            for ref_path in reference_images:
                img_bytes = Path(ref_path).read_bytes()
                b64 = base64.b64encode(img_bytes).decode()
                body["instances"][0]["referenceImages"] = [{
                    "referenceImage": {"bytesBase64Encoded": b64},
                    "referenceType": 1,
                }]

        data = await self._public_api(model_id, "predict", body)

        results: list[bytes] = []
        for pred in data.get("predictions", []):
            b64_data = pred.get("bytesBase64Encoded", "")
            if b64_data:
                results.append(base64.b64decode(b64_data))

        if not results:
            raise APIError(500, "No images returned from public API", data)
        return results

    async def _public_generate_content_image(
        self,
        prompt: str,
        config: dict,
        dims: dict,
        num_images: int,
        reference_images: list[str] | None,
    ) -> list[bytes]:
        """Generate images via public Gemini API (generateContent + responseModalities)."""
        model_id = config.get("public_model_id") or config["api_model_id"]
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

        body: dict[str, Any] = {
            "contents": [{"role": "user", "parts": parts}],
            "generationConfig": {
                "responseModalities": ["IMAGE", "TEXT"],
            },
        }

        data = await self._public_api(model_id, "generateContent", body)

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

    # --- MakerSuiteService image generation ---

    async def _generate_via_imagen(
        self,
        prompt: str,
        config: dict,
        dims: dict,
        num_images: int,
        reference_images: list[str] | None,
    ) -> list[bytes]:
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
        config = MODEL_CONFIG[model]
        model_id = config["api_model_id"]
        dims = ASPECT_RATIO_MAP[aspect_ratio]
        ratio_str = f"{dims['width']}:{dims['height']}"

        if self._use_public_api:
            pub_model_id = config.get("public_model_id") or model_id
            return await self._public_generate_video(
                prompt, pub_model_id, ratio_str, duration_seconds, reference_image, config
            )

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
        return await self._poll_video_grpc(operation_id)

    async def _public_generate_video(
        self,
        prompt: str,
        model_id: str,
        ratio_str: str,
        duration_seconds: int,
        reference_image: str | None,
        config: dict,
    ) -> bytes:
        """Generate video via public Gemini API."""
        body: dict[str, Any] = {
            "instances": [{
                "prompt": prompt,
            }],
            "parameters": {
                "aspectRatio": ratio_str,
                "durationSeconds": duration_seconds,
            },
        }

        if reference_image and config.get("supports_reference"):
            img_bytes = Path(reference_image).read_bytes()
            b64 = base64.b64encode(img_bytes).decode()
            body["instances"][0]["image"] = {"bytesBase64Encoded": b64}

        data = await self._public_api(model_id, "predictLongRunning", body)

        op_name = data.get("name", "")
        if not op_name:
            raise APIError(500, "No operation name returned", data)

        logger.info("Video generation started: %s", op_name)
        return await self._poll_video_public(op_name)

    async def _poll_video_public(self, operation_name: str) -> bytes:
        """Poll public API long-running operation."""
        key = _get_api_key()
        start = time.monotonic()
        session = await self._get_session()

        while time.monotonic() - start < POLL_TIMEOUT:
            url = f"{GEMINI_API_BASE}/{operation_name}?key={key}"
            async with session.get(url) as resp:
                text = await resp.text()
                if resp.status >= 400:
                    raise APIError(resp.status, text[:500])
                data = json.loads(text)

            if data.get("done"):
                response = data.get("response", {})
                for pred in response.get("predictions", []):
                    b64 = pred.get("bytesBase64Encoded", "")
                    if b64:
                        return base64.b64decode(b64)
                for vid in response.get("videos", []):
                    b64 = vid.get("bytesBase64Encoded", "")
                    if b64:
                        return base64.b64decode(b64)
                    uri = vid.get("uri", "")
                    if uri:
                        return await self._download_uri(uri)

                error = data.get("error", {})
                if error:
                    raise APIError(
                        error.get("code", 500),
                        error.get("message", "Video generation failed"),
                    )
                raise APIError(500, "Operation done but no video data", data)

            progress = data.get("metadata", {}).get("progressPercent", "?")
            elapsed = time.monotonic() - start
            logger.info("Video polling %s — %s%% (%.0fs)", operation_name, progress, elapsed)
            await asyncio.sleep(POLL_INTERVAL)

        raise APIError(408, f"Video generation timed out after {POLL_TIMEOUT}s")

    async def _poll_video_grpc(self, operation_id: str) -> bytes:
        """Poll GetGenerateVideoOperation (gRPC-web backend)."""
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
        session = await self._get_session()
        headers = self._build_grpc_headers() if not self._use_public_api else {}
        async with session.get(uri, headers=headers) as resp:
            if resp.status >= 400:
                raise APIError(resp.status, f"Failed to download from {uri}")
            return await resp.read()
