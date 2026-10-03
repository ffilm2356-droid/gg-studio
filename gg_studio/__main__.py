"""CLI entry point — python -m gg_studio"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import uuid
from pathlib import Path

import click

from .auth import load_accounts
from .models import (
    AspectRatio,
    BatchConfig,
    GenerationJob,
    JobStatus,
    ModelType,
    MODEL_CONFIG,
)
from .client import GoogleAIClient
from .worker import BatchWorker


def setup_logging(verbose: bool = False):
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="[%(asctime)s] [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )


@click.group()
@click.option("-v", "--verbose", is_flag=True, help="Debug logging")
def cli(verbose: bool):
    """GG Studio — Google Flow & Veo API Launcher v2"""
    setup_logging(verbose)


@cli.command()
@click.option("--prompt", "-p", required=True, help="Generation prompt")
@click.option(
    "--model",
    "-m",
    type=click.Choice([m.value for m in ModelType]),
    default="imagen4",
    help="Model to use",
)
@click.option(
    "--ratio",
    "-r",
    type=click.Choice([r.value for r in AspectRatio]),
    default="16:9",
    help="Aspect ratio",
)
@click.option("--output", "-o", default="./output", help="Output directory")
@click.option("--accounts", "-a", default="accounts.json", help="Accounts file")
@click.option("--reference", multiple=True, help="Reference image path(s)")
def generate(
    prompt: str,
    model: str,
    ratio: str,
    output: str,
    accounts: str,
    reference: tuple,
):
    """Generate a single image or video."""

    async def _run():
        accs = load_accounts(accounts)
        if not accs:
            click.echo("No valid accounts found.", err=True)
            sys.exit(1)

        client = GoogleAIClient(accs[0])
        model_type = ModelType(model)
        aspect = AspectRatio(ratio)
        config = MODEL_CONFIG[model_type]

        Path(output).mkdir(parents=True, exist_ok=True)

        try:
            if config["type"].value == "image":
                click.echo(f"Generating image with {config['display']}...")
                images = await client.generate_image(
                    prompt=prompt,
                    model=model_type,
                    aspect_ratio=aspect,
                    reference_images=list(reference),
                )
                for i, img in enumerate(images):
                    fname = f"{uuid.uuid4().hex[:8]}.png"
                    out_path = Path(output) / fname
                    out_path.write_bytes(img)
                    click.echo(f"Saved: {out_path}")
            else:
                click.echo(f"Generating video with {config['display']}...")
                video = await client.generate_video(
                    prompt=prompt,
                    model=model_type,
                    aspect_ratio=aspect,
                    reference_image=reference[0] if reference else None,
                )
                fname = f"{uuid.uuid4().hex[:8]}.mp4"
                out_path = Path(output) / fname
                out_path.write_bytes(video)
                click.echo(f"Saved: {out_path}")
        finally:
            await client.close()

    asyncio.run(_run())


@cli.command()
@click.option("--input", "-i", "input_file", required=True, help="Prompts file (txt/json)")
@click.option(
    "--model",
    "-m",
    type=click.Choice([m.value for m in ModelType]),
    default="imagen4",
)
@click.option(
    "--ratio",
    "-r",
    type=click.Choice([r.value for r in AspectRatio]),
    default="16:9",
)
@click.option("--workers", "-w", default=30, help="Max concurrent workers")
@click.option("--output", "-o", default="./output", help="Output directory")
@click.option("--accounts", "-a", default="accounts.json")
@click.option("--image-rpm", default=25, help="Images per minute per account")
@click.option("--video-rpm", default=5, help="Videos per minute per account")
@click.option("--window", default=70.0, help="Rate limit window (seconds)")
@click.option("--no-retry", is_flag=True, help="Disable auto-retry")
def batch(
    input_file: str,
    model: str,
    ratio: str,
    workers: int,
    output: str,
    accounts: str,
    image_rpm: int,
    video_rpm: int,
    window: float,
    no_retry: bool,
):
    """Batch generate from a prompts file."""

    async def _run():
        accs = load_accounts(accounts)
        if not accs:
            click.echo("No valid accounts found.", err=True)
            sys.exit(1)

        prompts = _load_prompts(input_file)
        if not prompts:
            click.echo("No prompts found.", err=True)
            sys.exit(1)

        click.echo(f"Loaded {len(prompts)} prompts, {len(accs)} accounts, {workers} workers")

        config = BatchConfig(
            max_workers=workers,
            wait_window=window,
            video_rpm=video_rpm,
            image_rpm=image_rpm,
            auto_retry=not no_retry,
            output_dir=output,
        )

        completed = 0
        failed = 0

        def on_complete(job: GenerationJob):
            nonlocal completed
            completed += 1
            click.echo(f"[{completed}/{len(prompts)}] Done: {job.prompt[:60]} -> {job.result_path}")

        def on_error(job: GenerationJob):
            nonlocal failed
            failed += 1
            click.echo(f"[FAIL] {job.prompt[:60]} -- {job.error}", err=True)

        worker = BatchWorker(
            accs, config, on_complete=on_complete, on_error=on_error
        )

        model_type = ModelType(model)
        aspect = AspectRatio(ratio)

        jobs = []
        for p in prompts:
            text = p if isinstance(p, str) else p.get("prompt", "")
            refs = [] if isinstance(p, str) else p.get("reference_images", [])
            jobs.append(
                GenerationJob(
                    id=uuid.uuid4().hex[:12],
                    prompt=text.strip(),
                    model=model_type,
                    aspect_ratio=aspect,
                    reference_images=refs,
                )
            )

        await worker.start()
        await worker.add_jobs(jobs)
        await worker.wait_all()
        await worker.stop()

        click.echo(f"\nDone. {completed} completed, {failed} failed.")

    asyncio.run(_run())


@cli.command()
@click.option("--port", "-p", default=8080, help="Server port")
@click.option("--host", "-h", default="0.0.0.0", help="Server host")
@click.option("--accounts", "-a", default="accounts.json")
@click.option("--workers", "-w", default=30)
@click.option("--image-rpm", default=25)
@click.option("--video-rpm", default=5)
def serve(
    port: int,
    host: str,
    accounts: str,
    workers: int,
    image_rpm: int,
    video_rpm: int,
):
    """Start the HTTP API server."""
    import uvicorn
    from .server import create_app

    setup_logging()

    config = BatchConfig(
        max_workers=workers,
        image_rpm=image_rpm,
        video_rpm=video_rpm,
    )

    app = create_app(accounts_path=accounts, config=config)
    click.echo(f"Starting GG Studio API server on {host}:{port}")
    uvicorn.run(app, host=host, port=port, log_level="info")


@cli.command()
@click.option("--accounts", "-a", default="accounts.json")
def check(accounts: str):
    """Verify account cookies are valid."""

    async def _run():
        accs = load_accounts(accounts)
        for acc in accs:
            client = GoogleAIClient(acc, timeout=30.0)
            try:
                await client._get_session()
                click.echo(f"[OK] {acc.name} -- cookies loaded, proxy: {acc.proxy or 'none'}")
            except Exception as e:
                click.echo(f"[FAIL] {acc.name} -- {e}", err=True)
            finally:
                await client.close()

    asyncio.run(_run())


def _load_prompts(path: str) -> list:
    p = Path(path)
    if not p.exists():
        return []

    text = p.read_text(encoding="utf-8").strip()

    if p.suffix == ".json":
        data = json.loads(text)
        if isinstance(data, list):
            return data
        return data.get("prompts", [])

    return [line.strip() for line in text.splitlines() if line.strip()]


if __name__ == "__main__":
    cli()
