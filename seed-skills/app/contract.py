"""The Conduit skill-execution webhook contract.

Conduit POSTs this body to a skill's registered ``endpoint_url`` after the
consumer's Lightning payment settles; the provider returns ``{"output": ...}``.
These models mirror ``conduit.services.skill_executor.execute_skill_webhook`` —
keep them in sync if the executor's payload changes.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class PaymentProof(BaseModel):
    """Bearer proof the invoice settled: SHA256(payment_preimage) == payment_hash."""

    payment_hash: str
    payment_preimage: str


class WebhookRequest(BaseModel):
    """The JSON body Conduit sends to a skill webhook."""

    execution_id: str
    skill_name: str
    input_data: dict[str, Any] = Field(default_factory=dict)
    # Optional so the server can run keyless in dev (REQUIRE_PAYMENT_PROOF=false);
    # Conduit always sends it in production.
    payment_proof: PaymentProof | None = None
    timestamp: str | None = None
