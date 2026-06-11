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

## Skills

Batch 1 (shipped — keyless / standard library):
- `hash-digest` — sha256 / sha512 / sha1 / md5 / sha3_256 / blake2b of text
- `text-stats` — word / character / line / sentence counts + reading time
- `json-csv` — JSON array of objects ⇄ CSV
- `entity-extract` — emails, URLs, IPv4, Bitcoin / Lightning, hashtags

Planned: `qr-generate`, `bolt11-decode`, `markdown-html`, `mempool-fees`, `btc-price`,
`weather`, `geocode`, `nostr-profile`, `pdf-text`, `image-convert`, `opentimestamps`.

## Tests

```bash
pip install -e '.[dev]'
pytest -q
```
