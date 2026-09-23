from typing import Any

import httpx

from catalyst_radar.adapters._retry import retry_async
from catalyst_radar.adapters.base import FetchResult, SourceAdapter
from catalyst_radar.config import settings
from catalyst_radar.dedup import dedup_key as make_dedup_key
from catalyst_radar.dedup import source_event_id as make_source_event_id

# Day-one exchange -> ISO country code (PRD: derive country from exchange).
EXCHANGE_COUNTRY = {"US": "US", "HK": "HK", "KO": "KR", "KQ": "KR"}

EQUITY_TYPES = {
    "common stock",
    "preferred stock",
    "ordinary share",
    "share",
    "stock",
}


class EodhdConfigError(RuntimeError):
    """Raised when EODHD is used without an API key."""


class EodhdCompanyReferenceAdapter(SourceAdapter):
    source_name = "eodhd"

    def __init__(self, api_key: str | None = None, base_url: str | None = None) -> None:
        self.api_key = api_key if api_key is not None else settings.eodhd_api_key
        self.base_url = (base_url or settings.eodhd_base_url).rstrip("/")
        self.timeout = settings.eodhd_request_timeout_seconds

    def _require_key(self) -> str:
        if not self.api_key:
            raise EodhdConfigError("EODHD_API_KEY is not configured")
        return self.api_key

    async def _get(self, url: str, params: dict[str, str]) -> httpx.Response:
        """GET with a short transient retry. A quota trip (HTTP 402) comes
        back as a normal response, not an exception, so it never loops here —
        only genuine timeouts / connection resets are retried."""

        async def _do() -> httpx.Response:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                return await client.get(url, params=params)

        return await retry_async(
            _do, attempts=3, base_delay=1.0, max_delay=8.0, label="eodhd"
        )

    async def fetch(self, target: str) -> FetchResult:
        """Fetch the exchange symbol list for one exchange code."""
        key = self._require_key()
        url = f"{self.base_url}/exchange-symbol-list/{target}"
        params = {"api_token": key, "fmt": "json"}
        resp = await self._get(url, params)
        items: list[dict[str, Any]] = []
        if resp.status_code == 200:
            try:
                parsed = resp.json()
                if isinstance(parsed, list):
                    items = [r for r in parsed if isinstance(r, dict)]
            except ValueError:
                items = []
        return FetchResult(
            source_name=self.source_name,
            schema_name="eodhd.exchange_symbol_list.v1",
            source_url=str(resp.request.url),
            http_status=resp.status_code,
            payload=items if items else resp.text,
            items=items,
        )

    async def search(self, query: str) -> FetchResult:
        """Fallback resolver: EODHD symbol search."""
        key = self._require_key()
        url = f"{self.base_url}/search/{query}"
        params = {"api_token": key, "fmt": "json"}
        resp = await self._get(url, params)
        items: list[dict[str, Any]] = []
        if resp.status_code == 200:
            try:
                parsed = resp.json()
                if isinstance(parsed, list):
                    items = [r for r in parsed if isinstance(r, dict)]
            except ValueError:
                items = []
        return FetchResult(
            source_name=self.source_name,
            schema_name="eodhd.symbol_search.v1",
            source_url=str(resp.request.url),
            http_status=resp.status_code,
            payload=items if items else resp.text,
            items=items,
        )

    @staticmethod
    def is_equity_like(raw_item: dict[str, Any]) -> bool:
        return str(raw_item.get("Type", "")).strip().lower() in EQUITY_TYPES

    def normalize(self, raw_item: dict[str, Any]) -> dict[str, Any]:
        code = str(raw_item.get("Code", "")).strip()
        exchange = str(raw_item.get("Exchange", "")).strip()
        country = EXCHANGE_COUNTRY.get(exchange, str(raw_item.get("Country", "")).strip() or None)
        return {
            "symbol": code,
            "exchange": exchange,
            "company_name": str(raw_item.get("Name", "")).strip() or code,
            "country": country,
            "currency": (raw_item.get("Currency") or None),
            "isin": (raw_item.get("Isin") or raw_item.get("ISIN") or None),
            "instrument_type": str(raw_item.get("Type", "")).strip() or None,
            "source": self.source_name,
            "source_payload": raw_item,
        }

    def source_event_id(self, raw_item: dict[str, Any]) -> str:
        return make_source_event_id(
            self.source_name,
            "company",
            str(raw_item.get("Exchange", "")),
            str(raw_item.get("Code", "")),
        )

    def dedup_key(self, raw_or_normalized_item: dict[str, Any]) -> str:
        item = raw_or_normalized_item
        exchange = item.get("exchange") or item.get("Exchange")
        symbol = item.get("symbol") or item.get("Code")
        return make_dedup_key("eodhd", "company", str(exchange), str(symbol))
