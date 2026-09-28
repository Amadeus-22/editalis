"""The alert pipeline: collect -> classify -> match -> dispatch.

Every stage is idempotent and can run any number of times:
  - collect:  RawItem is unique per (source, external_id).
  - classify: only touches RawItems with classified_at IS NULL; Concurso is
              unique per raw_item_id.
  - match:    only touches Concursos with matched_at IS NULL; Delivery is
              unique per (subscriber_id, concurso_id).
  - dispatch: only sends PENDING deliveries, and marks them SENDING before the
              provider call, so a crash can never cause a duplicate message.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import or_, select
from sqlalchemy.orm import Session, joinedload, sessionmaker

from app.classifier import Classifier
from app.config import Settings
from app.db import insert_ignore, session_scope
from app.domain import DeliveryStatus, Plan, SourceItem
from app.matcher import matches
from app.messages import format_alert, pick_affiliate_link
from app.models import Concurso, Delivery, RawItem, Subscriber
from app.sender import Sender, SendError
from app.sources import Source
from app.sources.querido_diario import QueridoDiarioSource
from app.sources.rss import RssSource

logger = logging.getLogger(__name__)


def build_sources(settings: Settings, http: httpx.Client) -> list[Source]:
    sources: list[Source] = [
        QueridoDiarioSource(
            http,
            base_url=settings.qd_base_url,
            query=settings.qd_query,
            lookback_days=settings.qd_lookback_days,
            page_size=settings.qd_page_size,
            max_pages=settings.qd_max_pages,
        )
    ]
    sources.extend(RssSource(http, url) for url in settings.rss_feeds)
    return sources


# ---------------------------------------------------------------------------
# Stages
# ---------------------------------------------------------------------------


def collect(session: Session, sources: Sequence[Source], now: datetime) -> int:
    """Fetch every source and store new items. A failing source never aborts the run."""
    inserted = 0
    for source in sources:
        try:
            items = list(source.fetch(now))
        except httpx.HTTPError as exc:
            logger.warning("Source %s unavailable (%s); skipping it this cycle", source.name, exc)
            continue
        except Exception:
            logger.exception("Source %s failed; skipping it this cycle", source.name)
            continue
        count = insert_ignore(
            session, RawItem, _raw_item_rows(items, now), ("source", "external_id")
        )
        session.commit()
        logger.info("Source %s: %d fetched, %d new", source.name, len(items), count)
        inserted += count
    return inserted


def classify(session: Session, classifier: Classifier, now: datetime, limit: int = 200) -> int:
    """Classify unprocessed items. Returns how many became Concursos."""
    items = session.scalars(
        select(RawItem).where(RawItem.classified_at.is_(None)).order_by(RawItem.id).limit(limit)
    ).all()
    created = 0
    for item in items:
        try:
            result = classifier.classify(item.to_source_item())
        except Exception:
            logger.exception("Classifier crashed on raw_item %d; will retry next cycle", item.id)
            continue
        if result is not None and result.is_public_exam:
            created += insert_ignore(
                session, Concurso, [Concurso.row_from(item, result, now)], ("raw_item_id",)
            )
        item.classified_at = now
        # Commit per item so paid LLM work is never lost to a later failure.
        session.commit()
    return created


def match(session: Session, now: datetime) -> int:
    """Create Delivery rows for every (active subscriber, new Concurso) that matches."""
    concursos = session.scalars(
        select(Concurso).where(Concurso.matched_at.is_(None)).order_by(Concurso.id)
    ).all()
    if not concursos:
        return 0

    subscribers = session.scalars(select(Subscriber).where(Subscriber.active.is_(True))).all()
    profiles = [(s.id, s.profile()) for s in subscribers]
    created = 0
    for concurso in concursos:
        notice = concurso.to_classification()
        rows = [
            {
                "subscriber_id": subscriber_id,
                "concurso_id": concurso.id,
                "status": DeliveryStatus.PENDING.value,
                "created_at": now,
            }
            for subscriber_id, profile in profiles
            if matches(profile, notice)
        ]
        created += insert_ignore(session, Delivery, rows, ("subscriber_id", "concurso_id"))
        concurso.matched_at = now
        session.commit()
    return created


@dataclass
class DispatchReport:
    sent: int = 0
    failed: int = 0
    skipped: int = 0


def dispatch(
    session: Session,
    sender: Sender,
    now: datetime,
    *,
    free_delay: timedelta,
    affiliate_links: dict[str, str],
    limit: int = 200,
) -> DispatchReport:
    """Send due deliveries. Pro is immediate; Free waits `free_delay` after matching.

    The delay is evaluated here rather than stored, so upgrading to Pro releases
    pending alerts on the next cycle.
    """
    report = DispatchReport()
    due = session.scalars(
        select(Delivery)
        .join(Delivery.subscriber)
        .options(joinedload(Delivery.subscriber), joinedload(Delivery.concurso))
        .where(
            Delivery.status == DeliveryStatus.PENDING,
            Subscriber.active.is_(True),
            or_(Subscriber.plan == Plan.PRO, Delivery.created_at <= now - free_delay),
        )
        .order_by(Delivery.id)
        .limit(limit)
    ).all()

    for delivery in due:
        concurso = delivery.concurso
        if concurso.registration_deadline and concurso.registration_deadline < now.date():
            delivery.status = DeliveryStatus.SKIPPED
            delivery.error = "registration closed before dispatch"
            session.commit()
            report.skipped += 1
            continue

        notice = concurso.to_classification()
        affiliate_link = pick_affiliate_link(notice.areas, affiliate_links)
        text = format_alert(notice, concurso.url, affiliate_link)

        # Claim the delivery before calling the provider (at-most-once delivery).
        delivery.status = DeliveryStatus.SENDING
        delivery.attempts += 1
        session.commit()

        try:
            result = sender.send(delivery.subscriber.phone, text)
        except SendError as exc:
            logger.warning("Delivery %d failed: %s", delivery.id, exc)
            delivery.status = DeliveryStatus.FAILED
            delivery.error = str(exc)[:1000]
            report.failed += 1
        else:
            delivery.status = DeliveryStatus.SENT
            delivery.sent_at = now
            delivery.provider_message_id = result.provider_message_id
            report.sent += 1
        session.commit()
    return report


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


@dataclass
class CycleReport:
    started_at: datetime
    collected: int = 0
    classified: int = 0
    matched: int = 0
    dispatch: DispatchReport = field(default_factory=DispatchReport)

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def run_cycle(
    session_factory: sessionmaker[Session],
    sources: Sequence[Source],
    classifier: Classifier,
    sender: Sender,
    settings: Settings,
    now: datetime | None = None,
) -> CycleReport:
    now = now or datetime.now(UTC)
    report = CycleReport(started_at=now)
    with session_scope(session_factory) as session:
        report.collected = collect(session, sources, now)
    with session_scope(session_factory) as session:
        report.classified = classify(session, classifier, now, settings.classify_batch_size)
    with session_scope(session_factory) as session:
        report.matched = match(session, now)
    with session_scope(session_factory) as session:
        report.dispatch = dispatch(
            session,
            sender,
            now,
            free_delay=timedelta(hours=settings.free_delay_hours),
            affiliate_links=settings.affiliate_links,
            limit=settings.dispatch_batch_size,
        )
    logger.info("Cycle finished: %s", report.as_dict())
    return report


def _raw_item_rows(items: Sequence[SourceItem], now: datetime) -> list[dict[str, object]]:
    rows: dict[tuple[str, str], dict[str, object]] = {}
    for item in items:
        rows.setdefault(
            (item.source, item.external_id),
            {
                "source": item.source,
                "external_id": item.external_id,
                "title": item.title[:500],
                "url": item.url[:1000],
                "content": item.content,
                "uf_hint": item.uf_hint,
                "organization_hint": item.organization_hint,
                "published_at": item.published_at,
                "fetched_at": now,
            },
        )
    return list(rows.values())
