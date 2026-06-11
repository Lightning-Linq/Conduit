"""image-convert — convert an image between formats, with size/pixel guards.

Untrusted-binary input, so it's bounded: the base64 is size-capped before decode,
the decoded image is capped at 10 MB, dimensions are checked against a pixel cap
(and Pillow's decompression-bomb guard is tightened), and both input and output
formats are allowlisted to the common, well-exercised ones. Any decode failure
becomes a 400, not a 500.
"""

from __future__ import annotations

import base64
import binascii
import io

from PIL import Image

from app.registry import Skill, SkillError, register

_MAX_BYTES = 10 * 1024 * 1024  # 10 MB decoded input
_MAX_PIXELS = 24_000_000  # ~24 MP
# Allowlisted formats (Pillow names) for both input and output.
_FORMATS = {"PNG", "JPEG", "WEBP", "GIF", "BMP"}
_ALIASES = {"JPG": "JPEG"}

Image.MAX_IMAGE_PIXELS = _MAX_PIXELS  # Pillow raises on decompression bombs past this


def _norm_format(value: str) -> str:
    fmt = _ALIASES.get(value.strip().upper(), value.strip().upper())
    if fmt not in _FORMATS:
        raise SkillError(f"unsupported format {value!r}; choose from {sorted(_FORMATS)}")
    return fmt


def run(input_data: dict) -> dict:
    encoded = input_data.get("image_base64")
    target = input_data.get("to_format")
    if not isinstance(encoded, str) or not encoded:
        raise SkillError("`image_base64` (base64-encoded image) is required")
    if not isinstance(target, str):
        raise SkillError("`to_format` is required")
    out_format = _norm_format(target)
    if len(encoded) > _MAX_BYTES * 4 // 3 + 16:
        raise SkillError("image too large (max 10 MB)")
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise SkillError(f"`image_base64` is not valid base64: {exc}") from exc
    if len(raw) > _MAX_BYTES:
        raise SkillError("image too large (max 10 MB)")

    try:
        image = Image.open(io.BytesIO(raw))
        # Reject a disallowed format or an oversized image from the header alone,
        # BEFORE a full decode, so the parser surface stays the allowlist.
        if image.format not in _FORMATS:
            raise SkillError(f"unsupported input format {image.format!r}")
        if image.width * image.height > _MAX_PIXELS:
            raise SkillError("image dimensions exceed the pixel cap")
        image.load()  # only now fully decode an allowlisted, size-checked image
    except SkillError:
        raise
    except Exception as exc:  # not an image / bomb / truncated -> 400
        raise SkillError(f"could not read image: {exc}") from exc

    out_image = image
    if out_format == "JPEG" and image.mode in ("RGBA", "LA", "P"):
        out_image = image.convert("RGB")  # JPEG has no alpha channel
    buffer = io.BytesIO()
    try:
        out_image.save(buffer, format=out_format)
    except Exception as exc:
        raise SkillError(f"could not convert to {out_format}: {exc}") from exc
    return {
        "format": out_format,
        "width": image.width,
        "height": image.height,
        "encoding": "base64",
        "image": base64.b64encode(buffer.getvalue()).decode("ascii"),
    }


register(
    Skill(
        name="image-convert",
        description="Convert an image between PNG/JPEG/WEBP/GIF/BMP (base64 in and out).",
        handler=run,
        input_example={"image_base64": "iVBORw0KGgo...", "to_format": "JPEG"},
    )
)
