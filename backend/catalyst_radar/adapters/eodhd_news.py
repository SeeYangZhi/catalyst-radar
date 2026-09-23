from typing import Any

import httpx

from catalyst_radar.adapters.base import FetchResult, SourceAdapter
from catalyst_radar.adapters.eodhd import EodhdConfigError
from catalyst_radar.config import settings
from catalyst_radar.dedup import dedup_key as make_dedup_key
from catalyst_radar.dedup import source_event_id as make_source_event_id
from catalyst_radar.dedup import stable_hash

# company_reference / tracked_companies store the LISTING exchange
# (mirrors EODHD's /calendar conventions: "NASDAQ", "NYSE", "HK", "KO"…),
# but EODHD's /news endpoint requires a COUNTRY suffix ("MU.US", not
# "MU.NASDAQ"), so US-listed tickers come back empty without this map.
# HK/KO/KQ already match EODHD's news suffix; unknown exchanges pass
# through as a best effort.
_US_LISTING_EXCHANGES = frozenset(
    {
        "NASDAQ",
        "NYSE",
        "NYSE ARCA",
        "NYSE MKT",
        "AMEX",
        "BATS",
        "OTC",
        "OTCBB",
        "OTCCE",
        "OTCGREY",
        "OTCMKTS",
        "OTCQB",
        "OTCQX",
        "PINK",
        "US",
    }
)


def to_news_code(symbol: str, exchange: str | None) -> str:
    """Translate (symbol, listing_exchange) into the ticker code EODHD's
    news endpoint expects."""
    ex = (exchange or "").strip().upper()
    suffix = "US" if ex in _US_LISTING_EXCHANGES else ex
    return f"{symbol}.{suffix}" if suffix else symbol


class EodhdNewsAdapter(SourceAdapter):
    """Candidate catalyst source. Never notify directly from raw news —
    deterministic prefilters + LLM classification gate alert creation."""

    source_name = "eodhd_news"

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        max_pages: int = 1,
    ) -> None:
        self.api_key = api_key if api_key is not None else settings.eodhd_api_key
        self.base_url = (base_url or settings.eodhd_base_url).rstrip("/")
        self.timeout = settings.eodhd_request_timeout_seconds
        # 1 page (50 items) is enough for the steady-state poll cadence;
        # bump on first-time backfill for a newly tracked company so the
        # last few weeks of news aren't permanently invisible.
        self.max_pages = max(1, max_pages)

    def _require_key(self) -> str:
        if not self.api_key:
            raise EodhdConfigError("EODHD_API_KEY is not configured")
        return self.api_key

    async def fetch(self, target: str) -> FetchResult:
        """Fetch recent news for one EODHD symbol code (e.g. AAPL.US)."""
        key = self._require_key()
        url = f"{self.base_url}/news"
        per_page = 50
        items: list[dict[str, Any]] = []
        last_resp: httpx.Response | None = None
        last_payload: Any = None
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            for page in range(self.max_pages):
                params = {
                    "api_token": key,
                    "fmt": "json",
                    "s": target,
                    "limit": per_page,
                    "offset": page * per_page,
                }
                resp = await client.get(url, params=params)
                last_resp = resp
                if resp.status_code != 200:
                    last_payload = resp.text
                    break
                try:
                    parsed = resp.json()
                except ValueError:
                    last_payload = resp.text
                    break
                last_payload = parsed
                if not isinstance(parsed, list):
                    break
                page_items = [r for r in parsed if isinstance(r, dict)]
                items.extend(page_items)
                if len(page_items) < per_page:
                    break  # exhausted the feed

        assert last_resp is not None
        return FetchResult(
            source_name=self.source_name,
            schema_name="eodhd.news.v1",
            source_url=str(last_resp.request.url),
            http_status=last_resp.status_code,
            payload=last_payload,
            items=items,
        )

    def normalize(self, raw_item: dict[str, Any]) -> dict[str, Any]:
        return {
            "title": (raw_item.get("title") or "").strip(),
            "content": (raw_item.get("content") or "").strip(),
            "link": raw_item.get("link"),
            "date": raw_item.get("date"),
            "symbols": raw_item.get("symbols") or [],
            "tags": raw_item.get("tags") or [],
            "sentiment": raw_item.get("sentiment") or {},
        }

    def source_event_id(self, raw_item: dict[str, Any]) -> str:
        link = raw_item.get("link") or ""
        basis = link or f"{raw_item.get('title', '')}|{raw_item.get('date', '')}"
        return make_source_event_id("eodhd", "news", stable_hash(basis))

    def dedup_key(self, raw_or_normalized_item: dict[str, Any]) -> str:
        item = raw_or_normalized_item
        link = item.get("link") or ""
        title = item.get("title") or ""
        date = item.get("date") or ""
        content = item.get("content") or ""
        # URL first; fall back to title+date+content hash for repostings.
        return make_dedup_key(link or stable_hash(title, date, content[:500]))
