"""qr-generate — render text/data as a QR-code PNG (base64)."""

from __future__ import annotations

import base64
import io

import qrcode

from app.registry import Skill, SkillError, register

_MAX_CHARS = 2000  # bounds the payload; also well within QR capacity


def _int_in(value: object, lo: int, hi: int) -> bool:
    # bool is an int subclass — reject it so `true` can't pass as 1.
    return isinstance(value, int) and not isinstance(value, bool) and lo <= value <= hi


def run(input_data: dict) -> dict:
    data = input_data.get("data")
    if not isinstance(data, str) or not data:
        raise SkillError("`data` (non-empty string) is required")
    if len(data) > _MAX_CHARS:
        raise SkillError(f"`data` too long (max {_MAX_CHARS} chars)")
    box_size = input_data.get("box_size", 10)
    border = input_data.get("border", 4)
    if not _int_in(box_size, 1, 40):
        raise SkillError("`box_size` must be an integer in 1..40")
    if not _int_in(border, 0, 20):
        raise SkillError("`border` must be an integer in 0..20")

    qr = qrcode.QRCode(box_size=box_size, border=border)
    qr.add_data(data)
    qr.make(fit=True)
    image = qr.make_image()
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return {
        "format": "png",
        "encoding": "base64",
        "image": base64.b64encode(buffer.getvalue()).decode("ascii"),
    }


register(
    Skill(
        name="qr-generate",
        description="Render text or a URI as a QR-code PNG, returned base64-encoded.",
        handler=run,
        input_example={"data": "lightning:lnbc25u1p..."},
    )
)
