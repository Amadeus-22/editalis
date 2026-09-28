"""Asaas billing events → subscriber plan.

Subscriptions are created in Asaas with `externalReference` set to the
subscriber id. Each webhook event is applied at most once, keyed by its id.
"""

from __future__ import annotations

import logging
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy.orm import Session

from app.db import insert_ignore
from app.domain import Plan
from app.models import ProcessedWebhookEvent, Subscriber

logger = logging.getLogger(__name__)

PROVIDER = "asaas"

UPGRADE_EVENTS = frozenset({"PAYMENT_CONFIRMED", "PAYMENT_RECEIVED"})
DOWNGRADE_EVENTS = frozenset(
    {
        "PAYMENT_OVERDUE",
        "PAYMENT_REFUNDED",
        "PAYMENT_CHARGEBACK_REQUESTED",
        "SUBSCRIPTION_DELETED",
        "SUBSCRIPTION_INACTIVATED",
    }
)


class Outcome(StrEnum):
    APPLIED = "applied"
    DUPLICATE = "duplicate"
    IGNORED = "ignored"
    UNKNOWN_SUBSCRIBER = "unknown_subscriber"


def plan_for_event(event: str) -> Plan | None:
    """The plan an event implies, or None if the event does not affect the plan."""
    if event in UPGRADE_EVENTS:
        return Plan.PRO
    if event in DOWNGRADE_EVENTS:
        return Plan.FREE
    return None


def subscriber_reference(payload: dict[str, Any]) -> int | None:
    """Read the subscriber id from the payment or subscription `externalReference`."""
    for key in ("payment", "subscription"):
        obj = payload.get(key)
        if isinstance(obj, dict):
            reference = obj.get("externalReference")
            if isinstance(reference, str) and reference.isdigit():
                return int(reference)
    return None


def apply_asaas_event(session: Session, payload: dict[str, Any], now: datetime) -> Outcome:
    """Apply one Asaas webhook event. Safe to call again with the same event."""
    event_id, event = payload.get("id"), payload.get("event")
    if not isinstance(event_id, str) or not isinstance(event, str):
        return Outcome.IGNORED

    claimed = insert_ignore(
        session,
        ProcessedWebhookEvent,
        [{"provider": PROVIDER, "event_id": event_id, "event_type": event, "received_at": now}],
        ("provider", "event_id"),
    )
    if not claimed:
        return Outcome.DUPLICATE

    plan = plan_for_event(event)
    if plan is None:
        return Outcome.IGNORED

    subscriber_id = subscriber_reference(payload)
    subscriber = session.get(Subscriber, subscriber_id) if subscriber_id else None
    if subscriber is None:
        logger.warning("Asaas event %s (%s) has no known subscriber", event_id, event)
        return Outcome.UNKNOWN_SUBSCRIBER

    if subscriber.plan != plan:
        logger.info(
            "Subscriber %d: plan %s -> %s (%s)", subscriber.id, subscriber.plan, plan, event
        )
        subscriber.plan = plan
    return Outcome.APPLIED
