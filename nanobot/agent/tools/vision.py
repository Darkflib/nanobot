"""Vision tool: analyze images using a local YOLO model or a remote vision-capable LLM."""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

import httpx
from loguru import logger

from nanobot.agent.tools.base import Tool

if TYPE_CHECKING:
    from nanobot.providers.base import LLMProvider

# Supported image MIME types
_MIME_TYPES: dict[str, str] = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
}

MAX_IMAGE_BYTES = 20 * 1024 * 1024  # 20 MB
MAX_REDIRECTS = 5


def _is_url(value: str) -> bool:
    try:
        p = urlparse(value)
        return p.scheme in ("http", "https")
    except Exception:
        return False


def _mime_from_path(path: str) -> str:
    return _MIME_TYPES.get(Path(path).suffix.lower(), "image/jpeg")


def _mime_from_url(url: str) -> str:
    path = urlparse(url).path
    return _MIME_TYPES.get(Path(path).suffix.lower(), "image/jpeg")


async def _load_image_as_data_url(source: str) -> tuple[str, str]:
    """Load an image from a URL or file path and return (data_url, mime_type).

    Raises ValueError on unsupported source or oversized image.
    """
    if _is_url(source):
        async with httpx.AsyncClient(follow_redirects=True, max_redirects=MAX_REDIRECTS, timeout=30.0) as client:
            r = await client.get(source)
            r.raise_for_status()
            raw = r.content
        ctype = r.headers.get("content-type", "").split(";")[0].strip()
        mime = ctype if ctype.startswith("image/") else _mime_from_url(source)
    else:
        path = Path(source).expanduser()
        if not path.exists():
            raise ValueError(f"Image file not found: {source}")
        raw = path.read_bytes()
        mime = _mime_from_path(source)

    if len(raw) > MAX_IMAGE_BYTES:
        raise ValueError(f"Image too large: {len(raw)} bytes (max {MAX_IMAGE_BYTES})")

    encoded = base64.b64encode(raw).decode()
    return f"data:{mime};base64,{encoded}", mime


