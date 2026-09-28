"""Application settings, loaded from environment variables and `.env`."""

from __future__ import annotations

import logging
from functools import lru_cache
from typing import Annotated, Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Core
    database_url: str = "sqlite:///./editalis.db"
    log_level: str = "INFO"
    admin_token: str = ""

    # Classification. Without an API key the heuristic classifier is used.
    anthropic_api_key: str = ""
    classifier_model: str = "claude-sonnet-4-6"
    classifier_max_chars: int = 20_000

    # WhatsApp
    dry_run: bool = True
    wa_provider: Literal["meta", "evolution"] = "meta"
    wa_graph_version: str = "v23.0"
    wa_phone_number_id: str = ""
    wa_access_token: str = ""
    wa_template_name: str = "editalis_alert"
    wa_template_lang: str = "pt_BR"
    wa_verify_token: str = ""
    wa_app_secret: str = ""
    evolution_base_url: str = ""
    evolution_api_key: str = ""
    evolution_instance: str = ""

    # Billing (Asaas). Token configured on the webhook; sent as `asaas-access-token`.
    asaas_webhook_token: str = ""

    # Worker
    schedule_minutes: int = 30
    free_delay_hours: int = 24
    classify_batch_size: int = 200
    dispatch_batch_size: int = 200

    # Sources
    qd_base_url: str = "https://api.queridodiario.org.br"
    qd_query: str = '"concurso público"'
    qd_lookback_days: int = 2
    qd_page_size: int = 100
    qd_max_pages: int = 10
    rss_feeds: Annotated[list[str], NoDecode] = []

    # Monetization: JSON object mapping an Area value to a course affiliate URL.
    affiliate_links: dict[str, str] = {}

    @field_validator("rss_feeds", mode="before")
    @classmethod
    def _split_csv(cls, value: object) -> object:
        if isinstance(value, str):
            return [part.strip() for part in value.split(",") if part.strip()]
        return value

    @property
    def use_llm(self) -> bool:
        return bool(self.anthropic_api_key)


@lru_cache
def get_settings() -> Settings:
    return Settings()


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
