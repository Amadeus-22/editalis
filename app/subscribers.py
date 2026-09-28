"""Subscriber lifecycle operations shared by the API and webhooks."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.domain import DeliveryStatus, Plan
from app.models import Delivery, Subscriber
from app.onboarding import ProfileUpdate

OPT_OUT_KEYWORDS = frozenset({"SAIR", "PARAR", "STOP", "CANCELAR"})


def is_opt_out(text: str) -> bool:
    return text.strip().upper().rstrip(".!") in OPT_OUT_KEYWORDS


def opt_out(session: Session, phone: str, now: datetime) -> bool:
    """Deactivate a subscriber and cancel anything still queued for them.

    Returns False if the phone is unknown. Idempotent.
    """
    subscriber = session.scalar(select(Subscriber).where(Subscriber.phone == phone))
    if subscriber is None:
        return False
    if subscriber.active:
        subscriber.active = False
        subscriber.opted_out_at = now
    session.execute(
        update(Delivery)
        .where(
            Delivery.subscriber_id == subscriber.id,
            Delivery.status == DeliveryStatus.PENDING,
        )
        .values(status=DeliveryStatus.SKIPPED, error="subscriber opted out")
    )
    return True


def apply_profile_update(
    session: Session, phone: str, update: ProfileUpdate, now: datetime
) -> Subscriber:
    """Create or update a subscriber from a WhatsApp profile message.

    Only the fields present in the message are changed. Messaging us again is
    an explicit opt-in, so an opted-out subscriber is reactivated.
    """
    subscriber = session.scalar(select(Subscriber).where(Subscriber.phone == phone))
    if subscriber is None:
        subscriber = Subscriber(phone=phone, ufs=[], areas=[], plan=Plan.FREE, created_at=now)
        session.add(subscriber)
    if update.ufs:
        subscriber.ufs = list(update.ufs)
    if update.areas:
        subscriber.areas = [area.value for area in update.areas]
    if update.education:
        subscriber.education = update.education.value
    if update.min_salary_cents:
        subscriber.min_salary_cents = update.min_salary_cents
    if not subscriber.active:
        subscriber.active = True
        subscriber.opted_out_at = None
    return subscriber
