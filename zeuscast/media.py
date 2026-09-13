"""Prepares backgrounds for the LCD: JPEG stills and H.264 MP4 loops sized to the panel."""

from __future__ import annotations

import io
import shutil
import subprocess
from pathlib import Path

from PIL import Image, ImageOps

MAX_VIDEO_SECONDS = 30  # ZEUS CAST trims uploads to 30 s
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".gif"}


class MediaError(Exception):
    pass


def is_video(path: str | Path) -> bool:
    path = Path(path)
    if path.suffix.lower() != ".gif":
        return path.suffix.lower() in VIDEO_EXTENSIONS
    try:
        with Image.open(path) as im:
            return getattr(im, "n_frames", 1) > 1
    except OSError:
        return False


def _frame(image: Image.Image, width: int, height: int, mode: str) -> Image.Image:
    image = ImageOps.exif_transpose(image).convert("RGBA")
    if mode == "fill":
        image = ImageOps.fit(image, (width, height), Image.LANCZOS)
    else:
        image.thumbnail((width, height), Image.LANCZOS)
    canvas = Image.new("RGBA", (width, height), (0, 0, 0, 255))
    canvas.alpha_composite(image, ((width - image.width) // 2, (height - image.height) // 2))
    return canvas.convert("RGB")


def prepare_image(path: str | Path, width: int, height: int, mode: str = "fill") -> bytes:
    """Scale an image to the panel and encode it as JPEG (``mode`` is "fill" or "fit")."""
    try:
        with Image.open(path) as im:
            framed = _frame(im, width, height, mode)
    except OSError as e:
        raise MediaError(f"Can't read image {path}: {e}") from e
    out = io.BytesIO()
    framed.save(out, "JPEG", quality=92)
    return out.getvalue()


def ffmpeg_binary() -> str:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise MediaError("ffmpeg is not installed (sudo dnf install ffmpeg-free, or ffmpeg from RPM Fusion)")
    return ffmpeg


def h264_encoder(ffmpeg: str) -> str:
    result = subprocess.run([ffmpeg, "-hide_banner", "-encoders"], capture_output=True, text=True)
    for encoder in ("libx264", "libopenh264"):
        if f" {encoder} " in result.stdout:
            return encoder
    raise MediaError("ffmpeg has no H.264 encoder (install ffmpeg from RPM Fusion, or openh264)")


def prepare_video(path: str | Path, output: str | Path, width: int, height: int, mode: str = "fill") -> None:
    """Transcode a clip to the H.264/yuv420p MP4 the cooler plays."""
    ffmpeg = ffmpeg_binary()
    encoder = h264_encoder(ffmpeg)
    if mode == "fill":
        scale = f"scale={width}:{height}:force_original_aspect_ratio=increase:flags=lanczos,crop={width}:{height}"
    else:
        scale = (
            f"scale={width}:{height}:force_original_aspect_ratio=decrease:flags=lanczos,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:black"
        )
    quality = ["-crf", "23"] if encoder == "libx264" else ["-b:v", "2M"]
    command = [
        ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(path),
        "-t", str(MAX_VIDEO_SECONDS),
        "-vf", scale,
        "-an",
        "-c:v", encoder, *quality,
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
        str(output),
    ]  # fmt: skip
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0:
        raise MediaError(f"ffmpeg failed: {result.stderr.strip() or result.returncode}")


def video_first_frame(path: str | Path, width: int, height: int, mode: str = "fill") -> Image.Image | None:
    try:
        ffmpeg = ffmpeg_binary()
    except MediaError:
        return None
    result = subprocess.run(
        [ffmpeg, "-hide_banner", "-loglevel", "error", "-i", str(path), "-frames:v", "1", "-f", "image2pipe", "-c:v", "png", "-"],
        capture_output=True,
    )
    if result.returncode != 0 or not result.stdout:
        return None
    with Image.open(io.BytesIO(result.stdout)) as im:
        return _frame(im, width, height, mode)


def load_preview(path: str | Path, width: int, height: int, mode: str = "fill") -> Image.Image | None:
    """An RGB still of what the background will look like on the panel."""
    if is_video(path):
        return video_first_frame(path, width, height, mode)
    try:
        with Image.open(path) as im:
            return _frame(im, width, height, mode)
    except OSError:
        return None
