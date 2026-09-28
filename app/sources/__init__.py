"""Data source adapters. One module per source; register new ones in `pipeline.build_sources()`."""

from app.sources.base import Source

__all__ = ["Source"]
