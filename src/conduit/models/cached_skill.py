"""Federated skill-catalog cache — verified skill listings from other nodes.

Federation #2. The main marketplace discovery reads remote skills from this table
instead of hitting Nostr relays / peers on every query. Rows are ALREADY-VERIFIED
kind-38383 listings (the event signature is re-checked on ingest); the raw event is
kept so a stricter verifier can re-check, or we can re-broadcast. NIP-33 replaceable:
one row per (provider_pubkey, skill_id), newest event_created_at wins.

Trust: a peer/relay is untrusted infrastructure. Signatures are re-verified on
ingest, so a source cannot forge or inflate a listing — only serve junk (rejected on
verify) or withhold (mitigated by multiple sources). Provider verification badges are
NOT trusted from this cache; the federated reputation overlay (#1) is the cross-node
trust signal.
"""

from sqlalchemy import BigInteger, Index, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from conduit.models.base import Base


class CachedSkill(Base):
    """A verified remote skill listing cached from a catalog transport."""

    __tablename__ = "cached_skills"

    # NIP-33 replaceable coordinate: one row per (provider_pubkey, skill_id),
    # newest event_created_at wins (idempotent upsert on this key).
    provider_pubkey: Mapped[str] = mapped_column(String(64), index=True)  # Nostr signer (x-only)
    skill_id: Mapped[str] = mapped_column(String(255))  # event 'd' tag (skill UUID)

    # The latest kind-38383 event ingested for this coordinate.
    event_id: Mapped[str] = mapped_column(String(64))
    event_created_at: Mapped[int] = mapped_column(BigInteger)  # unix seconds; newest wins

    # Provenance — which transport surfaced it (untrusted; display/debug only).
    origin: Mapped[str] = mapped_column(String(16))  # "relay" | "peer"
    source_id: Mapped[str | None] = mapped_column(String(512), nullable=True)

    # Parsed listing fields for discovery queries (mirror Skill's discoverable cols).
    provider_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    provider_lightning_address: Mapped[str | None] = mapped_column(String(255), nullable=True)
    name: Mapped[str] = mapped_column(String(255), index=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    category: Mapped[str | None] = mapped_column(String(100), index=True, nullable=True)
    tags: Mapped[str | None] = mapped_column(Text, nullable=True)  # comma-separated
    price_sats: Mapped[int] = mapped_column(BigInteger, default=0)
    endpoint_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    input_schema: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    output_schema: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    # Full Nostr event, so a stricter future verifier can re-check from cache.
    raw_event: Mapped[dict] = mapped_column(JSONB)

    __table_args__ = (
        UniqueConstraint("provider_pubkey", "skill_id", name="uq_cached_skill_coord"),
        # Discovery filters by (provider, skill) coordinate and by category/name.
        Index("ix_cached_skills_provider_skill", "provider_pubkey", "skill_id"),
    )

    def __repr__(self) -> str:
        return f"<CachedSkill {self.name!r} provider={self.provider_pubkey[:8]} ({self.origin})>"
