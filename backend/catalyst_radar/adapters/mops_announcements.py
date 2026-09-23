"""Taiwan material-information announcements via MOPS (公開資訊觀測站).

option 2: EODHD's /news returns ``[]`` for Taiwan listings (verified
live on 6451.TW / 6451.TWO), leaving small-cap TW names with web_search as
their only news source. MOPS — the TWSE's Market Observation Post System at
``mops.twse.com.tw`` — is the *authoritative primary source*: listed and
TPEx companies are legally required to disclose material information there.

Endpoint (discovered 2026-06-11, mirrors what the MOPS SPA itself calls):

    POST https://mops.twse.com.tw/mops/api/t05st01
    Content-Type: application/json
    {"companyId": "6451", "year": "115", "month": "6",
     "firstDay": "", "lastDay": ""}

- ``year`` is the ROC (民國) calendar year = CE − 1911 (2026 → 115).
- ``firstDay`` / ``lastDay`` must be present (empty strings are fine) or the
  API returns ``{"code": 500, "message": "傳入參數異常"}``.
- Rows-present months return ``code 200`` with ``result.data`` =
  ``[co_id, abbrev, "115/06/01", "10:38:43", subject, detail_ref]`` rows.
- Months with no announcements return ``code 406`` / ``"查無相符資料"`` with
  ``result: null`` — an *empty* outcome, not an error.
- The legacy ``/mops/web/ajax_t05st01`` form endpoint is WAF-blocked for
  non-browser clients ("FOR SECURITY REASONS…"); the JSON API above works
  with a plain User-Agent header.

Each sweep fetches the current and previous ROC month (Taipei clock) so the
21-day catalyst freshness gate is always fully covered across month
boundaries, including for freshly-tracked companies.

Announcements have no stable public permalink (the SPA renders details via a
POST popup), so items carry ``link=None`` — the catalyst pipeline then keys
cross-source dedup on the normalized title (``content_text_key``), which is
exactly right for link-less primary disclosures.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from catalyst_radar.adapters.base import FetchResult, SourceAdapter
from catalyst_radar.config import settings
from catalyst_radar.dedup import dedup_key as make_dedup_key
from catalyst_radar.dedup import source_event_id as make_source_event_id
from catalyst_radar.dedup import stable_hash
from catalyst_radar.logging import get_logger

log = get_logger(__name__)

_API_URL = "https://mops.twse.com.tw/mops/api/t05st01"
_TAIPEI = ZoneInfo("Asia/Taipei")
# The bare-curl default UA is WAF-blocked on mops.twse.com.tw; a plain
# browser-family UA is accepted (verified live).
_HEADERS = {
    "User-Agent": settings.scraper_user_agent,
}
_TIMEOUT = 30.0
# "No announcements this month" — an empty result, not a failure.
_EMPTY_CODE = 406

# Exchange labels under which Taiwan listings appear in company_reference /
# tracked_companies. Mirrors the frontend's NEWS_GAP_EXCHANGES .
_TW_EXCHANGES = frozenset({"TW", "TWO", "TWSE", "TPEX"})

# MOPS has no per-article outlet; label items so review UI / classifier
# context show the primary source.
_SOURCE_LABEL = "公開資訊觀測站"


def supports(exchange: str | None) -> bool:
    """True when this exchange's material information is disclosed on MOPS
    (i.e. a Taiwan TWSE / TPEx listing)."""
    return (exchange or "").strip().upper() in _TW_EXCHANGES


def roc_date_to_iso(date_s: Any, time_s: Any) -> str | None:
    """``"115/06/01"`` + ``"10:38:43"`` (ROC calendar, Taipei wall clock)
    -> ``"2026-06-01T10:38:43+08:00"``. The explicit offset keeps the
    downstream UTC-assume-on-naive parser (``catalyst_classify._news_dt``) from
    skewing the freshness-gate age by 8 hours. None on any malformed input."""
    raw = str(date_s or "").strip()
    if not raw:
        return None
    parts = raw.split("/")
    if len(parts) != 3:
        return None
    try:
        year = int(parts[0]) + 1911  # ROC -> CE
        month = int(parts[1])
        day = int(parts[2])
        t = str(time_s or "").strip() or "00:00:00"
        hh, mm, ss = (int(x) for x in t.split(":"))
        dt = datetime(year, month, day, hh, mm, ss, tzinfo=_TAIPEI)
    except (TypeError, ValueError):
        return None
    return dt.strftime("%Y-%m-%dT%H:%M:%S+08:00")


def _roc_compact_to_iso_date(raw: Any) -> str | None:
    """``"1150506"`` (compact ROC date from the detail-ref parameters)
    -> ``"2026-05-06"``. None on malformed input."""
    s = str(raw or "").strip()
    if len(s) != 7 or not s.isdigit():
        return None
    try:
        year = int(s[:3]) + 1911
        d = datetime(year, int(s[3:5]), int(s[5:7]))
    except ValueError:
        return None
    return d.strftime("%Y-%m-%d")


def _clean_subject(raw: Any) -> str:
    """Collapse the multi-line 主旨 cell (announcements wrap with \\r\\n)
    into a single-line title."""
    return " ".join(str(raw or "").split()).strip()


def parse_response(doc: Any) -> list[dict[str, Any]]:
    """Pure: one t05st01 JSON envelope -> normalized
    ``{title, content, link, date, source_label, co_id, seq}`` items.

    Defensive throughout: empty months (code 406 / result null), garbage
    envelopes, and malformed rows all fail soft — bad rows are skipped, an
    unparseable document yields ``[]`` (zero-yield is detectable upstream
    via the source-run item counts)."""
    if not isinstance(doc, dict):
        return []
    result = doc.get("result")
    if not isinstance(result, dict):
        return []  # code 406 "查無相符資料" lands here: result is null
    data = result.get("data")
    if not isinstance(data, list):
        return []

    items: list[dict[str, Any]] = []
    for row in data:
        # Row shape: [co_id, abbrev, ROC-date, time, subject, detail_ref]
        if not isinstance(row, (list, tuple)) or len(row) < 5:
            continue
        title = _clean_subject(row[4])
        if not title:
            continue
        co_id = str(row[0] or "").strip()
        abbrev = str(row[1] or "").strip()
        date_iso = roc_date_to_iso(row[2], row[3])
        seq: str | None = None
        enter_date: str | None = None
        if len(row) > 5 and isinstance(row[5], dict):
            params = row[5].get("parameters")
            if isinstance(params, dict):
                if params.get("serialNumber") is not None:
                    seq = str(params["serialNumber"]).strip() or None
                enter_date = _roc_compact_to_iso_date(params.get("enterDate"))
        items.append(
            {
                "title": title,
                # Subject + provenance line: keeps short CJK subjects above
                # the prefilter's 40-char too_short bar and tells the
                # classifier this is a mandatory primary-source disclosure,
                # not press coverage.
                "content": f"{title}（{_SOURCE_LABEL}重大訊息 / TWSE MOPS material "
                f"information disclosure, {abbrev} {co_id}）",
                "link": None,  # no stable permalink — see module docstring
                "date": date_iso,
                "source_label": _SOURCE_LABEL,
                "co_id": co_id,
                "enter_date": enter_date,
                "seq": seq,
            }
        )
    return items


def _months_to_sweep(now: datetime | None = None) -> list[tuple[int, int]]:
    """Current and previous (ROC year, month) by the Taipei clock — together
    they always cover the 21-day catalyst freshness window."""
    local = (now or datetime.now(UTC)).astimezone(_TAIPEI)
    first_of_month = local.replace(day=1)
    prev = first_of_month - timedelta(days=1)
    return [
        (local.year - 1911, local.month),
        (prev.year - 1911, prev.month),
    ]


class MopsAnnouncementsAdapter(SourceAdapter):
    """Candidate catalyst source. Like every news adapter, never notifies
    directly — deterministic prefilters + LLM classification gate alerts."""

    source_name = "mops_announcements"
    schema_name = "mops.t05st01.v1"
    source_url = _API_URL

    async def fetch(self, target: str) -> FetchResult:
        """``target`` is the Taiwan stock code (e.g. ``6451``). Sweeps the
        current + previous ROC month; never raises. Each month is fetched
        and parsed under its own try (mirroring the per-URL pattern in
        ``hkex_newly_listed``) so one bad month — a transport error or a
        non-JSON WAF block page — never discards the other month's items."""
        responses: list[Any] = []
        items: list[dict[str, Any]] = []
        months = _months_to_sweep()
        ok = False
        last_error: str | None = None
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT, headers=_HEADERS) as client:
                for roc_year, month in months:
                    body = {
                        "companyId": target,
                        "year": str(roc_year),
                        "month": str(month),
                        "firstDay": "",
                        "lastDay": "",
                    }
                    try:
                        resp = await client.post(_API_URL, json=body)
                        doc = resp.json()
                    except Exception as exc:  # noqa: BLE001 - one bad month must not abort the sweep
                        log.warning(
                            "mops_month_fetch_failed",
                            co_id=target,
                            month=f"{roc_year}/{month:02d}",
                            error=repr(exc),
                        )
                        last_error = repr(exc)
                        continue
                    responses.append(doc)
                    code = doc.get("code") if isinstance(doc, dict) else None
                    if resp.status_code == 200 and code in (200, _EMPTY_CODE):
                        ok = True
                        items.extend(parse_response(doc))
                    else:
                        last_error = f"http {resp.status_code} api-code {code}"
        except Exception as exc:  # noqa: BLE001 - one bad company must not abort the run
            log.warning("mops_fetch_failed", co_id=target, error=repr(exc))
            last_error = repr(exc)

        if not ok:
            log.warning("mops_fetch_rejected", co_id=target, error=last_error)
        return FetchResult(
            source_name=self.source_name,
            schema_name=self.schema_name,
            source_url=self.source_url,
            http_status=200 if ok else 0,
            payload={
                "co_id": target,
                "months": [f"{y}/{m:02d}" for y, m in months],
                "responses": responses,
                "error": last_error,
            },
            items=items,
        )

    def normalize(self, raw_item: dict[str, Any]) -> dict[str, Any]:
        # fetch already emits the {title, content, link, date} shape the
        # pipeline consumes; nothing left to reshape.
        return raw_item

    def source_event_id(self, raw_item: dict[str, Any]) -> str:
        """``mops:<co_id>:<date>:<seq-or-hash>``.

        MOPS serial numbers are a stable sequence per *enterDate* (the
        data-entry day from the detail-ref parameters), NOT per the
        displayed 發言日期 — two same-day announcements can both carry
        seq 1 with different enterDates (observed live on 6451). So the
        seq fast-path keys on enterDate; anything missing either falls
        back to display-date + title hash."""
        co_id = str(raw_item.get("co_id") or "").strip()
        seq = raw_item.get("seq")
        enter_date = raw_item.get("enter_date")
        if seq and enter_date:
            return make_source_event_id("mops", co_id, enter_date, str(seq))
        date_part = str(raw_item.get("date") or "")[:10] or "undated"
        return make_source_event_id(
            "mops", co_id, date_part, stable_hash(raw_item.get("title"))
        )

    def dedup_key(self, raw_item: dict[str, Any]) -> str:
        return make_dedup_key(
            raw_item.get("co_id"),
            str(raw_item.get("date") or "")[:10],
            raw_item.get("title"),
        )
