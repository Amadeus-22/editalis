"""The contract every data source implements."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from typing import Protocol

from app.domain import SourceItem


class Source(Protocol):
    """A data source yields raw items. It never touches the database.

    `external_id` must be stable across runs: together with `name` it is the
    idempotency key of RawItem.
    """

    name: str

    def fetch(self, now: datetime) -> Iterable[SourceItem]: ...
