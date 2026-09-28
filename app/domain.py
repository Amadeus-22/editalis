"""Domain types shared by sources, classifier, matcher and message formatting.

Nothing in this module touches the database or the network.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum

UFS: tuple[str, ...] = (
    "AC", "AL", "AM", "AP", "BA", "CE", "DF", "ES", "GO", "MA", "MG", "MS", "MT", "PA",
    "PB", "PE", "PI", "PR", "RJ", "RN", "RO", "RR", "RS", "SC", "SE", "SP", "TO",
)  # fmt: skip


class NoticeKind(StrEnum):
    OPENING = "opening"
    RECTIFICATION = "rectification"
    SUMMONS = "summons"
    RESULT = "result"
    OTHER = "other"


class Area(StrEnum):
    ADMINISTRATIVE = "administrative"
    BANKING = "banking"
    EDUCATION = "education"
    ENGINEERING = "engineering"
    HEALTH = "health"
    IT = "it"
    LEGAL = "legal"
    SECURITY = "security"
    TAX = "tax"
    OTHER = "other"


class EducationLevel(StrEnum):
    ELEMENTARY = "elementary"
    HIGH_SCHOOL = "high_school"
    TECHNICAL = "technical"
    HIGHER = "higher"


EDUCATION_RANK: dict[EducationLevel, int] = {
    EducationLevel.ELEMENTARY: 0,
    EducationLevel.HIGH_SCHOOL: 1,
    EducationLevel.TECHNICAL: 2,
    EducationLevel.HIGHER: 3,
}


class Plan(StrEnum):
    FREE = "free"
    PRO = "pro"


class DeliveryStatus(StrEnum):
    PENDING = "pending"
    SENDING = "sending"
    SENT = "sent"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass(frozen=True, slots=True)
class SourceItem:
    """A raw document fetched from a data source, before classification."""

    source: str
    external_id: str
    title: str
    url: str
    content: str
    published_at: datetime | None = None
    uf_hint: str | None = None
    organization_hint: str | None = None


@dataclass(frozen=True, slots=True)
class Classification:
    """Structured facts about a notice. Always produced by `classifier._sanitize`."""

    is_public_exam: bool
    kind: NoticeKind = NoticeKind.OTHER
    organization: str | None = None
    ufs: tuple[str, ...] = ()
    areas: tuple[Area, ...] = ()
    education_levels: tuple[EducationLevel, ...] = ()
    vacancies: int | None = None
    max_salary_cents: int | None = None
    registration_deadline: date | None = None


@dataclass(frozen=True, slots=True)
class SubscriberProfile:
    """Matching preferences. Empty collections mean "no restriction"."""

    ufs: frozenset[str] = field(default_factory=frozenset)
    areas: frozenset[Area] = field(default_factory=frozenset)
    education: EducationLevel | None = None
    min_salary_cents: int | None = None
