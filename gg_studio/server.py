"""HTTP API server for remote generation control."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Any

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from .auth import load_accounts
from .models import (
    AspectRatio,
    BatchConfig,
    GenerationJob,
    ModelType,
    MODEL_CONFIG,
)
from .worker import BatchWorker

logger = logging.getLogger("gg_studio.server")

_worker: BatchWorker | None = None
_jobs: dict[str, GenerationJob] = {}


def _serialize_job(job: GenerationJob) -> dict:
    return {
        "id": job.id,
        "prompt": job.prompt,
        "model": job.model.value,
        "aspect_ratio": job.aspect_ratio.value,
        "status": job.status.value,
        "account": job.account,
        "result_path": job.result_path,
        "error": job.error,
        "retries": job.retries,
    }


async def health(request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok", "version": "2.0.0"})


async def list_models(request: Request) -> JSONResponse:
    models = []
    for model, cfg in MODEL_CONFIG.items():
        models.append(
            {
                "id": model.value,
                "display": cfg["display"],
                "type": cfg["type"].value,
                "supports_reference": cfg.get("supports_reference", False),
            }
        )
    return JSONResponse({"models": models})


async def generate(request: Request) -> JSONResponse:
    global _worker
    if not _worker:
        return JSONResponse({"error": "Worker not initialized"}, status_code=503)

    body = await request.json()
    prompt = body.get("prompt", "").strip()
    if not prompt:
        return JSONResponse({"error": "prompt required"}, status_code=400)

    try:
        model = ModelType(body.get("model", "imagen4"))
    except ValueError:
        return JSONResponse({"error": f"Invalid model"}, status_code=400)

    try:
        ratio = AspectRatio(body.get("aspect_ratio", "16:9"))
    except ValueError:
        ratio = AspectRatio.LANDSCAPE_16_9

    job = GenerationJob(
        id=uuid.uuid4().hex[:12],
        prompt=prompt,
        model=model,
        aspect_ratio=ratio,
        reference_images=body.get("reference_images", []),
        max_retries=body.get("max_retries", 3),
    )

    _jobs[job.id] = job
    await _worker.add_job(job)

    return JSONResponse({"job": _serialize_job(job)}, status_code=202)


async def batch_generate(request: Request) -> JSONResponse:
    global _worker
    if not _worker:
        return JSONResponse({"error": "Worker not initialized"}, status_code=503)

    body = await request.json()
    prompts = body.get("prompts", [])
    if not prompts:
        return JSONResponse({"error": "prompts array required"}, status_code=400)

    try:
        model = ModelType(body.get("model", "imagen4"))
    except ValueError:
        return JSONResponse({"error": "Invalid model"}, status_code=400)

    try:
        ratio = AspectRatio(body.get("aspect_ratio", "16:9"))
    except ValueError:
        ratio = AspectRatio.LANDSCAPE_16_9

    jobs = []
    for p in prompts:
        text = p if isinstance(p, str) else p.get("prompt", "")
        refs = [] if isinstance(p, str) else p.get("reference_images", [])
        if not text.strip():
            continue
        job = GenerationJob(
            id=uuid.uuid4().hex[:12],
            prompt=text.strip(),
            model=model,
            aspect_ratio=ratio,
            reference_images=refs,
        )
        _jobs[job.id] = job
        jobs.append(job)

    await _worker.add_jobs(jobs)

    return JSONResponse(
        {"jobs": [_serialize_job(j) for j in jobs], "count": len(jobs)},
        status_code=202,
    )


async def job_status(request: Request) -> JSONResponse:
    job_id = request.path_params["job_id"]
    job = _jobs.get(job_id)
    if not job:
        return JSONResponse({"error": "Job not found"}, status_code=404)
    return JSONResponse({"job": _serialize_job(job)})


async def list_jobs(request: Request) -> JSONResponse:
    status_filter = request.query_params.get("status")
    jobs = list(_jobs.values())
    if status_filter:
        jobs = [j for j in jobs if j.status.value == status_filter]
    return JSONResponse(
        {
            "jobs": [_serialize_job(j) for j in jobs[-100:]],
            "total": len(jobs),
            "stats": _worker.stats if _worker else {},
        }
    )


async def worker_stats(request: Request) -> JSONResponse:
    if not _worker:
        return JSONResponse({"error": "Worker not initialized"}, status_code=503)
    accounts_info = []
    for acc in _worker.accounts:
        accounts_info.append(
            {
                "name": acc.name,
                "active_jobs": acc.active_jobs,
                "total_generated": acc.total_generated,
                "errors": acc.errors,
            }
        )
    return JSONResponse(
        {
            "stats": _worker.stats,
            "accounts": accounts_info,
            "config": {
                "max_workers": _worker.config.max_workers,
                "image_rpm": _worker.config.image_rpm,
                "video_rpm": _worker.config.video_rpm,
                "wait_window": _worker.config.wait_window,
            },
        }
    )


def create_app(
    accounts_path: str = "accounts.json",
    config: BatchConfig | None = None,
) -> Starlette:
    global _worker

    cfg = config or BatchConfig()

    async def startup():
        global _worker
        accounts = load_accounts(accounts_path)
        _worker = BatchWorker(accounts, cfg)
        await _worker.start()
        logger.info("Worker started with %d accounts", len(accounts))

    async def shutdown():
        global _worker
        if _worker:
            await _worker.stop()

    routes = [
        Route("/health", health, methods=["GET"]),
        Route("/models", list_models, methods=["GET"]),
        Route("/generate", generate, methods=["POST"]),
        Route("/batch", batch_generate, methods=["POST"]),
        Route("/jobs", list_jobs, methods=["GET"]),
        Route("/jobs/{job_id}", job_status, methods=["GET"]),
        Route("/stats", worker_stats, methods=["GET"]),
    ]

    middleware = [
        Middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_methods=["*"],
            allow_headers=["*"],
        )
    ]

    return Starlette(
        routes=routes,
        middleware=middleware,
        on_startup=[startup],
        on_shutdown=[shutdown],
    )
