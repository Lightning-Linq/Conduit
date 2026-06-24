"""Custody + data-minimization tripwire — the Phase 1 headline security guard.

A STATIC guard that fails CI if a future change sneaks into the swap modules:
  * custody — a key/seed/mnemonic, or a swap create/fund/claim call, OR
  * a data leak to Satora — a payment_hash / BOLT11 / payer identity in the
    Satora-facing client.

It inspects CODE ONLY (comments + triple-quoted docstrings/prose are stripped), so the
modules can freely DOCUMENT the invariant ("never holds a mnemonic") without tripping the
guard, while real usage or a leaked request key is still caught. The matching BEHAVIORAL
guard (the live outbound /quote request carries only amount + token/chain) lives in
test_swap_provider.test_quote_data_minimization.
"""

import io
import pathlib
import tokenize

_SERVICES = pathlib.Path(__file__).resolve().parent.parent / "src" / "conduit" / "services"


def _code_only(name: str) -> str:
    """Module source with comments + triple-quoted strings removed, lowercased."""
    src = (_SERVICES / name).read_text()
    parts: list[str] = []
    for tok in tokenize.generate_tokens(io.StringIO(src).readline):
        if tok.type == tokenize.COMMENT:
            continue
        if tok.type == tokenize.STRING:
            body = tok.string.lstrip("rbfuRBFU")
            if body.startswith('"""') or body.startswith("'''"):
                continue  # drop docstrings / long prose
        parts.append(tok.string)
    return "".join(parts).lower()


# Neither swap module may hold a key/seed or execute/fund/claim a swap (CODE, not comments).
_NO_CUSTODY = (
    "mnemonic",
    "seed_phrase",
    "private_key",
    "privkey",
    "secret_key",
    "create_swap",
    "createswap",
    "claimviagasless",
    "fund_swap",
    "fundswap",
)

# swap_provider is the ONLY Satora-facing module: it may send nothing but amount +
# token/chain, and only GET (never POST a swap or leak identity/invoice).
_PROVIDER_NO_LEAK = (
    "payment_hash",
    "bolt11",
    "lnbc",
    "preimage",
    "payer_pubkey",
    "macaroon",
    ".post(",
    "/swap",
)


def test_swap_modules_hold_no_keys_and_execute_no_swap():
    for name in ("swap_provider.py", "stablecoin_quote.py"):
        code = _code_only(name)
        for token in _NO_CUSTODY:
            assert token not in code, f"{name} CODE has forbidden custody/exec token: {token!r}"


def test_satora_facing_client_leaks_no_identity_invoice_or_swap_call():
    code = _code_only("swap_provider.py")
    for token in _PROVIDER_NO_LEAK:
        assert token not in code, (
            f"swap_provider.py CODE must not reference {token!r} (Satora leak / swap call)"
        )
