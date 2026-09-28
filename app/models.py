"""SQLAlchemy ORM models.

The two unique constraints below are what make the pipeline idempotent.
Never drop them:
  - RawItem:  (source, external_id)
  - Delivery: (subscriber_id, exam_id)
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    TypeDecorator,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from app.domain import (
    Area,
    Classification,
    DeliveryStatus,
    EducationLevel,
    NoticeKind,
    Plan,
    SourceItem,
    SubscriberProfile,
)


def utcnow() -> datetime:
    return datetime.now(UTC)


class UTCDateTime(TypeDecorator[datetime]):
    """Stores naive UTC, returns aware UTC. Consistent across SQLite and Postgres."""

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Any) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("naive datetimes are not allowed; use UTC-aware values")
        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect: Any) -> datetime | None:
        return value.replace(tzinfo=UTC) if value is not None else None


class Base(DeclarativeBase):
    pass


class Subscriber(Base):
    __tablename__ = "subscribers"

    id: Mapped[int] = mapped_column(primary_key=True)
    phone: Mapped[str] = mapped_column(String(20), unique=True)
    name: Mapped[str | None] = mapped_column(String(120))
    ufs: Mapped[list[str]] = mapped_column(JSON, default=list)
    areas: Mapped[list[str]] = mapped_column(JSON, default=list)
    education: Mapped[str | None] = mapped_column(String(20))
    min_salary_cents: Mapped[int | None] = mapped_column(Integer)
    plan: Mapped[str] = mapped_column(String(10), default=Plan.FREE)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    opted_out_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)

    deliveries: Mapped[list[Delivery]] = relationship(back_populates="subscriber")

    def profile(self) -> SubscriberProfile:
        return SubscriberProfile(
            ufs=frozenset(self.ufs or ()),
            areas=frozenset(Area(a) for a in self.areas or ()),
            education=EducationLevel(self.education) if self.education else None,
            min_salary_cents=self.min_salary_cents,
        )


class RawItem(Base):
    __tablename__ = "raw_items"
    __table_args__ = (
        UniqueConstraint("source", "external_id", name="uq_raw_items_source_external_id"),
        Index("ix_raw_items_classified_at", "classified_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(50))
    external_id: Mapped[str] = mapped_column(String(255))
    title: Mapped[str] = mapped_column(String(500))
    url: Mapped[str] = mapped_column(String(1000))
    content: Mapped[str] = mapped_column(Text)
    uf_hint: Mapped[str | None] = mapped_column(String(2))
    organization_hint: Mapped[str | None] = mapped_column(String(255))
    published_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    fetched_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    classified_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    def to_source_item(self) -> SourceItem:
        return SourceItem(
            source=self.source,
            external_id=self.external_id,
            title=self.title,
            url=self.url,
            content=self.content,
            published_at=self.published_at,
            uf_hint=self.uf_hint,
            organization_hint=self.organization_hint,
        )


class Exam(Base):
    """A classified notice about a public exam, derived from exactly one RawItem."""

    __tablename__ = "exams"
    __table_args__ = (Index("ix_exams_matched_at", "matched_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    raw_item_id: Mapped[int] = mapped_column(ForeignKey("raw_items.id"), unique=True)
    kind: Mapped[str] = mapped_column(String(20))
    organization: Mapped[str | None] = mapped_column(String(255))
    ufs: Mapped[list[str]] = mapped_column(JSON, default=list)
    areas: Mapped[list[str]] = mapped_column(JSON, default=list)
    education_levels: Mapped[list[str]] = mapped_column(JSON, default=list)
    vacancies: Mapped[int | None] = mapped_column(Integer)
    max_salary_cents: Mapped[int | None] = mapped_column(Integer)
    registration_deadline: Mapped[date | None] = mapped_column(Date)
    url: Mapped[str] = mapped_column(String(1000))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    matched_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    deliveries: Mapped[list[Delivery]] = relationship(back_populates="exam")

    @staticmethod
    def row_from(raw_item: RawItem, result: Classification, now: datetime) -> dict[str, Any]:
        return {
            "raw_item_id": raw_item.id,
            "kind": result.kind.value,
            "organization": result.organization,
            "ufs": list(result.ufs),
            "areas": [a.value for a in result.areas],
            "education_levels": [e.value for e in result.education_levels],
            "vacancies": result.vacancies,
            "max_salary_cents": result.max_salary_cents,
            "registration_deadline": result.registration_deadline,
            "url": raw_item.url,
            "created_at": now,
        }

    def to_classification(self) -> Classification:
        return Classification(
            is_public_exam=True,
            kind=NoticeKind(self.kind),
            organization=self.organization,
            ufs=tuple(self.ufs or ()),
            areas=tuple(Area(a) for a in self.areas or ()),
            education_levels=tuple(EducationLevel(e) for e in self.education_levels or ()),
            vacancies=self.vacancies,
            max_salary_cents=self.max_salary_cents,
            registration_deadline=self.registration_deadline,
        )


class ProcessedWebhookEvent(Base):
    """Inbound webhook events already applied. Makes webhook handling idempotent."""

    __tablename__ = "processed_webhook_events"
    __table_args__ = (
        UniqueConstraint("provider", "event_id", name="uq_processed_webhook_events_provider_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    provider: Mapped[str] = mapped_column(String(20))
    event_id: Mapped[str] = mapped_column(String(100))
    event_type: Mapped[str] = mapped_column(String(60))
    received_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class Delivery(Base):
    """One alert for one subscriber. Written before any send is attempted."""

    __tablename__ = "deliveries"
    __table_args__ = (
        UniqueConstraint("subscriber_id", "exam_id", name="uq_deliveries_subscriber_exam"),
        Index("ix_deliveries_status", "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    subscriber_id: Mapped[int] = mapped_column(ForeignKey("subscribers.id"))
    exam_id: Mapped[int] = mapped_column(ForeignKey("exams.id"))
    status: Mapped[str] = mapped_column(String(10), default=DeliveryStatus.PENDING)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    provider_message_id: Mapped[str | None] = mapped_column(String(255))
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    sent_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    subscriber: Mapped[Subscriber] = relationship(back_populates="deliveries")
    exam: Mapped[Exam] = relationship(back_populates="deliveries")
