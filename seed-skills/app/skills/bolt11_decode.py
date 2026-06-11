"""bolt11-decode — decode a BOLT11 Lightning invoice into its fields."""

from __future__ import annotations

import bolt11

from app.registry import Skill, SkillError, register


def _safe(invoice: bolt11.Bolt11, name: str):
    """Read an optional bolt11 property, tolerating an absent tag."""
    try:
        return getattr(invoice, name)
    except Exception:
        return None


def run(input_data: dict) -> dict:
    invoice = input_data.get("invoice")
    if not isinstance(invoice, str) or not invoice.strip():
        raise SkillError("`invoice` (bolt11 string) is required")
    try:
        decoded = bolt11.decode(invoice.strip())
    except Exception as exc:  # any bech32 / bolt11 parse failure -> 400, not 500
        raise SkillError(f"invalid bolt11 invoice: {exc}") from exc

    amount_msat = decoded.amount_msat
    return {
        "network": decoded.currency,
        "amount_msat": amount_msat,
        "amount_sat": amount_msat // 1000 if amount_msat else None,
        "description": _safe(decoded, "description"),
        "payment_hash": _safe(decoded, "payment_hash"),
        "payee": _safe(decoded, "payee"),
        "timestamp": decoded.date,
        "expiry_seconds": _safe(decoded, "expiry"),
        "min_final_cltv_expiry": _safe(decoded, "min_final_cltv_expiry"),
        "expired": decoded.has_expired(),
    }


register(
    Skill(
        name="bolt11-decode",
        description="Decode a BOLT11 Lightning invoice: amount, description, payment hash, expiry.",
        handler=run,
        input_example={"invoice": "lnbc25u1p..."},
    )
)
