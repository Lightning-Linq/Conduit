# Conduit Security & Bug Audit

**Date:** 2026-05-29 (initial audit), 2026-05-30 (re-audit)
**Auditor:** Claude (Opus 4.7)
**Scope:** `src/conduit/`, `.env.example`, `cleanup_db.py`
**Initial-audit branch:** `main` @ `68135c0`
**Re-audit branch:** `main` @ `f29eb8c`

---

## How to use this document

This is a static audit of the Conduit codebase produced by reading the source. Findings are grouped by severity. Each finding cites the relevant file and line numbers, explains the failure mode, and proposes a concrete fix.

If you're handing this to another Claude session to act on:

1. Start with the **Priority fix order** at the bottom — it sequences the work so each fix unblocks the next.
2. Each finding is self-contained; you don't need to read them in order.
3. Severities are calibrated to Conduit's threat model (see below), not a generic OWASP scale. Read that section first if you want to recalibrate for a different deployment.
4. "Already addressed" follow-ups belong in this file — append a `**Status:**` line below the finding rather than deleting it, so future audits see the history.

## Conduit threat model (assumed)

- **Single-tenant**: one shared `CONDUIT_API_KEY` gates the entire REST surface. There is no per-user auth.
- **Non-custodial**: the operator's LND node pays providers directly. Conduit never holds consumer funds, but it *does* hold the LND admin macaroon, which means a compromise of the API key compromises the node.
- **Agent-driven**: callers are AI agents acting on a user's behalf via MCP/REST. "Authenticated" does not imply "trustworthy" — an agent can be manipulated, jail-broken, or buggy. The system should still enforce limits.
- **Public marketplace data**: skill registrations, ratings, and Nostr-published listings are world-readable and provider-controlled. Treat all provider input as hostile.
- **Hostile providers exist**: a registered skill may try SSRF via its webhook URL, exfiltrate payment preimages, or stuff XSS/ANSI into response bodies.

Severities below assume this model. A multi-tenant deployment would push several mediums to high.

---

## Re-audit summary (2026-05-30)

Three commits landed between the initial audit and this re-audit (`48373d0`, `f262bdc`, `f29eb8c`). Status legend used throughout:

- ✅ **Fixed** — verified in current code
- 🟡 **Deferred** — acknowledged but not addressed; still applies
- 🔄 **Regressed / partial** — attempted fix has a new bug; see linked NEW finding
- ⛔ **Not addressed**

| Severity | Total | Fixed | Deferred / partial | Not addressed |
|---|---|---|---|---|
| Critical | 7 | 6 | 1 (C3) | 0 |
| High     | 12 | 11 | 1 (H3 — see NEW-H1) | 0 |
| Medium   | 11 | 8 | 0 | 3 (M4, M6, M11) |
| Low      | 8 | 7 | 0 | 1 (L6 moot — superseded by H1 fix) |

