"""Batch-two skills: qr-generate, bolt11-decode, markdown-html."""

import base64

# A self-contained 2500-sat mainnet invoice minted with the bolt11 lib (fixed
# description / payment_hash). decode() does not reject by age, so it stays valid.
_INVOICE = (
    "lnbc25u1pj48ugqpp5kcampk0zxjggjkwyy0pq3s54x6dxna53yrjd6urn34wefsyahq8qsp5jy94h2"
    "k3xjkq8jhhm083tz6eggm25sf489nctfzudjqplsv48zwsdpjgdhkuer4d96zqum9v4jzqumtd9kxcg"
    "r5v4ehggrfdemx76trv59ctu9ys5zaf0lkcjegq4820nkqlhxgtgxedgcry52jdywdygxu79y5ccdly"
    "tqlk4z9uvawjwy66jrw90a72ks7r8457hu9gv59fmpycqzxlrfa"
)
_INVOICE_PAYMENT_HASH = "b63bb0d9e234908959c423c208c295369a69f69120e4dd70738d5d94c09db80e"


def test_qr_generate_returns_png(client, paid):
    r = client.post("/skills/qr-generate", json=paid("qr-generate", {"data": "hello"}))
    out = r.json()["output"]
    assert out["format"] == "png"
    assert base64.b64decode(out["image"])[:8] == b"\x89PNG\r\n\x1a\n"  # PNG magic


def test_qr_generate_rejects_empty(client, paid):
    r = client.post("/skills/qr-generate", json=paid("qr-generate", {"data": ""}))
    assert r.status_code == 400


def test_bolt11_decode_fields(client, paid):
    r = client.post("/skills/bolt11-decode", json=paid("bolt11-decode", {"invoice": _INVOICE}))
    out = r.json()["output"]
    assert out["network"] == "bc"
    assert out["amount_sat"] == 2500
    assert out["description"] == "Conduit seed skill test invoice"
    assert out["payment_hash"] == _INVOICE_PAYMENT_HASH


def test_bolt11_decode_rejects_garbage(client, paid):
    r = client.post(
        "/skills/bolt11-decode",
        json=paid("bolt11-decode", {"invoice": "not-an-invoice"}),
    )
    assert r.status_code == 400


def test_markdown_html_renders(client, paid):
    r = client.post(
        "/skills/markdown-html",
        json=paid("markdown-html", {"markdown": "# Hi\n\n**bold**"}),
    )
    out = r.json()["output"]
    assert "<h1>" in out["html"]
    assert "<strong>bold</strong>" in out["html"]


def test_markdown_html_sanitizes_script_by_default(client, paid):
    r = client.post(
        "/skills/markdown-html",
        json=paid("markdown-html", {"markdown": "ok <script>alert(1)</script>"}),
    )
    out = r.json()["output"]
    assert "<script>" not in out["html"]
    assert out["sanitized"] is True


def test_markdown_html_allow_raw_html_opts_out(client, paid):
    r = client.post(
        "/skills/markdown-html",
        json=paid("markdown-html", {"markdown": "<b>raw</b>", "allow_raw_html": True}),
    )
    out = r.json()["output"]
    assert "<b>raw</b>" in out["html"]
    assert out["sanitized"] is False
