from __future__ import annotations

from typing import Any, Protocol


class Adapter(Protocol):
    """Every ingestion method implements this and returns unified-schema records,
    so the rest of the router never learns how a story was read."""

    name: str

    def fetch(self, source: dict[str, Any], limit: int, since: str | None) -> list[dict[str, Any]]:
        ...


class FetchError(RuntimeError):
    pass
