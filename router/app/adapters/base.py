from __future__ import annotations

from typing import Any, Protocol


class Adapter(Protocol):
    """Every ingestion tier implements this and returns unified-schema records,
    so the rest of the router never learns which tier a story came from."""

    name: str

    def fetch(self, source: dict[str, Any], limit: int, since: str | None) -> list[dict[str, Any]]:
        ...


class FetchError(RuntimeError):
    pass
