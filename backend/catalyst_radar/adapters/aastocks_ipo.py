"""AAStocks HK IPO calendar adapter.

AAStocks serves no clean server-rendered IPO table — the calendar is
JS-hydrated. We render it with Playwright (headless Chromium), then
parse the rendered DOM. Every upcoming/recent IPO is a ``<td>`` carrying
clean ``data-*`` attributes (symbol, desp, listdate, app dates), so the
parser is a pure function over the rendered HTML and is fixture-tested
offline without a browser.
"""

from datetime import UTC, datetime
from html.parser import HTMLParser
from typing import Any

from catalyst_radar.adapters._retry import retry_async
from catalyst_radar.adapters.base import FetchResult, SourceAdapter
from catalyst_radar.adapters.hk_codes import pad_code
from catalyst_radar.config import settings
from catalyst_radar.dedup import dedup_key as make_dedup_key
from catalyst_radar.dedup import source_event_id as make_source_event_id
from catalyst_radar.logging import get_logger

log = get_logger(__name__)


class AastocksRenderError(RuntimeError):
    """Headless render failed (browser missing, timeout, page changed)."""


def _parse_date(value: str | None) -> datetime | None:
    """AAStocks emits YYYY/MM/DD in data-listdate."""
    if not value:
        return None
    try:
        return datetime.strptime(str(value)[:10], "%Y/%m/%d").replace(tzinfo=UTC)
    except (ValueError, TypeError):
        return None


class _IpoCellParser(HTMLParser):
    """Collect every <td data-symbol=...> with its data-* attributes."""

    def __init__(self) -> None:
        super().__init__()
        self.rows: list[dict[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "td":
            return
        a = {k: (v or "") for k, v in attrs}
        if not a.get("data-symbol"):
            return
        self.rows.append(a)


def parse_ipocalendar(html: str) -> list[dict[str, Any]]:
    """Pure: rendered AAStocks IPO-calendar HTML -> raw item dicts.

    Deduped by stock code (the calendar can repeat a code across the
    subscription and listing day cells); the richest row wins.
    """
    p = _IpoCellParser()
    p.feed(html or "")
    by_code: dict[str, dict[str, Any]] = {}
    for a in p.rows:
        code = pad_code(a.get("data-symbol"))
        name = (a.get("data-desp") or "").strip()
        if not code or not name:
            continue
        item = {
            "code": code,
            "name": name,
            "list_date": a.get("data-listdate") or None,
            "app_open": a.get("data-appopen") or None,
            "app_close": a.get("data-appclose") or None,
            "ann_date": a.get("data-anndate") or None,
            "list_label": (a.get("data-listlabel") or "").strip() or None,
        }
        prev = by_code.get(code)
        # Prefer the row that actually carries a listing date.
        if prev is None or (item["list_date"] and not prev.get("list_date")):
            by_code[code] = item
    return list(by_code.values())


class AastocksIpoAdapter(SourceAdapter):
    source_name = "aastocks"

    def __init__(self, url: str | None = None, timeout_seconds: int | None = None) -> None:
        self.url = url or settings.aastocks_ipo_calendar_url
        self.timeout_seconds = timeout_seconds or settings.aastocks_render_timeout_seconds

    async def _render(self) -> str:
        # Chromium launch failures (sandbox/socket hiccups) and goto/selector
        # timeouts are transient — a fresh browser usually succeeds. Keep the
        # attempt count SMALL since each launch is expensive; ``retry_on`` is
        # broad because Playwright surfaces these as plain exceptions, not
        # httpx types, and the outer fetch() fail-soft catches anything left.
        return await retry_async(
            self._render_once,
            attempts=2,
            base_delay=2.0,
            max_delay=5.0,
            retry_on=(Exception,),
            label="aastocks_render",
        )

    async def _render_once(self) -> str:
        from playwright.async_api import async_playwright

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(args=["--no-sandbox"])
            try:
                page = await browser.new_page()
                await page.goto(self.url, timeout=self.timeout_seconds * 1000)
                # The IPO rows hydrate into table.tblCalendar after JS runs.
                await page.wait_for_selector(
                    "table.tblCalendar td[data-symbol]",
                    timeout=self.timeout_seconds * 1000,
                )
                el = await page.query_selector("table.tblCalendar")
                return await el.inner_html() if el else await page.content()
            finally:
                await browser.close()

    async def fetch(self, target: Any = None) -> FetchResult:
        try:
            html = await self._render()
        except Exception as exc:  # noqa: BLE001 - render is best-effort
            log.warning("aastocks_render_failed", error=repr(exc))
            return FetchResult(
                source_name=self.source_name,
                schema_name="aastocks.ipocalendar.v1",
                source_url=self.url,
                http_status=0,
                payload=f"render failed: {exc!r}",
                items=[],
            )
        items = parse_ipocalendar(html)
        return FetchResult(
            source_name=self.source_name,
            schema_name="aastocks.ipocalendar.v1",
            source_url=self.url,
            http_status=200,
            payload=html,
            items=items,
        )

    def normalize(self, raw_item: dict[str, Any]) -> dict[str, Any]:
        code = pad_code(raw_item.get("code")) or str(raw_item.get("code") or "")
        name = raw_item["name"]
        return {
            "event_type": "ipo",
            "symbol": code,
            "exchange": "HKSE",  # maps to country HK via ipo_sync mapping
            "company_name": name,
            "title": f"IPO: {name}",
            "event_date": _parse_date(raw_item.get("list_date")),
            "payload": {
                "code": code,
                "name": name,
                "exchange": "HKSE",
                "source": "aastocks",
                "list_date": raw_item.get("list_date"),
                "app_open": raw_item.get("app_open"),
                "app_close": raw_item.get("app_close"),
                "ann_date": raw_item.get("ann_date"),
                "list_label": raw_item.get("list_label"),
            },
        }

    def source_event_id(self, raw_item: dict[str, Any]) -> str:
        # Stock code is the stable cross-source key (AAStocks/HKEX/EODHD
        # all carry it), so HK IPO rows dedupe onto one Event.
        return make_source_event_id("hkex", "ipo", pad_code(raw_item.get("code")))

    def dedup_key(self, raw_or_normalized_item: dict[str, Any]) -> str:
        return make_dedup_key(self.source_event_id(raw_or_normalized_item))
