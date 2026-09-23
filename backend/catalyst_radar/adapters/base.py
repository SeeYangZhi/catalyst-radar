from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class FetchResult:
    """Raw fetch output: the payload plus enough metadata to persist it."""

    source_name: str
    schema_name: str
    source_url: str
    http_status: int
    payload: Any
    items: list[dict[str, Any]] = field(default_factory=list)


class SourceAdapter(ABC):
    """Small interface every source adapter implements (PRD Module 8)."""

    source_name: str

    @abstractmethod
    async def fetch(self, target: Any) -> FetchResult: ...

    @abstractmethod
    def normalize(self, raw_item: dict[str, Any]) -> dict[str, Any]: ...

    @abstractmethod
    def source_event_id(self, raw_item: dict[str, Any]) -> str: ...

    @abstractmethod
    def dedup_key(self, raw_or_normalized_item: dict[str, Any]) -> str: ...
