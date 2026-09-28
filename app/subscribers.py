"""Subscriber lifecycle operations shared by the API and webhooks."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.domain import DeliveryStatus
from app.models import Delivery, Subscriber

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
