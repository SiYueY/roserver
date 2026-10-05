"""Decode bounded ROS Image snapshots, respecting channel order and row stride."""
from __future__ import annotations

from io import BytesIO
from typing import Any


def image_pil(message: Any) -> Any:
    from PIL import Image
    formats = {"rgb8": ("RGB", "RGB", 3), "bgr8": ("RGB", "BGR", 3),
               "rgba8": ("RGBA", "RGBA", 4), "bgra8": ("RGBA", "BGRA", 4),
               "mono8": ("L", "L", 1)}
    if message.encoding not in formats:
        raise ValueError(f"Unsupported camera encoding: {message.encoding}")
    mode, raw, channels = formats[message.encoding]
    width, height, step = int(message.width), int(message.height), int(message.step)
    if not 0 < width <= 8192 or not 0 < height <= 8192 or width * height > 16_000_000:
        raise ValueError("Camera dimensions exceed the image limit.")
    if not width * channels <= step <= width * channels + 65536:
        raise ValueError("Invalid camera row stride.")
    data = getattr(message, "data_bytes", None)
    if data is None:
        data = bytes(message.data)
    if len(data) < height * step:
        raise ValueError("Truncated ROS camera image.")
    return Image.frombytes(mode, (width, height), data, "raw", raw, step, 1).convert("RGB")


def image_jpeg(message: Any) -> bytes:
    output = BytesIO()
    image_pil(message).save(output, "JPEG", quality=85)
    return output.getvalue()