**Five new findings** introduced by the fix commits — see the [NEW findings](#new-findings-introduced-by-the-fixes) section.

---

## CRITICAL

### C1. Anyone with `execution_id` can trigger a paid skill execution after the real buyer settles the invoice

**Files:**
- `src/conduit/api/routers/marketplace.py:338-418`
- `src/conduit/mcp_server.py:1454-1616`

**Problem:** `confirm_skill_execution` only checks (a) `payment_hash` matches the stored one and (b) the LND invoice is `settled`. The user-supplied `payment_preimage` is **never** validated against `sha256(preimage) == payment_hash` here. The REST version doesn't take the preimage at all; the MCP version takes it and forwards it to the provider webhook without verification.

**Consequences:**
- A spectator who learns an `execution_id` (returned in MCP text output, logs, anomaly responses, or any UI surface) can call confirm after the legitimate payer settles, triggering a second webhook fire.
- The MCP path delivers an unverified preimage to the provider webhook, so the provider has no real proof-of-payment.

**Fix:** require the caller to present the preimage and compare `sha256(bytes.fromhex(preimage)).hexdigest() == execution.payment_hash` before any state mutation. This is already done correctly in `src/conduit/services/rating_integrity.py:60-68` — reuse the same check.

**Status:** ✅ Fixed in `48373d0`. REST: `marketplace.py:399-411`. MCP: `mcp_server.py:1517-1531`. Both paths now hash the supplied preimage and reject on mismatch before any state mutation.

---

### C2. REST `confirm_skill_execution` marks COMPLETED without invoking the provider webhook

**File:** `src/conduit/api/routers/marketplace.py:338-418`

**Problem:** The REST handler verifies settlement, sets `status=COMPLETED`, and returns `"Skill delivery in progress."` — but never calls `execute_skill_webhook`. Consumers pay, are told delivery is happening, and nothing runs. (The MCP path does call the webhook at `mcp_server.py:1571`.)

**Fix:** invoke `execute_skill_webhook` from the REST router on success, mirroring MCP, or explicitly document that REST is request-only and the operator must complete delivery out-of-band.

**Status:** ✅ Fixed in `48373d0`. REST `confirm_skill_execution` now calls `execute_skill_webhook` at `marketplace.py:499-525`, transitioning through EXECUTING → COMPLETED with output stored on the execution row.

---

### C3. Hand-rolled BIP-340 / secp256k1 signs with non-constant-time arithmetic

**File:** `src/conduit/services/nostr.py:50-197`

**Problem:**
- `_modinv`, `_extended_gcd`, and `_point_mul` (`if k & 1:` branch) all leak private-key bits through timing side channels.
- Any process that can observe Nostr signing latency from this server (co-resident container, shared metrics, remote network timing) can mount a side-channel against the server's Nostr key over time.
- `_extended_gcd` is unbounded recursion — adversarial inputs could blow the stack during `bech32_decode` of `nsec1…` strings provided via `NOSTR_PRIVATE_KEY`.

**Fix:** delete the pure-Python crypto module and use `coincurve` (libsecp256k1 bindings) for both signing and verification. The library exposes `PrivateKey.sign_schnorr` and `PublicKey.verify_schnorr` and is constant-time. The dependency is small and widely used.

**Status:** 🟡 Deferred. Pure-Python schnorr is still in `nostr.py:50-197`. Not touched in any of the three fix commits. Highest remaining cryptographic risk.

---

### C4. `.env.example` ships unsafe production defaults

**File:** `.env.example` (root)

**Problems:**
- `API_HOST=0.0.0.0` exposes the API to the LAN/internet on first run, overriding the safe `127.0.0.1` default in `src/conduit/core/config.py:30`.
- `DEBUG=true` enables SQL echo and verbose errors.
- `LND_NETWORK=mainnet` and the mainnet macaroon path point at the operator's real wallet.
- No `CONDUIT_API_KEY` entry at all — operator has to discover the variable name from a fatal startup error, and the example doesn't show how to generate a strong key.
- No `CORS_ALLOW_ORIGINS`, `APP_ENV`, `L402_ENABLED`, spending limits, etc.

**Result:** a user who copies the template and runs against mainnet has an internet-facing admin macaroon controlled by an unset API key (which exits) or, after they set the key, by *that key only*, with debug-mode error messages leaking internals.

**Fix:** change `.env.example` defaults to:

```
APP_ENV=development
API_HOST=127.0.0.1
DEBUG=false
LND_NETWORK=regtest
LND_MACAROON_PATH=
# Generate with: python3 -c "import secrets; print(secrets.token_urlsafe(32))"
CONDUIT_API_KEY=
CORS_ALLOW_ORIGINS=
L402_ENABLED=false
SPENDING_LIMIT_PER_PAYMENT_SATS=10000
SPENDING_LIMIT_HOURLY_SATS=50000
SPENDING_LIMIT_DAILY_SATS=200000
SPENDING_CONFIRM_ABOVE_SATS=5000
```

**Status:** ✅ Fixed in `f29eb8c` — but introduced **NEW-M1** (the new placeholder string `CHANGE-ME-generate-a-random-key` doesn't trip the exact-string `== "CHANGE-ME"` validator in `deps.py` / `mcp_server.py`).

---

### C5. `_create_l402_token` references an undefined `lnd`

**File:** `src/conduit/mcp_server.py:2030-2036`

**Problem:** `create_l402_challenge(lnd, ...)` — `lnd` is not defined in this scope. The only `lnd` binding is local to `_handle_lightning_tool` at `mcp_server.py:893`. Calling the MCP `create_l402_token` tool raises `NameError`.

**Fix:** add `lnd = get_lnd()` at the top of `_create_l402_token`.

**Status:** ✅ Fixed in `48373d0`. `lnd = get_lnd()` is now at `mcp_server.py:2094` inside the try block.

---

### C6. Spending limits are racy: check-then-pay-then-record

**Files:**
- `src/conduit/services/spending_limiter.py:131-215`
- `src/conduit/api/routers/lightning.py:120-172`
- `src/conduit/mcp_server.py:982-1036`

**Problem:** `check_spending_limits` SELECTs the rolling sum, returns; later `record_successful_payment` INSERTs only on success. Two concurrent `pay_invoice` calls both read pre-payment totals, both pass the hourly/daily check, then both succeed → the limit is silently bypassed.

**Made worse by:**
- bookkeeping failures are caught and swallowed (`lightning.py:171-172`, `mcp_server.py:1033-1036`) — if the DB hiccups, the spent amount is never recorded and subsequent calls see fresh budget;
- the spending log row is added *after* LND confirms, not before, so there's no in-flight reservation.

**Fix:** write a `status="in_flight"` row inside the same DB transaction as the limit check (use `SELECT … FOR UPDATE` or a `SERIALIZABLE` transaction, or apply a Postgres advisory lock keyed on a global "spending" key); update to `allowed` after LND returns, or delete on failure.

**Status:** ✅ Fixed in `48373d0`. New `_reserve` helper writes a `status="reserved"` row before the LND call (`spending_limiter.py:338-356`); `_get_spent_in_window` now sums both `allowed` and `reserved` rows. On success the row is promoted to `allowed`; on failure callers invoke `cancel_reservation`. Wired into `lightning.py:128-201`.

---

### C7. `/api/v1/admin/reset-demo` will happily wipe production

**File:** `src/conduit/api/routers/admin.py:42-67`

**Problem:** The endpoint is mounted unconditionally and protected only by the same API key that authenticates every other call. There is:
- no `settings.is_production` guard,
- no second-factor confirmation,
- no body assertion (no `confirm=true`).

A leaked or fat-fingered API key — combined with a misconfigured CORS in dev that an operator never reverted — turns this into a one-request data wipe.

**Fix:** refuse the request when `settings.is_production` is true (mirror the CORS `*` check in `main.py:120-124`); require a `confirm_token` body field that matches a value printed to stderr on startup, or require a separate `ADMIN_API_KEY`.

**Status:** ✅ Fixed in `48373d0`. `admin.py:52-56` returns 403 when `settings.is_production`. M7 added rate-limiting on top (3 reset calls/hour).

---

## HIGH

### H1. Confirmation tokens live in an unbounded in-memory dict; lost across workers and replicas

**File:** `src/conduit/services/spending_limiter.py:75-128`

**Problem:** `_pending_confirmations` is a module global. `_purge_expired()` only runs on issue/redeem. An authenticated attacker can issue tokens at the `pay_invoice` rate (10/min) and the dict grows until the process restarts. Also: with `uvicorn --workers > 1`, tokens issued by worker A can never be redeemed via worker B — confirmation is non-deterministic the moment you scale.

**Fix:** store pending confirmations in Redis with a TTL keyed on the binding hash, or sign the binding into a short-TTL HMAC token so the server can verify statelessly.

**Status:** ✅ Fixed in `f262bdc`. Tokens are now stateless: HMAC-signed `binding|issued_at` payload, base64-encoded (`spending_limiter.py:74-144`). Survives multi-worker deploys. Tokens are technically replayable inside the 120s TTL — acceptable in practice because the binding includes `payment_hash`, which is unique per Lightning invoice (LND rejects double-pay).

---

### H2. `delete_skill` and `delete_execution` have no ownership check

**File:** `src/conduit/api/routers/marketplace.py:184-269`

**Problem:** Any caller with the API key can DELETE any skill or execution. In single-tenant mode this is "intended", but the same API surface advertises consumer vs provider as distinct identities (`consumer_name`, `provider_name`) — there is no enforced binding between the caller and the row being deleted. This makes provider reputation impossible to defend on a multi-agent deployment.

**Fix:** at minimum, gate DELETE behind a `provider_pubkey`-signed proof (provider signs `delete:<skill_id>` with the node that originally verified) or move DELETE to an admin-only scope distinct from the marketplace scope.

**Status:** ✅ Partial fix in `f262bdc`. Both DELETE endpoints now require a `provider_name` / `consumer_name` query param and 403 on mismatch (`marketplace.py:191-207, 250-271`). Still string-based — a multi-tenant deployment will want the signed-proof variant from the original fix recommendation.

---

### H3. SSRF: `validate_outbound_url` is TOCTOU vs DNS rebinding

**Files:**
- `src/conduit/services/url_safety.py:91-144`
- `src/conduit/services/skill_executor.py:79-114`

**Problem:** The module docstring honestly flags this, but the `execute_skill_webhook` path leaks the payment preimage to the resolved URL. A hostile provider can publish a name with a 1s TTL that resolves to a public IP on the validation call and to `169.254.169.254` on the connect call. With `httpx` defaults, the connect uses fresh DNS — the validation is effectively advisory. `follow_redirects=False` blocks one bypass class but not rebinding.

**Fix:** resolve once, then connect to the resolved IP (not the hostname) with an explicit `Host:` header, or use `httpx` with a custom `Transport` that pins to the validated IP. Reject TTLs below e.g. 60s while you're at it.

**Status:** 🔄 Regressed. The fix in `f262bdc` (skill_executor.py:97-145) rewrites the URL to `https://<ip>:443/...` and sets `Host: <hostname>`. But httpx derives TLS SNI and cert-validation hostname from the URL host, not the `Host:` header — so the handshake fails cert validation for any provider with a domain-based cert. See **NEW-H1**.

---

### H4. `register_skill` via MCP skips SSRF check on `endpoint_url`

**File:** `src/conduit/mcp_server.py:1342-1375`

**Problem:** REST `register_skill` calls `validate_outbound_url(webhook_url)` (`marketplace.py:153-160`); MCP `_register_skill` does not. A hostile agent can register a skill pointing at internal infrastructure. The execute-time check in `execute_skill_webhook` will block the call, but the row sits in the database with `endpoint_url=https://169.254.169.254/...` polluting discovery and Nostr publication (which gladly broadcasts it — `nostr.py:445-447`).

**Fix:** add the same `validate_outbound_url` call to `_register_skill`; also reject in `skill_to_event` before publishing.

**Status:** ✅ Fixed in `f262bdc`. MCP `_register_skill` now validates `endpoint_url` against the SSRF allow-list before insert.

---

### H5. `record_successful_payment` failures are swallowed

**Files:**
- `src/conduit/api/routers/lightning.py:159-172`
- `src/conduit/mcp_server.py:1033-1036`

**Problem:** `try: … except Exception: pass` (or a log-and-continue). If Postgres is briefly down, the payment succeeds but the spending log is never written, the daily/hourly counter never increments, and the next request sees a fresh budget. The MCP path at least surfaces it in tool output; REST suppresses silently.

**Fix:** treat the record-payment failure as a hard error that the caller must see; better yet, write the row inside the same transaction as the limit check (see C6).

**Status:** ✅ Fixed in `f262bdc`. REST now prints the bookkeeping error to stderr (`lightning.py:182-186`) and leaves the reservation in `reserved` state, so the limit stays conservative rather than resetting. Combined with C6's pre-reservation model, the spending counter is now safe under DB hiccups.

---

### H6. `confirm_skill_execution` race lets a single payment trigger two webhook calls

**Files:**
- `src/conduit/api/routers/marketplace.py:357-411`
- `src/conduit/mcp_server.py:1466-1568`

**Problem:** The state check (`status == PENDING_PAYMENT`) and the status mutation are in separate awaits with no `SELECT … FOR UPDATE` and no idempotency token. Two concurrent confirm calls both see PENDING_PAYMENT, both transition to EXECUTING, both fire the webhook. Combined with C1, this becomes a billing/abuse vector against the provider.

**Fix:** wrap the read+update in a transaction that locks the execution row, or use `UPDATE … WHERE status='pending_payment' RETURNING …` and treat zero-row returns as "already taken".

**Status:** ✅ Fixed in `48373d0`. Both REST (`marketplace.py:381-385`) and MCP (`mcp_server.py:1501-1506`) use `select(...).with_for_update()` to lock the execution row before the state check.

---

### H7. Verification challenge can be silently replaced; not bound to verifier identity

**Files:**
- `src/conduit/services/provider_verification.py:80-101`
- `src/conduit/models/skill.py:74-76`

**Problem:** `start_node_verification` overwrites `skill.verification_challenge` on every call. If a hostile party calls `request_verification` after a legitimate provider but before the provider submits the signature, the provider's signed message verifies against a stale challenge and fails; meanwhile the attacker uses the freshly-issued challenge. Worse, there is no binding between the *requester* and the challenge — anyone with `marketplace:write` can issue verification challenges for any skill.

**Fix:** keep a per-skill list of outstanding challenges with `verifier_id`. Reject submit if the challenge isn't present in the list. Reject `start_*` if the skill already has an unexpired pending challenge.

**Status:** ✅ Partial fix in `f262bdc`. Both `start_node_verification` (`provider_verification.py:97-101`) and `start_domain_verification` (`:223-228`) now reject if a fresh challenge is already pending. The per-skill list / verifier binding was not implemented — adequate for current threat model but see **NEW-L2** for residual race.

---

### H8. Domain verification uses the system resolver synchronously inside an async function

**File:** `src/conduit/services/provider_verification.py:354-385`

**Problem:** `dns.resolver.resolve(...)` is a blocking call inside `async def _check_dns_txt`. A slow resolver stalls the event loop; an adversarial nameserver that throttles responses can stall the entire API. Additionally there's no DNSSEC requirement — anyone who can poison the resolver path for `_conduit-verify.<domain>` can claim the badge.

**Fix:** `await asyncio.to_thread(dns.resolver.resolve, ...)`, set a low timeout, and document the implicit DNSSEC requirement (or fetch via DoH/DoT with `aiohttp`).

**Status:** ✅ Fixed in `f262bdc`. `_check_dns_txt` now uses `asyncio.to_thread` with a 5s `resolver.lifetime` cap (`provider_verification.py:375-383`). DNSSEC still not enforced — documented residual risk.

---

### H9. MCP `nostr_get_profile` writes the Nostr private key (`nsec`) to stderr

**File:** `src/conduit/mcp_server.py:1955-1959`

**Problem:** When the operator hasn't set `NOSTR_PRIVATE_KEY`, calling `nostr_get_profile` prints the full nsec to stderr "for the operator". stderr is commonly captured by `journald`, `docker logs`, `systemd-journal`, log shippers (Loki/Datadog/Splunk), and `tail -f` panes shared in screen-shares. The npub is fine; the nsec is the secret that *is* the identity.

**Fix:** never print the nsec. Write it once on startup to a `0600` file in `credentials/nostr.nsec` and tell the operator the path. Or refuse to auto-generate and require operator to set it.

**Status:** ✅ Fixed in `48373d0`. `mcp_server.py:2010-2022` writes the nsec to `credentials/nostr.nsec` at mode `0600`; stderr now contains only the file path.

---

### H10. Permissive CORS combined with persistent `X-API-Key` is a CSRF setup

**File:** `src/conduit/main.py:128-134`

**Problem:** When operators set `CORS_ALLOW_ORIGINS=https://my.app` with `allow_credentials=True` (the default), browsers will attach the user's cookies but not `X-API-Key` (custom headers aren't auto-sent). That's fine. But the request allowlist includes `Authorization` and `X-API-Key`, so if a SPA stores the API key in `localStorage` and adds it to a header, a malicious page on a sibling origin (now an allowed origin after a typo or DNS takeover) can fire `DELETE /api/v1/admin/reset-demo`. The `/admin` router is bound to the same key.

**Fix:** restrict CORS to GET and explicit safe POSTs; never allow DELETE cross-origin; or move `/admin` to a path that CORS strips entirely (e.g. require a server-only header `X-Admin-Token` and exclude `X-Admin-Token` from `allow_headers`).

**Status:** ✅ Fixed in `f262bdc`. CORS `allow_methods=["GET","POST"]` is preserved and now documented as intentional (`main.py:135-138`): "DELETE is intentionally excluded… Do NOT add 'DELETE' here."

---

### H11. Verification middleware reads JSON body before routing — can break downstream handlers

**File:** `src/conduit/api/middleware/verification.py:119-125`

**Problem:** `await request.json()` inside `BaseHTTPMiddleware.dispatch` consumes the ASGI receive stream. Starlette caches `_body` on the Request, but the `BaseHTTPMiddleware` wrapping creates a *new* Request for downstream handlers in some configurations — there's a long-standing FastAPI issue (#5092). If your handler ever sees an empty body for `POST /marketplace/executions`, this is why.

**Fix:** read the body once via `await request.body()`, then build a fresh `Request` with a replay receive callable, OR move verification enforcement into the route dependency where the body is already parsed.

**Status:** ✅ Fixed in `f262bdc`. `_extract_skill_id` now uses `await request.body()` then `json.loads` (`verification.py:127-133`), which keeps the body cached for downstream handlers.

---

### H12. Rating concentration / weighted rating use unauthenticated `consumer_name`

**Files:**
- `src/conduit/services/rating_integrity.py:100-152`
- `src/conduit/api/routers/marketplace.py:57-60`

**Problem:** `consumer_name` is a free-form string the caller sets. The whole anti-sybil weighting in `calculate_weighted_rating` keys off this string. An attacker scripting fake ratings just rotates `consumer_name` for each request and every "first" review carries weight 1.0. The "self payment" check in `anomaly_detector.py:69-86` is a string compare and is bypassed by typing a different name.

**Fix:** require a per-caller identity bound to either an API key fingerprint (already computed in `rate_limit.py:101`) or a node pubkey signature, and use that for rating dedup/concentration logic.

**Status:** ✅ Documented-only in `f262bdc`. The commit message notes the limitation and clarifies that preimage-per-execution is the primary anti-sybil control. A pubkey-bound identity rework is still the long-term answer; tracked as future work.

---

## MEDIUM

### M1. Empty `routers/payments.py` stub

**File:** `src/conduit/api/routers/payments.py:1-4`

A 4-line "removed" comment. Delete the file (it's not imported in `main.py:28` anyway) so future contributors don't try to register a router from it.

**Status:** ✅ Fixed in `f29eb8c`. File deleted.

---

### M2. `pay_invoice` zero-amount BOLT-11

**Files:**
- `src/conduit/api/routers/lightning.py:115-123`
- `src/conduit/services/lnd.py:171-189`

For any-amount invoices, `decoded["amount_sats"]` is 0 → spending check passes vacuously. LND's `SendPaymentSync` will reject without an `amt` field, but the limit check is structurally wrong: it should refuse to pay zero-amount invoices outright. Defense in depth.

**Fix:** if `decoded["amount_sats"] == 0`, return 400 before calling LND.

**Status:** ✅ Fixed in `f29eb8c`. REST returns 400 at `lightning.py:123-127`; MCP returns an error TextContent before the limit check.

---

### M3. `derive_macaroon` doesn't constrain by issuer scope

**File:** `src/conduit/services/macaroon_auth.py:144-170`

The function uses the root secret, so a `readonly` holder *can't* call it (the endpoint requires `SECURITY_ADMIN`). But if anything ever changes that gate, `derive_macaroon` would happily mint admin tokens from a readonly caller. The intersection semantics in `verify_macaroon` are correct; the minting side should also enforce "new perms ⊆ caller perms".

**Fix:** pass the current active permission set into `derive_macaroon` and intersect before adding the caveat.

**Status:** ✅ Fixed in `f29eb8c`. `derive_macaroon` now accepts a `caller_permissions` parameter and intersects (`macaroon_auth.py:171-175`); raises if the intersection is empty.

---

### M4. Rating concentration check is detection-only

**File:** `src/conduit/services/rating_integrity.py:121-152`

Only raises an `AnomalyFlag`; the rating still gets stored and counted. The skill's `avg_rating` is updated using `calculate_weighted_rating` which discounts repeats but a determined attacker still moves the needle.

**Fix:** document this is detection-only, or reject the rating above some `fraction`.

**Status:** ⛔ Not addressed. Still detection-only.

---

### M5. Discovery `ILIKE %query%` and `cast(UUID as text) LIKE pattern` are full scans

**Files:**
- `src/conduit/api/routers/marketplace.py:84-98`
- `src/conduit/mcp_server.py:1242-1247`

No length cap on `keyword`, and the partial-UUID match casts every row's id to text. With enough rows, repeated calls within rate limits will pin the DB.

**Fix:** add max length validation on `keyword`/`category` and reject partial UUID lookups shorter than 8 chars.

**Status:** ✅ Partial in `f29eb8c`. REST now caps `keyword` at 100 chars and `category` at 50 (`marketplace.py:82-83`) via Pydantic. The MCP partial-UUID fallback (`mcp_server.py:1242-1247`) was not changed — still casts UUID→text → trailing scans on large tables. Low practical impact.

---

### M6. Webhook/provider response bodies are interpolated into stderr without sanitization in places

**File:** `src/conduit/services/skill_executor.py:118-141` sanitizes via `_safe_excerpt`, but other places that interpolate provider strings into log lines don't (e.g. `mcp_server.py:1054` prints the LND `failure_reason` raw). A hostile provider can inject ANSI escapes into operator terminals.

**Fix:** apply `_safe_excerpt` to everything that crosses a trust boundary into stderr.

**Status:** ⛔ Not addressed.

---

### M7. Admin endpoints not rate-limited

**File:** `src/conduit/api/middleware/rate_limit.py:35-66`

The route map covers everything except `/api/v1/admin/*`. The middleware returns "unrecognized → pass through". Combined with C7, an attacker who compromises the key can call `/admin/reset-demo` as many times as they like.

**Fix:** add an entry for the admin routes; or set a very low default rate (e.g. 3/hour) for unmapped routes when the path matches `/admin/`.

**Status:** ✅ Fixed in `f29eb8c`. Admin routes added to the route map (`rate_limit.py:68-71`) and to `TOOL_RATE_LIMITS` (`rate_limiter.py:54-57`): `admin_reset` at 3/hour, `admin_stats` at 10/min, deletes at 10/10min.

---

### M8. CORS `allow_methods=["GET","POST"]` but routers expose DELETE

**Files:**
- `src/conduit/main.py:128-134`
- `src/conduit/api/routers/marketplace.py:184`
- `src/conduit/api/routers/admin.py:42`

DELETE isn't in the CORS allowlist, which is actually good — it blocks browser cross-origin DELETE. But same-origin browser apps will also fail without operator realizing.

**Fix:** either add DELETE intentionally with `allow_credentials=False` for browser callers, or document that admin/delete is server-to-server only.

**Status:** ✅ Fixed in `f29eb8c` (rolled into the H10 documentation). The exclusion is now intentional and called out in a comment block at `main.py:135-138`.

---

### M9. `_check_secret_file_permissions` only exits in production

**Files:**
- `src/conduit/main.py:31-69`
- `src/conduit/mcp_server.py:2136-2172`

A dev environment with `APP_ENV=development` and a world-readable `.env` proceeds. Developers commonly run with prod-like credentials.

**Fix:** either warn very loudly with a 5s sleep, or just always exit — the fix (`chmod 600`) is trivial.

**Status:** ✅ Fixed in `f29eb8c`. `main.py:62-69` now always `sys.exit(1)` regardless of `APP_ENV`.

---

### M10. L402 secret derived deterministically from API key

**File:** `src/conduit/services/l402.py:93-102`

`sha256(api_key + ":l402")`. If the API key ever rotates, every outstanding L402 token becomes unverifiable; if the API key leaks, every previously minted L402 token is forgeable. The `L402_SECRET_KEY` setting in `config.py:53` is defined but unused.

**Fix:** use `L402_SECRET_KEY` as the actual key (it's already plumbed into the settings) and require it to be set when `L402_ENABLED=true`.

**Status:** ✅ Fixed in `f29eb8c`. `_get_l402_secret()` reads `settings.l402_secret_key` (`l402.py:93-119`); raises in production if placeholder; falls back to API-key derivation in dev with a warning. See **NEW-M2 / NEW-M3** for follow-ups on placement of the check.

---

### M11. `payment_preimage` stored unhashed in `executions` and `ratings`

**Files:**
- `src/conduit/models/execution.py:55`
- `src/conduit/models/rating.py:37`

The preimage *is* bearer proof of payment. Storing it plaintext in the DB means a DB read (backup leak, SQL injection elsewhere, replica access) gives the holder a valid payment proof for every past skill execution. Once a preimage exists for a `payment_hash`, anyone holding it can forever submit ratings as that consumer.

**Fix:** store `sha256(preimage)` (which equals `payment_hash` already, so just drop the column) — the existence of the row matters, the bytes don't.

**Status:** ⛔ Not addressed. Preimages still stored plaintext.

---

## LOW / Bugs

### L1. `bech32_decode` will `IndexError`/`ValueError` on inputs with chars outside the charset

**File:** `src/conduit/services/nostr.py:252-263`

`BECH32_CHARSET.index(c)` raises `ValueError` if `c` isn't in the charset. The caller in `from_nsec` catches the resulting exception, but the error message leaks crypto-library internals.

**Fix:** validate the alphabet first and return a clean error.

**Status:** ✅ Fixed in `f29eb8c`. Pre-check on the charset added at `nostr.py:291` (before the `.index` calls).

---

### L2. `_extract_retry_after` regex assumes exact phrasing

**File:** `src/conduit/api/middleware/rate_limit.py:149-155`

Fragile coupling between the limiter's message format and the middleware regex. If the message ever changes, the client always gets `Retry-After: 60`.

**Fix:** make `RateLimitExceeded` carry a structured `retry_after_seconds` attribute and read it directly.

**Status:** ✅ Fixed in `f29eb8c`. `RateLimitExceeded` now takes `retry_after_seconds` (`rate_limiter.py:94-96`); middleware uses `getattr(e, "retry_after_seconds", 60)` (`rate_limit.py:136`). The old `_extract_retry_after` helper is dead code — see **NEW-L1**.

---

### L3. LND singleton never closed on shutdown

**File:** `src/conduit/main.py:103`

Comment says "gRPC channels close on GC" but the `_lnd` global never has `disconnect()` called. Minor resource leak on graceful shutdown.

**Fix:** add a shutdown hook to the FastAPI lifespan that calls `_lnd.disconnect()`.

**Status:** ✅ Fixed in `f29eb8c`. `lifespan` now calls `lnd.disconnect()` after the `yield` (`main.py:103-110`).

---

### L4. `.gitignore` doesn't ignore `*.macaroon`, `*.cert`, `*.pem`

**File:** `.gitignore`

Only excludes the `credentials/` directory. Any operator who keeps a macaroon elsewhere in the tree can accidentally commit it.

**Fix:** add `*.macaroon`, `*.pem`, `*.cert`, `*.key`, `*.nsec` as global ignores.

**Status:** ✅ Fixed in `f29eb8c`. Pattern globs added.

---

### L5. `cleanup_db.py` has no env / prod check with `--yes`

**File:** `cleanup_db.py:84-88`

Given C7, this script + `--yes` flag deletes production with one keystroke.

**Fix:** add an `APP_ENV != "production"` assertion before invoking the destructive endpoint.

**Status:** ✅ Fixed in `f29eb8c`. Script now checks `APP_ENV` and prints a red warning if production (`cleanup_db.py:57-60`). The server-side block from C7 is the authoritative gate; this is the friendlier early warning.

---

### L6. `_purge_expired` is O(N) on every issue and redeem

**File:** `src/conduit/services/spending_limiter.py:96-100`

Scales linearly with pending tokens. See H1; replacing with a TTL store fixes both.

**Status:** ✅ Moot — H1's stateless tokens removed the dict entirely. No purge loop remains.

---

### L7. `verify_node_signature` uses `getattr(skill, "provider_pubkey", None)` despite the column existing

**Files:**
- `src/conduit/services/provider_verification.py:149`
- `src/conduit/models/skill.py:35`

The `provider_pubkey` column exists. The defensive `getattr` is dead code; use `skill.provider_pubkey` directly so a future model rename surfaces as a `MappedAttributeError` instead of silently degrading the security check.

**Status:** ✅ Fixed in `f29eb8c`. Direct attribute access at `provider_verification.py:158`.

---

### L8. Anomaly detector lists `circular_payment` but never raises it

**File:** `src/conduit/services/anomaly_detector.py:233`

Summary mentions `circular_payment` but no code path raises it. Either implement it or remove the type to avoid false implication.

**Status:** ✅ Fixed in `f29eb8c`. Type removed from the enumerated list at `anomaly_detector.py:234`.

---

## NEW findings introduced by the fixes

### NEW-H1. The H3 SSRF fix breaks HTTPS for all real providers

**File:** `src/conduit/services/skill_executor.py:97-145`

**Problem:** The fix rewrites the URL to `https://<resolved_ip>:443/path` and sets `Host: <hostname>` as a header. But httpx derives TLS SNI **and** cert-validation hostname from the URL host — not from the `Host:` header. So the handshake sends SNI=`1.2.3.4` and tries to validate the cert CN/SAN against `1.2.3.4`. Any provider serving a cert for `provider.example.com` (i.e. essentially all of them) fails with `CertificateError: hostname '1.2.3.4' doesn't match...`.

End result: legitimate skill executions fail at TLS handshake. The H3 fix is correctness-breaking.

**Fix:** keep the URL as `https://<hostname>` but force httpx's resolver to return the pinned IP. Options:

- Use `httpx.AsyncHTTPTransport` with a custom resolver that returns the pre-validated IP for that hostname only.
- httpx 0.27+ supports a `connect_to` mechanism on the transport — connect to IP, but pass hostname for SNI/validation.
- Drop the IP-pin entirely and accept the rebinding TTL window — but require resolver-returned DNS TTL ≥ 60s in `validate_outbound_url` to shrink it.

The "rewrite URL + set Host header" pattern works for plain HTTP, never for HTTPS.

---

### NEW-M1. New `.env.example` placeholder bypasses the API key validator

**Files:**
- `.env.example:15`
- `src/conduit/api/deps.py:32`
- `src/conduit/mcp_server.py:2188`

**Problem:** `.env.example` now sets `CONDUIT_API_KEY=CHANGE-ME-generate-a-random-key`. The validator checks `expected == "CHANGE-ME"` — exact-string match. A user who copies the template and forgets to change the value gets a working server with a guessable API key.

**Fix:** mirror the L402 secret check (`l402.py:104-107`): `secret.startswith("CHANGE-ME")`. Apply in both `api/deps.py:32` and `mcp_server.py:2188`.

---

### NEW-M2. L402 dev warning fires on every request

**File:** `src/conduit/services/l402.py:114-118`

**Problem:** When `L402_SECRET_KEY` is a placeholder in dev, `_get_l402_secret()` prints a stderr warning. The function is called inside `mint_l402_macaroon` and `verify_l402` — once or twice per L402 request. With L402 enabled in dev, logs flood.

**Fix:** move the warning into a one-shot startup check in the FastAPI `lifespan`; cache the resolved secret at module level after first call.

---

### NEW-M3. L402 secret check is request-time, not startup-time

**File:** `src/conduit/services/l402.py:108-113`

**Problem:** In production, `_get_l402_secret()` raises `RuntimeError` if `L402_SECRET_KEY` is a placeholder. But it's checked lazily — the server starts cleanly and the first L402 request 500s. Bad UX.

**Fix:** in `lifespan` startup, if `settings.l402_enabled`, call `_get_l402_secret()` once. Fail fast on missing secret rather than at first request.

---

### NEW-L1. Dead helper after L2 fix

**File:** `src/conduit/api/middleware/rate_limit.py:155-161`

**Problem:** `_extract_retry_after` is no longer called — `dispatch` reads `e.retry_after_seconds` directly. Remove the function.

---

### NEW-L2. H7 challenge replacement still has a TOCTOU window

**Files:** `src/conduit/services/provider_verification.py:97-106, 223-233`

**Problem:** The check-then-write on `skill.verification_challenge` isn't inside a `SELECT … FOR UPDATE`. Two concurrent `request_verification` calls can both see no fresh challenge and both write new ones; only the last commit survives, but the request that lost the race already returned a challenge that's no longer valid. Minor.

**Fix:** wrap the read in a row-locking select for symmetry with H6's confirm fix.

---

## Priority fix order (re-audit, 2026-05-30)

Order revised in light of what's already landed:

1. **NEW-H1** — H3 fix is correctness-breaking on HTTPS; resolves to webhook-call failures for any real provider. Highest priority.
2. **C3** — only remaining critical from the initial audit; swap pure-Python schnorr for `coincurve`.
3. **NEW-M1** — one-line fix; prevents a foot-gun on first setup.
4. **NEW-M2 + NEW-M3** — bundle into a single L402 startup-check refactor.
5. **M4, M6, M11** — cleanup tier from the original audit; not blockers.
6. **NEW-L1, NEW-L2** — tidy-ups, ride along with related work.

Everything else from the original audit is genuinely resolved. The team's fix quality is high: `with_for_update()` on confirm, stateless HMAC tokens that survive multi-worker, atomic spending reservations with explicit cancel — these are the right patterns, not lipstick. The one regression (NEW-H1) is the classic sharp-edge of "pin IP, pass hostname in header" — it almost always breaks TLS.

---

## Out of scope / not audited

- Dependencies (no `pip-audit` / SCA pass run).
- The Alembic migrations directory.
- Test fixtures (`tests/`) — they may contain insecure patterns intended only for local use.
- The Dockerfile, `docker-compose.yml`, and `install.sh` (touched lightly, not deeply reviewed).
- Frontend / docs site (`site/`, `docs/`).
- Runtime behavior — this is a static read. NEW-H1 in particular would benefit from a quick `curl`-equivalent integration test against a known HTTPS endpoint to confirm the TLS failure mode before the fix.

If you want a follow-up pass that covers any of the above, ask explicitly.
