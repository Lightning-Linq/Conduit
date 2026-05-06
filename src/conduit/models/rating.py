"""Rating model — reputation system backed by Lightning payment proofs."""

import uuid

from sqlalchemy import ForeignKey, Integer, String, Text, CheckConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from conduit.models.base import Base


class Rating(Base):
    """
    A rating submitted after a skill execution.

    The payment_preimage serves as cryptographic proof that the
    rater actually paid for and received the skill — no fake reviews
    without real transactions.
    """

    __tablename__ = "ratings"

    # Which execution is being rated
    execution_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("skill_executions.id"), index=True
    )

    # Who is rating
    rater_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    rater_pubkey: Mapped[str | None] = mapped_column(String(66), nullable=True)

    # Rating
    score: Mapped[int] = mapped_column(Integer)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Payment proof (ensures only real customers can rate)
    payment_preimage: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # Constraints
    __table_args__ = (
        CheckConstraint("score >= 1 AND score <= 5", name="valid_score_range"),
    )

    # Relationships
    execution: Mapped["SkillExecution"] = relationship(back_populates="ratings")

    def __repr__(self) -> str:
        return f"<Rating execution={self.execution_id} score={self.score}>"
