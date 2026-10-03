"""Core API client — direct HTTP calls to Google AI Studio endpoints."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
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

BASE_URL = "https://generativelanguage.googleapis.com"
AISTUDIO_URL = "https://aistudio.google.com"

POLL_INTERVAL = 5.0
POLL_TIMEOUT = 600.0


class APIError(Exception):
    def __init__(self, status: int, message: str, body: Any = None):
        self.status = status
        self.body = body
        super().__init__(f"HTTP {status}: {message}")


class GoogleAIClient:
    """Stateless API client bound to a single account."""

    def __init__(self, account: Account, timeout: float = 120.0):
        self.account = account
        self.timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: Optional[aiohttp.ClientSession] = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            connector = None
            if self.account.proxy and ProxyConnector:
                connector = ProxyConnector.from_url(self.account.proxy)
            self._session = aiohttp.ClientSession(
                connector=connector,
                timeout=self.timeout,
                headers=get_auth_headers(self.account),
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
        params: dict | None = None,
    ) -> dict:
        session = await self._get_session()
        async with session.request(
            method, url, json=json_data, params=params
        ) as resp:
            body = await resp.text()
            if resp.status >= 400:
                raise APIError(resp.status, body[:500], body)
            try:
                return json.loads(body)
            except json.JSONDecodeError:
                if body.startswith(")]}'\n"):
                    return json.loads(body[4:].strip())
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
        url = f"{BASE_URL}/v1beta/{model_id}:predict"

        instances = [{"prompt": prompt}]

        if reference_images and config.get("supports_reference"):
            for ref_path in reference_images:
                img_bytes = Path(ref_path).read_bytes()
                b64 = base64.b64encode(img_bytes).decode()
                instances[0]["referenceImages"] = instances[0].get(
                    "referenceImages", []
                )
                instances[0]["referenceImages"].append(
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

        data = await self._request("POST", url, json_data=payload)
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
        url = f"{BASE_URL}/v1beta/{model_id}:generateContent"

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

        data = await self._request("POST", url, json_data=payload)
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
        url = f"{BASE_URL}/v1beta/{model_id}:predictLongRunning"

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

        data = await self._request("POST", url, json_data=payload)
        operation_name = data.get("name")
        if not operation_name:
            raise APIError(500, "No operation name returned", data)

        return await self._poll_operation(operation_name)

    async def _poll_operation(self, operation_name: str) -> bytes:
        url = f"{BASE_URL}/v1beta/{operation_name}"
        start = time.monotonic()

        while time.monotonic() - start < POLL_TIMEOUT:
            data = await self._request("GET", url)

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

            await asyncio.sleep(POLL_INTERVAL)

        raise APIError(408, f"Operation timed out after {POLL_TIMEOUT}s")

    # ------------------------------------------------------------------
    # AI Studio internal endpoints (alternative auth path)
    # ------------------------------------------------------------------

    async def generate_image_aistudio(
        self,
        prompt: str,
        model: ModelType = ModelType.IMAGEN_4,
        aspect_ratio: AspectRatio = AspectRatio.LANDSCAPE_16_9,
        num_images: int = 1,
    ) -> list[bytes]:
        """Use AI Studio's internal RPC endpoint (cookie auth, no API key)."""
        config = MODEL_CONFIG[model]
        model_id = config["api_model_id"]

        url = (
            f"{AISTUDIO_URL}/_/api/v1/generate_image"
            f"?model={model_id}"
        )

        dims = ASPECT_RATIO_MAP[aspect_ratio]

        payload = {
            "prompt": prompt,
            "sampleCount": num_images,
            "aspectRatio": f"{dims['width']}:{dims['height']}",
        }

        session = await self._get_session()
        headers = get_auth_headers(self.account)
        headers["X-Requested-With"] = "XMLHttpRequest"

        async with session.post(url, json=payload, headers=headers) as resp:
            body = await resp.text()
            if resp.status >= 400:
                raise APIError(resp.status, body[:500], body)

            if body.startswith(")]}'\n"):
                body = body[4:].strip()

            data = json.loads(body)
            results: list[bytes] = []
            for item in data if isinstance(data, list) else [data]:
                for img in item.get("images", item.get("predictions", [])):
                    b64 = img.get("bytesBase64Encoded", img.get("data", ""))
                    if b64:
                        results.append(base64.b64decode(b64))
            return results

    async def generate_video_aistudio(
        self,
        prompt: str,
        model: ModelType = ModelType.VEO_3,
        aspect_ratio: AspectRatio = AspectRatio.LANDSCAPE_16_9,
        duration_seconds: int = 8,
    ) -> tuple[str, None]:
        """Start video gen via AI Studio internal RPC. Returns operation name."""
        config = MODEL_CONFIG[model]
        model_id = config["api_model_id"]

        url = (
            f"{AISTUDIO_URL}/_/api/v1/generate_video"
            f"?model={model_id}"
        )

        dims = ASPECT_RATIO_MAP[aspect_ratio]

        payload = {
            "prompt": prompt,
            "aspectRatio": f"{dims['width']}:{dims['height']}",
            "durationSeconds": duration_seconds,
        }

        session = await self._get_session()
        headers = get_auth_headers(self.account)
        headers["X-Requested-With"] = "XMLHttpRequest"

        async with session.post(url, json=payload, headers=headers) as resp:
            body = await resp.text()
            if resp.status >= 400:
                raise APIError(resp.status, body[:500], body)

            if body.startswith(")]}'\n"):
                body = body[4:].strip()

            data = json.loads(body)
            op_name = data.get("name", "")
            return op_name, None
