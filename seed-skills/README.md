# Conduit Seed Skills

Keyless reference skills for [Conduit](https://github.com/Lightning-Linq/Conduit) — a
small FastAPI webhook server any Conduit node can list and sell over Lightning. It
doubles as the **public skill-provider template**: fork it, add a skill, point a
Conduit listing at your URL.

"Keyless" means no third-party API keys. Skills use the standard library, public
no-key APIs, or self-contained packages — nothing to provision before you deploy.

## The contract

Conduit POSTs to your skill's `endpoint_url` after the consumer's Lightning payment
settles:

```json
{
  "execution_id": "…",
  "skill_name": "hash-digest",
  "input_data": { "text": "hello world" },
  "payment_proof": { "payment_hash": "…", "payment_preimage": "…" },
  "timestamp": "2026-…Z"
}
```

You return — within 30s, as `2xx` JSON:

```json
{ "output": { "algorithm": "sha256", "hex": "b94d27b9…" } }
```

The server verifies `SHA256(payment_preimage) == payment_hash` before running a skill
(disable with `REQUIRE_PAYMENT_PROOF=false` for local testing). See `app/payment.py`
for the production hardening note — bind the hash to an invoice you actually issued.

## Run it

```bash
pip install -e .              # or: pip install -e '.[all]' for every skill
REQUIRE_PAYMENT_PROOF=false uvicorn app.main:app --reload
curl localhost:8000/skills    # the catalog
```

Routes:
- `POST /skills/{name}` — run one skill (the canonical per-skill `endpoint_url`).
- `POST /execute` — run the skill named in the body (one URL for all skills).
- `GET /skills` — catalog (names, descriptions, example inputs).
- `GET /` — health + skill count.

## Add a skill

Drop a module in `app/skills/`:

```python
from app.registry import Skill, SkillError, register

def run(input_data: dict) -> dict:
    name = input_data.get("name")
    if not isinstance(name, str):
        raise SkillError("`name` (string) is required")
    return {"greeting": f"hello {name}"}

register(Skill(name="greet", description="Greet someone.", handler=run,
               input_example={"name": "alice"}))
```

Add it to the import line in `app/skills/__init__.py`. Handlers may be sync or async;
raise `SkillError` for bad input (→ HTTP 400). Then list it on your Conduit node with
`endpoint_url = https://<host>/skills/greet`.

## Deploy

Conduit's SSRF guard refuses private-IP and non-HTTPS webhooks, so this must run on a
**public host over HTTPS** — not a localhost shortcut beside the node. Recommended:
this server on a small VPS (behind a TLS reverse proxy); your Lightning node stays
wherever it already lives.

## Getting listed on the marketplace

Skills are discovered over Nostr: publish each one as a `kind-38383` event in Conduit's
format (name, description, category, price, provider, Lightning address, and the
`endpoint_url`), and the [lightninglinq.ai marketplace](https://lightninglinq.ai/marketplace)
reads them live, falling back to a committed `skills.json` snapshot.

Because `kind-38383` is a shared Nostr kind that other apps also use, the marketplace
currently shows skills from a curated set of provider keys. To be listed today, open an
issue or PR adding your Nostr pubkey to the `PROVIDERS` list in `docs/marketplace.html`.

**Planned (open discovery):** providers tag their skill events with a `conduit` label (a
`t` tag) and the marketplace filters on that tag, so any provider can list without being
added by hand. Provider verification (NIP-05) and payment-bound reputation help consumers
tell quality listings apart.

## Skills

Batch 1 (shipped — keyless / standard library):
- `hash-digest` — sha256 / sha512 / sha1 / md5 / sha3_256 / blake2b of text
- `text-stats` — word / character / line / sentence counts + reading time
- `json-csv` — JSON array of objects ⇄ CSV
- `entity-extract` — emails, URLs, IPv4, Bitcoin / Lightning, hashtags

Batch 2 (shipped — light libraries; install the extra to enable each):
- `qr-generate` — text/URI → QR-code PNG (base64) — `pip install -e '.[qr]'`
- `bolt11-decode` — decode a BOLT11 Lightning invoice — `.[bolt11]`
- `markdown-html` — Markdown → HTML, HTML-sanitized by default — `.[markdown]`

Batch 3 (shipped — public no-key APIs; needs the `net` extra). Each calls a fixed
host with user input only in query params, so there is no user-controlled-host SSRF:
- `mempool-fees` — recommended Bitcoin fee rates (sat/vB) — mempool.space
- `btc-price` — current BTC price in a fiat currency — mempool.space
- `weather` — current conditions for a lat/lon — Open-Meteo
- `geocode` — place name → coordinates — Open-Meteo

Batch 4a (shipped — new transports; relay/calendar hosts are fixed, not user-supplied):
- `nostr-profile` — Nostr kind-0 profile lookup by pubkey (hex/npub), websocket — `.[net]`
- `opentimestamps` — OpenTimestamps proof committing a SHA-256 hash to Bitcoin — `.[timestamps]`

Batch 4b (shipped — untrusted-binary parsing, hardened with size + pixel caps and
format allowlists; install the extra):
- `pdf-text` — extract text from a PDF, ≤10 MB / ≤50 pages — `.[pdf]`
- `image-convert` — convert between PNG/JPEG/WEBP/GIF/BMP, ≤24 MP — `.[image]`

All 15 reference skills are shipped.

## Tests

```bash
pip install -e '.[dev]'
pytest -q
```
