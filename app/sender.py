"""WhatsApp delivery providers. Only `pipeline.dispatch()` may call `send()`."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol

import httpx

from app.config import Settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SendResult:
    provider_message_id: str | None = None


class SendError(Exception):
    """Raised when a provider rejects or fails to accept a message."""


class Sender(Protocol):
    def send(self, to: str, text: str) -> SendResult: ...


class ConsoleSender:
    """DRY_RUN=1: log the message instead of sending it."""

    def send(self, to: str, text: str) -> SendResult:
        logger.info("[dry-run] to=%s\n%s", to, text)
        return SendResult(provider_message_id=None)


class MetaCloudSender:
    """Meta WhatsApp Cloud API. Business-initiated messages must use an approved template.

    The template is expected to have a single body variable ({{1}}). Meta rejects
    template parameters containing line breaks, so they are flattened.
    """

    def __init__(self, http: httpx.Client, settings: Settings) -> None:
        self._http = http
        self._url = (
            f"https://graph.facebook.com/{settings.wa_graph_version}"
            f"/{settings.wa_phone_number_id}/messages"
        )
        self._token = settings.wa_access_token
        self._template = settings.wa_template_name
        self._lang = settings.wa_template_lang

    def send(self, to: str, text: str) -> SendResult:
        payload = {
            "messaging_product": "whatsapp",
            "to": to,
            "type": "template",
            "template": {
                "name": self._template,
                "language": {"code": self._lang},
                "components": [
                    {"type": "body", "parameters": [{"type": "text", "text": flatten(text)}]}
                ],
            },
        }
        response = _post(self._http, self._url, payload, {"Authorization": f"Bearer {self._token}"})
        messages = response.get("messages") or [{}]
        return SendResult(provider_message_id=messages[0].get("id"))


class EvolutionSender:
    """Self-hosted Evolution API (v2 `sendText` endpoint)."""

    def __init__(self, http: httpx.Client, settings: Settings) -> None:
        self._http = http
        self._url = (
            f"{settings.evolution_base_url.rstrip('/')}/message/sendText/"
            f"{settings.evolution_instance}"
        )
        self._api_key = settings.evolution_api_key

    def send(self, to: str, text: str) -> SendResult:
        response = _post(
            self._http, self._url, {"number": to, "text": text}, {"apikey": self._api_key}
        )
        key = response.get("key") or {}
        return SendResult(provider_message_id=key.get("id"))


def flatten(text: str) -> str:
    """Template-safe text: no newlines/tabs and no runs of 4+ spaces."""
    parts = [" ".join(line.split()) for line in text.splitlines()]
    return " · ".join(part for part in parts if part)


def _post(http: httpx.Client, url: str, payload: dict, headers: dict[str, str]) -> dict:
    try:
        response = http.post(url, json=payload, headers=headers, timeout=15)
    except httpx.HTTPError as exc:
        raise SendError(f"transport error: {exc}") from exc
    if response.is_error:
        raise SendError(f"HTTP {response.status_code}: {response.text[:300]}")
    try:
        return response.json()
    except ValueError:
        return {}


def build_sender(settings: Settings, http: httpx.Client) -> Sender:
    if settings.dry_run:
        return ConsoleSender()
    if settings.wa_provider == "evolution":
        return EvolutionSender(http, settings)
    return MetaCloudSender(http, settings)
