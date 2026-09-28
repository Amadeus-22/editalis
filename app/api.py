"""HTTP API: health check, admin subscriber management and the WhatsApp webhook."""

from __future__ import annotations

import hashlib
import hmac
import logging
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response, status
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import __version__
from app.config import Settings, configure_logging, get_settings
from app.db import get_engine, init_db, make_session_factory
from app.domain import UFS, Area, EducationLevel, Plan
from app.models import Subscriber
from app.subscribers import is_opt_out, opt_out

logger = logging.getLogger(__name__)

PHONE_PATTERN = r"^55\d{10,11}$"  # E.164 without "+": 55 + area code + number


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    configure_logging(settings.log_level)
    init_db(get_engine(settings.database_url))
    yield


app = FastAPI(title="Public Exam Alerts", version=__version__, lifespan=lifespan)


# ---------------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------------


def get_session(settings: Annotated[Settings, Depends(get_settings)]) -> Iterator[Session]:
    factory = make_session_factory(get_engine(settings.database_url))
    with factory() as session:
        yield session


def require_admin(
    settings: Annotated[Settings, Depends(get_settings)],
    x_admin_token: Annotated[str | None, Header()] = None,
) -> None:
    if not settings.admin_token:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "ADMIN_TOKEN is not configured")
    if not x_admin_token or not hmac.compare_digest(x_admin_token, settings.admin_token):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid admin token")


SessionDep = Annotated[Session, Depends(get_session)]
SettingsDep = Annotated[Settings, Depends(get_settings)]
AdminDep = Depends(require_admin)


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class ProfileFields(BaseModel):
    ufs: list[str] | None = None
    areas: list[Area] | None = None
    education: EducationLevel | None = None
    min_salary_brl: Decimal | None = Field(default=None, ge=0, le=200_000)

    @field_validator("ufs")
    @classmethod
    def _valid_ufs(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        normalized = sorted({uf.strip().upper() for uf in value})
        invalid = [uf for uf in normalized if uf not in UFS]
        if invalid:
            raise ValueError(f"invalid UF(s): {', '.join(invalid)}")
        return normalized


class SubscriberCreate(ProfileFields):
    phone: str = Field(pattern=PHONE_PATTERN)
    name: str | None = Field(default=None, max_length=120)


class SubscriberUpdate(ProfileFields):
    name: str | None = Field(default=None, max_length=120)
    plan: Plan | None = None
    active: bool | None = None


class SubscriberOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    phone: str
    name: str | None
    ufs: list[str]
    areas: list[str]
    education: str | None
    min_salary_cents: int | None
    plan: str
    active: bool
    created_at: datetime


def _apply_profile(subscriber: Subscriber, data: dict[str, Any]) -> None:
    if "min_salary_brl" in data:
        value = data.pop("min_salary_brl")
        subscriber.min_salary_cents = int(value * 100) if value is not None else None
    if "areas" in data and data["areas"] is not None:
        data["areas"] = [area.value for area in data["areas"]]
    for key, value in data.items():
        setattr(subscriber, key, value)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.get("/health")
def health(session: SessionDep) -> dict[str, str]:
    session.execute(text("SELECT 1"))
    return {"status": "ok", "version": __version__}


@app.post(
    "/subscribers",
    status_code=status.HTTP_201_CREATED,
    response_model=SubscriberOut,
    dependencies=[AdminDep],
)
def create_subscriber(payload: SubscriberCreate, session: SessionDep) -> Subscriber:
    subscriber = Subscriber(phone=payload.phone, ufs=[], areas=[])
    _apply_profile(subscriber, payload.model_dump(exclude_none=True, exclude={"phone"}))
    session.add(subscriber)
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "phone already registered") from exc
    return subscriber


@app.get("/subscribers/{subscriber_id}", response_model=SubscriberOut, dependencies=[AdminDep])
def get_subscriber(subscriber_id: int, session: SessionDep) -> Subscriber:
    subscriber = session.get(Subscriber, subscriber_id)
    if subscriber is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "subscriber not found")
    return subscriber


@app.patch("/subscribers/{subscriber_id}", response_model=SubscriberOut, dependencies=[AdminDep])
def update_subscriber(
    subscriber_id: int, payload: SubscriberUpdate, session: SessionDep
) -> Subscriber:
    subscriber = get_subscriber(subscriber_id, session)
    _apply_profile(subscriber, payload.model_dump(exclude_unset=True))
    session.commit()
    return subscriber


@app.get("/webhooks/whatsapp", response_class=PlainTextResponse)
def verify_whatsapp_webhook(
    settings: SettingsDep,
    mode: Annotated[str, Query(alias="hub.mode")],
    token: Annotated[str, Query(alias="hub.verify_token")],
    challenge: Annotated[str, Query(alias="hub.challenge")],
) -> str:
    """Meta's one-time subscription handshake."""
    expected = settings.wa_verify_token
    if mode == "subscribe" and expected and hmac.compare_digest(token, expected):
        return challenge
    raise HTTPException(status.HTTP_403_FORBIDDEN, "verification failed")


@app.post("/webhooks/whatsapp", status_code=status.HTTP_200_OK)
async def receive_whatsapp_webhook(
    request: Request, settings: SettingsDep, session: SessionDep
) -> Response:
    """Inbound messages from the Meta Cloud API. Handles opt-out (SAIR)."""
    body = await request.body()
    if settings.wa_app_secret and not _valid_signature(
        body, request.headers.get("x-hub-signature-256"), settings.wa_app_secret
    ):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid signature")

    try:
        payload = await request.json()
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid JSON") from exc

    now = datetime.now(UTC)
    for phone, message in _iter_text_messages(payload):
        if is_opt_out(message) and opt_out(session, phone, now):
            logger.info("Subscriber %s opted out", phone)
    session.commit()
    # Always 200 so Meta does not retry payloads we chose to ignore.
    return Response(status_code=status.HTTP_200_OK)


def _valid_signature(body: bytes, header: str | None, secret: str) -> bool:
    if not header or not header.startswith("sha256="):
        return False
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(header.removeprefix("sha256="), digest)


def _iter_text_messages(payload: Any) -> Iterator[tuple[str, str]]:
    if not isinstance(payload, dict):
        return
    for entry in payload.get("entry") or []:
        for change in entry.get("changes") or []:
            for message in (change.get("value") or {}).get("messages") or []:
                if message.get("type") != "text":
                    continue
                phone = message.get("from")
                body = (message.get("text") or {}).get("body")
                if isinstance(phone, str) and isinstance(body, str):
                    yield phone, body
