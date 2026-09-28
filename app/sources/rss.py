"""Generic RSS/Atom adapter. Only register feeds whose terms allow this use."""

from __future__ import annotations

import calendar
import html
import re
from collections.abc import Iterator
from datetime import UTC, datetime
from urllib.parse import urlparse

import feedparser
import httpx

from app.domain import SourceItem

_TAGS = re.compile(r"<[^>]+>")


class RssSource:
    def __init__(self, http: httpx.Client, url: str, name: str | None = None) -> None:
        self._http = http
        self._url = url
        self.name = (name or f"rss:{urlparse(url).netloc}")[:50]

    def fetch(self, now: datetime) -> Iterator[SourceItem]:
        response = self._http.get(self._url, timeout=30, follow_redirects=True)
        response.raise_for_status()
        feed = feedparser.parse(response.content)
        for entry in feed.entries:
            link = entry.get("link")
            external_id = entry.get("id") or link
            if not external_id or not link:
                continue
            summary = entry.get("summary") or ""
            yield SourceItem(
                source=self.name,
                external_id=str(external_id)[:255],
                title=_strip_html(entry.get("title") or "")[:500],
                url=link,
                content=_strip_html(summary),
                published_at=_published_at(entry),
            )


def _strip_html(value: str) -> str:
    return " ".join(html.unescape(_TAGS.sub(" ", value)).split())


def _published_at(entry: feedparser.FeedParserDict) -> datetime | None:
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if not parsed:
        return None
    return datetime.fromtimestamp(calendar.timegm(parsed), tz=UTC)
