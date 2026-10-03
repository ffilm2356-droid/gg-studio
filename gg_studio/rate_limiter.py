"""Token-bucket rate limiter per account and model type."""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict

from .models import GenerationType


class RateLimiter:
    """Per-account rate limiter with separate image/video RPM."""

    def __init__(self, image_rpm: int = 25, video_rpm: int = 5, wait_window: float = 70.0, **_kw):
        self.image_rpm = image_rpm
        self.video_rpm = video_rpm
        self.window = wait_window
        self._timestamps: dict[str, list[float]] = defaultdict(list)
        self._lock = asyncio.Lock()

    def _key(self, account_name: str, gen_type: GenerationType) -> str:
        return f"{account_name}:{gen_type.value}"

    def _rpm(self, gen_type: GenerationType) -> int:
        return self.video_rpm if gen_type == GenerationType.VIDEO else self.image_rpm

    async def acquire(self, account_name: str, gen_type: GenerationType):
        key = self._key(account_name, gen_type)
        rpm = self._rpm(gen_type)

        while True:
            async with self._lock:
                now = time.monotonic()
                self._timestamps[key] = [
                    t for t in self._timestamps[key] if now - t < self.window
                ]

                if len(self._timestamps[key]) < rpm:
                    self._timestamps[key].append(now)
                    return

                oldest = self._timestamps[key][0]
                wait = self.window - (now - oldest) + 0.1

            await asyncio.sleep(wait)

    def current_usage(self, account_name: str, gen_type: GenerationType) -> tuple[int, int]:
        key = self._key(account_name, gen_type)
        now = time.monotonic()
        active = [t for t in self._timestamps[key] if now - t < self.window]
        rpm = self._rpm(gen_type)
        return len(active), rpm