class VisionTool(Tool):
    """Analyze an image using a local YOLO model or a remote vision-capable LLM.

    Backends
    --------
    - ``remote_llm``: Sends the image to the configured vision LLM and returns
      the model's natural-language answer.  The model must support vision input
      (e.g. ``openai/gpt-4o``, ``anthropic/claude-3-5-sonnet``).
    - ``yolo``:  Runs a local YOLO object-detection model (requires the
      ``ultralytics`` package) and returns a structured list of detected
      objects with class names, confidence scores, and bounding boxes.
    """

    name = "analyze_image"
    description = (
        "Analyze an image from a URL or local file path. "
        "Returns a description of the image contents or answers a specific question about it."
    )
    parameters = {
        "type": "object",
        "properties": {
            "image_source": {
                "type": "string",
                "description": "Image URL (http/https) or local file path to analyze.",
            },
            "question": {
                "type": "string",
                "description": (
                    "Question or instruction for the image analysis, "
                    "e.g. 'What objects are in this image?' or 'Describe the scene.'"
                ),
            },
        },
        "required": ["image_source"],
    }

    def __init__(
        self,
        backend: str = "remote_llm",
        provider: "LLMProvider | None" = None,
        vision_model: str = "",
        yolo_model: str = "yolo11n.pt",
    ) -> None:
        """
        Args:
            backend: ``"remote_llm"`` (default), ``"yolo"``, or ``"auto"``
                (try YOLO first, fall back to remote LLM).
            provider: LLM provider to use for ``remote_llm`` backend.
            vision_model: Model name for vision analysis.  Empty string means
                the caller should use the agent's default model.
            yolo_model: YOLO model name or path for the ``yolo`` backend.
        """
        self.backend = backend
        self.provider = provider
        self.vision_model = vision_model
        self.yolo_model = yolo_model

    async def execute(self, image_source: str, question: str | None = None, **kwargs: Any) -> str:
        """Analyze an image and return the result as a string."""
        question = question or "Describe what you see in this image."

        if self.backend == "yolo":
            try:
                return await self._analyze_yolo(image_source, question)
            except ImportError as exc:
                return f"Error: {exc}"
        if self.backend == "auto":
            try:
                return await self._analyze_yolo(image_source, question)
            except ImportError:
                pass
            except Exception as exc:
                logger.warning("YOLO vision analysis failed, falling back to remote LLM: {}", exc)
        return await self._analyze_remote(image_source, question)

    # ------------------------------------------------------------------
    # Remote LLM backend
    # ------------------------------------------------------------------

    async def _analyze_remote(self, image_source: str, question: str) -> str:
        """Send the image to the remote LLM and return the response."""
        if not self.provider:
            return (
                "Error: Vision tool (remote_llm backend) requires a configured LLM provider. "
                "Make sure an LLM provider with vision support is configured."
            )

        try:
            data_url, _mime = await _load_image_as_data_url(image_source)
        except Exception as exc:
            return json.dumps({"error": str(exc), "image_source": image_source}, ensure_ascii=False)

        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": data_url}},
                    {"type": "text", "text": question},
                ],
            }
        ]

        model = self.vision_model or None  # None → provider uses its default
        try:
            response = await self.provider.chat(messages=messages, model=model)
            return response.content or "No response from vision model."
        except Exception as exc:
            return json.dumps({"error": f"Vision LLM call failed: {exc}"}, ensure_ascii=False)

    # ------------------------------------------------------------------
    # YOLO backend
    # ------------------------------------------------------------------

    async def _analyze_yolo(self, image_source: str, question: str) -> str:
        """Run YOLO object detection and return structured results."""
        try:
            import ultralytics  # type: ignore[import-untyped]  # noqa: F401
        except ImportError:
            raise ImportError(
                "The 'ultralytics' package is required for the YOLO backend. "
                "Install it with: pip install ultralytics"
            )

        import asyncio

        # Download image to a temp file if it's a URL
        if _is_url(image_source):
            import tempfile

            data_url, mime = await _load_image_as_data_url(image_source)
            # Decode base64 data URL back to bytes for YOLO
            _, b64 = data_url.split(",", 1)
            raw = base64.b64decode(b64)
            ext = "." + mime.split("/")[-1].replace("jpeg", "jpg")
            with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
                tmp.write(raw)
                src_path = tmp.name
            cleanup = True
        else:
            src_path = str(Path(image_source).expanduser())
            if not Path(src_path).exists():
                return json.dumps({"error": f"Image file not found: {image_source}"}, ensure_ascii=False)
            cleanup = False

        try:
            loop = asyncio.get_event_loop()
            results = await loop.run_in_executor(None, self._run_yolo, src_path)
        finally:
            if cleanup:
                try:
                    os.unlink(src_path)
                except OSError:
                    pass

        return results

    def _run_yolo(self, image_path: str) -> str:
        """Run YOLO synchronously (called via executor to avoid blocking the event loop)."""
        from ultralytics import YOLO  # type: ignore[import-untyped]

        model = YOLO(self.yolo_model)
        results = model(image_path, verbose=False)

        detections = []
        for result in results:
            if result.boxes is None:
                continue
            for box in result.boxes:
                cls_id = int(box.cls[0].item())
                label = result.names.get(cls_id, str(cls_id))
                conf = round(float(box.conf[0].item()), 4)
                xyxy = [round(float(v), 1) for v in box.xyxy[0].tolist()]
                detections.append({
                    "class": label,
                    "confidence": conf,
                    "bbox_xyxy": xyxy,
                })

        return json.dumps(
            {
                "model": self.yolo_model,
                "image": image_path,
                "detections": detections,
                "count": len(detections),
            },
            ensure_ascii=False,
            indent=2,
        )
