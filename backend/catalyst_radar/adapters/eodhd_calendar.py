from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx

from catalyst_radar.adapters._retry import retry_async
from catalyst_radar.adapters.base import FetchResult, SourceAdapter
from catalyst_radar.adapters.eodhd import EodhdConfigError
from catalyst_radar.config import settings
from catalyst_radar.dedup import dedup_key as make_dedup_key
from catalyst_radar.dedup import source_event_id as make_source_event_id


def _parse_date(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(str(value)[:10], "%Y-%m-%d").replace(tzinfo=UTC)
    except (ValueError, TypeError):
        return None


class _EodhdCalendarBase(SourceAdapter):
    source_name = "eodhd"
    _path: str = ""
    _list_key: str = ""

    def __init__(self, api_key: str | None = None, base_url: str | None = None) -> None:
        self.api_key = api_key if api_key is not None else settings.eodhd_api_key
        self.base_url = (base_url or settings.eodhd_base_url).rstrip("/")
        self.timeout = settings.eodhd_request_timeout_seconds

    def _require_key(self) -> str:
        if not self.api_key:
            raise EodhdConfigError("EODHD_API_KEY is not configured")
        return self.api_key

    async def fetch(self, target: tuple[date, date]) -> FetchResult:
        key = self._require_key()
        date_from, date_to = target
        url = f"{self.base_url}/calendar/{self._path}"
        params = {
            "api_token": key,
            "fmt": "json",
            "from": date_from.isoformat(),
            "to": date_to.isoformat(),
        }

        async def _do() -> httpx.Response:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                return await client.get(url, params=params)

        # Quota (HTTP 402) returns a normal response, not an exception, so it
        # never loops here — only genuine timeouts / resets are retried.
        resp = await retry_async(
            _do, attempts=3, base_delay=1.0, max_delay=8.0, label=f"eodhd_{self._path}"
        )

        items: list[dict[str, Any]] = []
        payload: Any = resp.text
        if resp.status_code == 200:
            try:
                parsed = resp.json()
                payload = parsed
                if isinstance(parsed, dict):
                    raw = parsed.get(self._list_key, [])
                    items = [r for r in raw if isinstance(r, dict)]
            except ValueError:
                payload = resp.text
        return FetchResult(
            source_name=self.source_name,
            schema_name=f"eodhd.{self._path}.v1",
            source_url=str(resp.request.url),
            http_status=resp.status_code,
            payload=payload,
            items=items,
        )


class EodhdEarningsAdapter(_EodhdCalendarBase):
    _path = "earnings"
    _list_key = "earnings"

    def normalize(self, raw_item: dict[str, Any]) -> dict[str, Any]:
        code = str(raw_item.get("code", "")).strip()  # e.g. AAPL.US
        symbol, _, exchange = code.partition(".")
        return {
            "event_type": "earnings",
            "symbol": symbol or code,
            "exchange": exchange or None,
            "company_name": None,
            "title": f"{code} earnings",
            "event_date": _parse_date(raw_item.get("report_date")),
            "payload": {
                "code": code,
                "report_date": raw_item.get("report_date"),
                "fiscal_period_end": raw_item.get("date"),
                "before_after_market": raw_item.get("before_after_market"),
                "currency": raw_item.get("currency"),
                "estimate": raw_item.get("estimate"),
                "actual": raw_item.get("actual"),
                "difference": raw_item.get("difference"),
                "percent": raw_item.get("percent"),
            },
        }

    def source_event_id(self, raw_item: dict[str, Any]) -> str:
        return make_source_event_id(
            self.source_name,
            "earnings",
            str(raw_item.get("code", "")),
            str(raw_item.get("report_date", "")),
            str(raw_item.get("date", "")),
        )

    def dedup_key(self, raw_or_normalized_item: dict[str, Any]) -> str:
        return make_dedup_key(self.source_event_id(raw_or_normalized_item))


class EodhdIpoAdapter(_EodhdCalendarBase):
    _path = "ipos"
    _list_key = "ipos"

    def normalize(self, raw_item: dict[str, Any]) -> dict[str, Any]:
        name = str(raw_item.get("name", "")).strip()
        code = str(raw_item.get("code", "")).strip()
        exchange = str(raw_item.get("exchange", "")).strip()
        return {
            "event_type": "ipo",
            "symbol": code if code and code != "N/A" else None,
            "exchange": exchange or None,
            "company_name": name or None,
            "title": f"IPO: {name or code}",
            "event_date": _parse_date(raw_item.get("start_date")),
            "payload": {
                "code": raw_item.get("code"),
                "name": name,
                "exchange": exchange,
                "currency": raw_item.get("currency"),
                "start_date": raw_item.get("start_date"),
                "filing_date": raw_item.get("filing_date"),
                "amended_date": raw_item.get("amended_date"),
                "price_from": raw_item.get("price_from"),
                "price_to": raw_item.get("price_to"),
                "offer_price": raw_item.get("offer_price"),
                "shares": raw_item.get("shares"),
                "deal_type": raw_item.get("deal_type"),
            },
        }

    def source_event_id(self, raw_item: dict[str, Any]) -> str:
        code = raw_item.get("code")
        code_or_name = code if code and code != "N/A" else raw_item.get("name", "")
        return make_source_event_id(
            self.source_name,
            "ipo",
            str(raw_item.get("exchange", "")),
            str(code_or_name),
            str(raw_item.get("start_date", "")),
            str(raw_item.get("deal_type", "")),
        )

    def dedup_key(self, raw_or_normalized_item: dict[str, Any]) -> str:
        return make_dedup_key(self.source_event_id(raw_or_normalized_item))


def default_window(lookahead_days: int) -> tuple[date, date]:
    # UTC matches the EODHD calendar's own date semantics — using local
    # time on a container that may run in any timezone risks an off-by-day
    # window on either edge.
    today = datetime.now(UTC).date()
    return today, today + timedelta(days=lookahead_days)
