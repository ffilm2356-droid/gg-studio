"""Data models and configuration."""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Optional


class ModelType(str, enum.Enum):
    NARWHAL_FLASH = "narwhal"
    GEMINI_3_PRO = "gemini3pro"
    IMAGEN_4 = "imagen4"
    IMAGEN_3 = "imagen3"
    VEO_3 = "veo3"
    VEO_2 = "veo2"


class GenerationType(str, enum.Enum):
    IMAGE = "image"
    VIDEO = "video"


class AspectRatio(str, enum.Enum):
    LANDSCAPE_16_9 = "16:9"
    PORTRAIT_9_16 = "9:16"
    SQUARE_1_1 = "1:1"
    LANDSCAPE_4_3 = "4:3"
    PORTRAIT_3_4 = "3:4"


class JobStatus(str, enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    RETRYING = "retrying"


MODEL_CONFIG: dict[str, dict] = {
    ModelType.NARWHAL_FLASH: {
        "display": "Narwhal (Flash)",
        "type": GenerationType.IMAGE,
        "api_model_id": "models/imagen-3.0-generate-002",
        "endpoint": "generate_image",
        "supports_reference": True,
    },
    ModelType.GEMINI_3_PRO: {
        "display": "Gemini 3.0 Pro",
        "type": GenerationType.IMAGE,
        "api_model_id": "models/gemini-2.0-flash-exp",
        "endpoint": "generate_content",
        "supports_reference": True,
    },
    ModelType.IMAGEN_4: {
        "display": "Imagen 4.0",
        "type": GenerationType.IMAGE,
        "api_model_id": "models/imagen-4.0-generate-001",
        "endpoint": "generate_image",
        "supports_reference": True,
    },
    ModelType.IMAGEN_3: {
        "display": "Imagen 3",
        "type": GenerationType.IMAGE,
        "api_model_id": "models/imagen-3.0-generate-001",
        "endpoint": "generate_image",
        "supports_reference": False,
    },
    ModelType.VEO_3: {
        "display": "Veo 3",
        "type": GenerationType.VIDEO,
        "api_model_id": "models/veo-3.0-generate-preview",
        "endpoint": "generate_video",
        "supports_reference": True,
    },
    ModelType.VEO_2: {
        "display": "Veo 2",
        "type": GenerationType.VIDEO,
        "api_model_id": "models/veo-2.0-generate-001",
        "endpoint": "generate_video",
        "supports_reference": False,
    },
}

ASPECT_RATIO_MAP = {
    AspectRatio.LANDSCAPE_16_9: {"width": 1920, "height": 1080},
    AspectRatio.PORTRAIT_9_16: {"width": 1080, "height": 1920},
    AspectRatio.SQUARE_1_1: {"width": 1024, "height": 1024},
    AspectRatio.LANDSCAPE_4_3: {"width": 1408, "height": 1056},
    AspectRatio.PORTRAIT_3_4: {"width": 1056, "height": 1408},
}


@dataclass
class Account:
    name: str
    cookies: dict[str, str]
    proxy: Optional[str] = None
    active_jobs: int = 0
    total_generated: int = 0
    errors: int = 0


@dataclass
class GenerationJob:
    id: str
    prompt: str
    model: ModelType
    aspect_ratio: AspectRatio = AspectRatio.LANDSCAPE_16_9
    reference_images: list[str] = field(default_factory=list)
    status: JobStatus = JobStatus.PENDING
    account: Optional[str] = None
    result_path: Optional[str] = None
    error: Optional[str] = None
    retries: int = 0
    max_retries: int = 3
    operation_name: Optional[str] = None


@dataclass
class BatchConfig:
    max_workers: int = 30
    wait_window: float = 70.0
    video_rpm: int = 5
    image_rpm: int = 25
    auto_retry: bool = True
    output_dir: str = "./output"
