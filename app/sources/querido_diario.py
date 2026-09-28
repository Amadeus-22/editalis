"""Querido Diário (Open Knowledge Brasil): full-text search over municipal gazettes.

API docs: https://api.queridodiario.org.br/docs

NOTE: written against the documented `GET /gazettes` contract but not yet
verified live (the API was unreachable when this adapter was written). Confirm
parameter names and the response shape before relying on it in production.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from app.domain import SourceItem

logger = logging.getLogger(__name__)


class QueridoDiarioSource:
    name = "querido_diario"

    def __init__(
        self,
        http: httpx.Client,
        base_url: str,
        query: str,
        lookback_days: int = 2,
        page_size: int = 100,
        max_pages: int = 10,
    ) -> None:
        self._http = http
        self._url = f"{base_url.rstrip('/')}/gazettes"
        self._query = query
        self._lookback = timedelta(days=lookback_days)
        self._page_size = page_size
        self._max_pages = max_pages

    def fetch(self, now: datetime) -> Iterator[SourceItem]:
        since = (now - self._lookback).date().isoformat()
        offset = 0
        for _ in range(self._max_pages):
            response = self._http.get(
                self._url,
                params={
                    "querystring": self._query,
                    "published_since": since,
                    "offset": offset,
                    "size": self._page_size,
                    "excerpt_size": 1500,
                    "number_of_excerpts": 3,
                    "sort_by": "descending_date",
                },
                timeout=30,
            )
            response.raise_for_status()
            data = response.json()
            gazettes: list[dict[str, Any]] = data.get("gazettes") or []
            for gazette in gazettes:
                item = self._to_item(gazette)
                if item is not None:
                    yield item
            offset += len(gazettes)
            if not gazettes or offset >= int(data.get("total_gazettes") or 0):
                return
        logger.warning(
            "Querido Diário: stopped after %d pages (offset=%d)", self._max_pages, offset
        )

    def _to_item(self, gazette: dict[str, Any]) -> SourceItem | None:
        url = gazette.get("url") or gazette.get("txt_url")
        territory_id = gazette.get("territory_id")
        gazette_date = gazette.get("date")
        if not url or not territory_id or not gazette_date:
            logger.debug("Querido Diário: skipping incomplete gazette %r", gazette)
            return None

        territory = gazette.get("territory_name") or territory_id
        state = gazette.get("state_code")
        url_hash = hashlib.sha1(url.encode()).hexdigest()[:16]
        excerpts = [e for e in gazette.get("excerpts") or [] if isinstance(e, str)]
        return SourceItem(
            source=self.name,
            external_id=f"{territory_id}:{gazette_date}:{url_hash}",
            title=f"Diário Oficial de {territory}" + (f" ({state})" if state else ""),
            url=url,
            content="\n\n".join(excerpts),
            published_at=_parse_date(gazette_date),
            uf_hint=state,
            organization_hint=f"Município de {territory}",
        )


def _parse_date(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value).replace(tzinfo=UTC)
    except ValueError:
        return None
