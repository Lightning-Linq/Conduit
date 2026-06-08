"""Federated reputation cache — verified rating attestations from other nodes.

Federation #1, phase 4. Discovery reads aggregates from this table instead of
fetching from Nostr relays (and re-verifying Schnorr) on every query. Rows are
ALREADY-VERIFIED attestations (services/federation.verify_attestations); the raw
event is kept so a stricter future verifier can re-check, or we can re-broadcast.
"""

from sqlalchemy import BigInteger, CheckConstraint, Index, Integer, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from conduit.models.base import Base


class FederatedAttestation(Base):
    """A verified rating attestation cached from the federation transport."""

    __tablename__ = "federated_attestations"

    # Nostr event id: one row per distinct attestation event. Re-fetching the
    # same event is a no-op (idempotent upsert on this key).
    event_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)

    skill_id: Mapped[str] = mapped_column(String(255))
    provider_pubkey: Mapped[str] = mapped_column(String(64), index=True)
    rater_pubkey: Mapped[str] = mapped_column(String(64))
    payment_hash: Mapped[str] = mapped_column(String(64))
    score: Mapped[int] = mapped_column(Integer)

    # The attestation's own created_at (unix seconds) — distinct from this row's
    # created_at, which is when it was cached.
    attestation_created_at: Mapped[int] = mapped_column(BigInteger)

    # Full Nostr event, so a stricter future verifier can re-check from cache
    # (no relay round-trip) and so attestations can be re-broadcast.
    raw_event: Mapped[dict] = mapped_column(JSONB)

    __table_args__ = (
        CheckConstraint("score >= 1 AND score <= 5", name="valid_attestation_score"),
        # Aggregation reads by (skill, provider); index that access path.
        Index("ix_fed_att_skill_provider", "skill_id", "provider_pubkey"),
    )

    def __repr__(self) -> str:
        return (
            f"<FederatedAttestation skill={self.skill_id} "
            f"provider={self.provider_pubkey[:8]} score={self.score}>"
        )
