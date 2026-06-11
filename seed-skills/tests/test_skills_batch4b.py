"""Batch-4b skills: pdf-text and image-convert (untrusted-binary parsing)."""

import base64
import io

from PIL import Image


def _sample_pdf_b64() -> str:
    from fpdf import FPDF

    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("helvetica", size=24)
    try:
        pdf.cell(0, 10, text="Hello Conduit")
    except TypeError:  # older fpdf2 used `txt`
        pdf.cell(0, 10, txt="Hello Conduit")
    return base64.b64encode(bytes(pdf.output())).decode()


def _png_b64(size=(8, 8), color=(200, 30, 30)) -> str:
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode()


# ---- pdf-text ----


def test_pdf_text_extracts(client, paid):
    r = client.post("/skills/pdf-text", json=paid("pdf-text", {"pdf_base64": _sample_pdf_b64()}))
    out = r.json()["output"]
    assert out["pages"] == 1
    assert "Hello Conduit" in out["text"]


def test_pdf_text_rejects_bad_base64(client, paid):
    r = client.post("/skills/pdf-text", json=paid("pdf-text", {"pdf_base64": "!!!notb64"}))
    assert r.status_code == 400


def test_pdf_text_rejects_non_pdf(client, paid):
    junk = base64.b64encode(b"this is plainly not a pdf").decode()
    r = client.post("/skills/pdf-text", json=paid("pdf-text", {"pdf_base64": junk}))
    assert r.status_code == 400


# ---- image-convert ----


def test_image_convert_png_to_jpeg(client, paid):
    r = client.post(
        "/skills/image-convert",
        json=paid("image-convert", {"image_base64": _png_b64(), "to_format": "jpeg"}),
    )
    out = r.json()["output"]
    assert out["format"] == "JPEG"
    raw = base64.b64decode(out["image"])
    assert raw[:2] == b"\xff\xd8"  # JPEG start-of-image marker
    assert Image.open(io.BytesIO(raw)).format == "JPEG"


def test_image_convert_rejects_unknown_format(client, paid):
    r = client.post(
        "/skills/image-convert",
        json=paid("image-convert", {"image_base64": _png_b64(), "to_format": "tiff"}),
    )
    assert r.status_code == 400


def test_image_convert_rejects_non_image(client, paid):
    junk = base64.b64encode(b"not an image").decode()
    r = client.post(
        "/skills/image-convert",
        json=paid("image-convert", {"image_base64": junk, "to_format": "png"}),
    )
    assert r.status_code == 400


def test_image_convert_rejects_disallowed_input_format(client, paid):
    # A valid image in a NON-allowlisted format (TIFF) is refused from its header,
    # before a full decode — keeping the parser surface to the allowlist.
    buffer = io.BytesIO()
    Image.new("RGB", (8, 8), (0, 0, 0)).save(buffer, format="TIFF")
    tiff_b64 = base64.b64encode(buffer.getvalue()).decode()
    r = client.post(
        "/skills/image-convert",
        json=paid("image-convert", {"image_base64": tiff_b64, "to_format": "png"}),
    )
    assert r.status_code == 400


def test_image_convert_requires_to_format(client, paid):
    r = client.post(
        "/skills/image-convert",
        json=paid("image-convert", {"image_base64": _png_b64()}),
    )
    assert r.status_code == 400
