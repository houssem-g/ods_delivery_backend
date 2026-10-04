"""Blocking and reporting between users (App Review 1.2, user-generated content: the chat).

A block works both ways: neither user sees the other's messages, orders or offers any more,
and they are never put together on a new order. A report goes to the admins.
"""

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Text, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, created_at, uuid_pk

REPORT_REASONS = ("harassment", "inappropriate", "spam", "fraud", "dangerous", "other")
REPORT_STATUSES = ("open", "resolved", "dismissed")


class UserBlock(Base):
    __tablename__ = "user_blocks"
    __table_args__ = (
        CheckConstraint("blocker_id <> blocked_id", name="not_self"),
        Index("user_blocks_pair", "blocker_id", "blocked_id", unique=True),
        Index("ix_user_blocks_blocked_id", "blocked_id"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    blocker_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    blocked_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    created_at: Mapped[datetime] = created_at()


class UserReport(Base):
    __tablename__ = "user_reports"
    __table_args__ = (
        CheckConstraint("reason IN (" + ",".join(f"'{r}'" for r in REPORT_REASONS) + ")", name="reason"),
        CheckConstraint("status IN (" + ",".join(f"'{s}'" for s in REPORT_STATUSES) + ")", name="status"),
        Index("user_reports_open", "created_at", postgresql_where=text("status = 'open'")),
        Index("ix_user_reports_reported_id", "reported_id"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    reporter_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    reported_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    order_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("orders.id", ondelete="SET NULL")
    )
    message_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("messages.id", ondelete="SET NULL")
    )
    # the reported message's text at report time (kept even if the message goes away)
    message_excerpt: Mapped[str | None] = mapped_column(Text)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    details: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="open")
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = created_at()
