"""Batch worker — concurrent job processor with rate limiting and auto-retry."""

from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Callable, Optional

from .auth import load_accounts
from .client import APIError, GoogleAIClient
from .models import (
    MODEL_CONFIG,
    Account,
    AspectRatio,
    BatchConfig,
    GenerationJob,
    GenerationType,
    JobStatus,
    ModelType,
)
from .rate_limiter import RateLimiter

logger = logging.getLogger("gg_studio.worker")


class BatchWorker:
    """Manages a pool of workers processing generation jobs across accounts."""

    def __init__(
        self,
        accounts: list[Account],
        config: BatchConfig | None = None,
        on_complete: Callable[[GenerationJob], None] | None = None,
        on_error: Callable[[GenerationJob], None] | None = None,
        on_progress: Callable[[GenerationJob], None] | None = None,
    ):
        self.accounts = accounts
        self.config = config or BatchConfig()
        self.on_complete = on_complete
        self.on_error = on_error
        self.on_progress = on_progress

        self.rate_limiter = RateLimiter(
            image_rpm=self.config.image_rpm,
            video_rpm=self.config.video_rpm,
            wait_window=self.config.wait_window,
        )

        self._queue: asyncio.Queue[GenerationJob] = asyncio.Queue()
        self._clients: dict[str, GoogleAIClient] = {}
        self._running = False
        self._workers: list[asyncio.Task] = []
        self._account_idx = 0
        self._lock = asyncio.Lock()

        self.stats = {
            "total": 0,
            "completed": 0,
            "failed": 0,
            "running": 0,
            "pending": 0,
        }

        Path(self.config.output_dir).mkdir(parents=True, exist_ok=True)

        for acc in accounts:
            self._clients[acc.name] = GoogleAIClient(acc)

    def _next_account(self) -> Account:
        best = min(self.accounts, key=lambda a: a.active_jobs)
        return best

    async def add_job(self, job: GenerationJob):
        self.stats["total"] += 1
        self.stats["pending"] += 1
        await self._queue.put(job)

    async def add_jobs(self, jobs: list[GenerationJob]):
        for job in jobs:
            await self.add_job(job)

    async def start(self):
        self._running = True

        for name, client in self._clients.items():
            try:
                token = await client.ensure_oauth_token()
                if token:
                    logger.info("OAuth2 token acquired for account '%s'", name)
                else:
                    logger.warning("No OAuth2 token for account '%s' — will use cookie auth", name)
            except Exception as e:
                logger.warning("OAuth2 setup failed for '%s': %s", name, e)

        num_workers = min(self.config.max_workers, len(self.accounts) * 10)
        logger.info("Starting %d workers across %d accounts", num_workers, len(self.accounts))

        for i in range(num_workers):
            task = asyncio.create_task(self._worker_loop(i))
            self._workers.append(task)

    async def stop(self):
        self._running = False
        for task in self._workers:
            task.cancel()
        await asyncio.gather(*self._workers, return_exceptions=True)
        for client in self._clients.values():
            await client.close()

    async def wait_all(self):
        await self._queue.join()

    async def _worker_loop(self, worker_id: int):
        while self._running:
            try:
                job = await asyncio.wait_for(self._queue.get(), timeout=2.0)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                return

            try:
                await self._process_job(job, worker_id)
            except asyncio.CancelledError:
                return
            except Exception as e:
                logger.exception("Worker %d unhandled error: %s", worker_id, e)
            finally:
                self._queue.task_done()

    async def _process_job(self, job: GenerationJob, worker_id: int):
        account = self._next_account()
        job.account = account.name
        job.status = JobStatus.RUNNING
        self.stats["pending"] -= 1
        self.stats["running"] += 1
        account.active_jobs += 1

        if self.on_progress:
            self.on_progress(job)

        config = MODEL_CONFIG[job.model]
        gen_type = config["type"]

        try:
            await self.rate_limiter.acquire(account.name, gen_type)

            client = self._clients[account.name]

            if gen_type == GenerationType.IMAGE:
                images = await client.generate_image(
                    prompt=job.prompt,
                    model=job.model,
                    aspect_ratio=job.aspect_ratio,
                    reference_images=job.reference_images,
                )
                if images:
                    ext = "png"
                    out_path = Path(self.config.output_dir) / f"{job.id}.{ext}"
                    out_path.write_bytes(images[0])
                    job.result_path = str(out_path)

                    for i, img in enumerate(images[1:], 1):
                        extra = Path(self.config.output_dir) / f"{job.id}_{i}.{ext}"
                        extra.write_bytes(img)
                else:
                    raise APIError(500, "No images returned")

            elif gen_type == GenerationType.VIDEO:
                video_data = await client.generate_video(
                    prompt=job.prompt,
                    model=job.model,
                    aspect_ratio=job.aspect_ratio,
                )
                out_path = Path(self.config.output_dir) / f"{job.id}.mp4"
                out_path.write_bytes(video_data)
                job.result_path = str(out_path)

            job.status = JobStatus.COMPLETED
            self.stats["completed"] += 1
            account.total_generated += 1

            if self.on_complete:
                self.on_complete(job)

            logger.info(
                "[Worker %d] Completed: %s -> %s",
                worker_id,
                job.prompt[:50],
                job.result_path,
            )

        except APIError as e:
            account.errors += 1
            if job.retries < job.max_retries and self.config.auto_retry:
                job.retries += 1
                job.status = JobStatus.RETRYING
                self.stats["running"] -= 1
                self.stats["pending"] += 1
                logger.warning(
                    "[Worker %d] Retry %d/%d for '%s': %s",
                    worker_id,
                    job.retries,
                    job.max_retries,
                    job.prompt[:50],
                    e,
                )
                await asyncio.sleep(2 ** job.retries)
                await self._queue.put(job)
            else:
                job.status = JobStatus.FAILED
                job.error = str(e)
                self.stats["failed"] += 1
                if self.on_error:
                    self.on_error(job)
                logger.error(
                    "[Worker %d] Failed: '%s' -- %s", worker_id, job.prompt[:50], e
                )

        except Exception as e:
            job.status = JobStatus.FAILED
            job.error = str(e)
            self.stats["failed"] += 1
            account.errors += 1
            if self.on_error:
                self.on_error(job)
            logger.error("[Worker %d] Error: %s", worker_id, e)

        finally:
            account.active_jobs -= 1
            self.stats["running"] -= 1
