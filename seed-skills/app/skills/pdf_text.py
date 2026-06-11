"""pdf-text — extract text from a base64-encoded PDF.

Untrusted-binary input, so it's bounded: the base64 is size-capped before decode,
the decoded PDF is capped at 10 MB, and at most 50 pages are read. pypdf does not
execute embedded JavaScript, and any parse failure becomes a 400 rather than a 500.
"""

from __future__ import annotations

import base64
import binascii
import io

import pypdf

from app.registry import Skill, SkillError, register

_MAX_BYTES = 10 * 1024 * 1024  # 10 MB decoded
_MAX_PAGES = 50


def run(input_data: dict) -> dict:
    encoded = input_data.get("pdf_base64")
    if not isinstance(encoded, str) or not encoded:
        raise SkillError("`pdf_base64` (base64-encoded PDF) is required")
    if len(encoded) > _MAX_BYTES * 4 // 3 + 16:  # reject before allocating the decode
        raise SkillError("PDF too large (max 10 MB)")
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise SkillError(f"`pdf_base64` is not valid base64: {exc}") from exc
    if len(raw) > _MAX_BYTES:
        raise SkillError("PDF too large (max 10 MB)")
    try:
        reader = pypdf.PdfReader(io.BytesIO(raw))
        page_count = len(reader.pages)
        texts = [reader.pages[i].extract_text() or "" for i in range(min(page_count, _MAX_PAGES))]
    except Exception as exc:  # malformed / encrypted PDF -> 400, not 500
        raise SkillError(f"could not read PDF: {exc}") from exc
    return {
        "pages": page_count,
        "pages_extracted": len(texts),
        "truncated": page_count > _MAX_PAGES,
        "text": "\n\n".join(texts).strip(),
    }


register(
    Skill(
        name="pdf-text",
        description="Extract text from a PDF (base64-encoded), up to 50 pages.",
        handler=run,
        input_example={"pdf_base64": "JVBERi0xLjQ..."},
    )
)
