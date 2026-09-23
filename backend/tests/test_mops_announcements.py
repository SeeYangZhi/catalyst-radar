"""MOPS (公開資訊觀測站) material-information adapter tests .

Parser tests are pinned to synthetic fixtures that mirror the exact shape of
``POST /mops/api/t05st01`` responses (invented company 9917 / 虹橋精密):
a rows-present month (June ROC 115) and an empty month (code 406
"查無相符資料", result null).

Integration tests opt in to the MOPS sweep explicitly — conftest pins
``catalyst_mops_news_enabled`` off for the suite so unrelated TW-company
tests stay deterministic.
"""

import json
from pathlib import Path

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from catalyst_radar.adapters import mops_announcements
from catalyst_radar.adapters.base import FetchResult
from catalyst_radar.adapters.eodhd_news import EodhdNewsAdapter
from catalyst_radar.adapters.mops_announcements import (
    MopsAnnouncementsAdapter,
    parse_response,
    roc_date_to_iso,
    supports,
)
from catalyst_radar.models.company import TrackedCompany
from catalyst_radar.models.event import Event
from catalyst_radar.models.notification import Notification
from catalyst_radar.services.catalyst_sync import sync_catalysts
from catalyst_radar.services.openai_classifier import ClassificationResult

_FIXTURES = Path(__file__).parent / "fixtures"
ROWS = json.loads((_FIXTURES / "mops_announcements.json").read_text())
EMPTY = json.loads((_FIXTURES / "mops_announcements_empty.json").read_text())


# ── exchange routing ──────────────────────────────────────────────────


def test_supports_only_taiwan_exchanges() -> None:
    assert supports("TW") and supports("TWO") and supports("TPEX") and supports("TWSE")
    assert supports(" two ")  # case + whitespace tolerant
    assert not supports("US")
    assert not supports("HK")
    assert not supports("SSE")
    assert not supports(None)


# ── ROC calendar conversion ───────────────────────────────────────────


def test_roc_date_to_iso_converts_to_taipei_offset() -> None:
    # ROC 115 = CE 2026; naive Taipei wall-clock gets an explicit +08:00 so
    # catalyst_classify._news_dt (UTC-assume-on-naive) doesn't skew age by 8h.
    assert roc_date_to_iso("115/06/01", "10:38:43") == "2026-06-01T10:38:43+08:00"


def test_roc_date_to_iso_handles_missing_time() -> None:
    assert roc_date_to_iso("115/06/01", "") == "2026-06-01T00:00:00+08:00"
    assert roc_date_to_iso("115/06/01", None) == "2026-06-01T00:00:00+08:00"


def test_roc_date_to_iso_rejects_garbage() -> None:
    assert roc_date_to_iso("", "10:00:00") is None
    assert roc_date_to_iso(None, None) is None
    assert roc_date_to_iso("not/a/date", "10:00:00") is None
    assert roc_date_to_iso("115/13/40", "10:00:00") is None


# ── response parsing (fixture-pinned) ─────────────────────────────────


def test_parse_response_happy_path() -> None:
    items = parse_response(ROWS)
    assert len(items) == 6
    it = items[0]
    assert it["co_id"] == "9917"
    assert it["title"] == "說明媒體報導"
    assert it["date"] == "2026-06-01T10:38:43+08:00"
    assert it["seq"] == "1"
    assert it["link"] is None  # MOPS announcements have no stable permalink
    assert it["source_label"] == "公開資訊觀測站"
    # content carries the subject plus a provenance line (keeps short CJK
    # subjects above the prefilter's too_short bar and tells the classifier
    # this is a primary-source disclosure).
    assert "說明媒體報導" in it["content"]
    assert "公開資訊觀測站" in it["content"]


def test_parse_response_collapses_multiline_subjects() -> None:
    # Row 3 of the fixture carries a literal \r\n inside the subject.
    items = parse_response(ROWS)
    multiline = items[2]
    assert "\r" not in multiline["title"] and "\n" not in multiline["title"]
    assert multiline["title"].startswith("（補充115/5/20公告）")


def test_parse_response_empty_month_returns_empty_list() -> None:
    # code 406 / "查無相符資料" / result null — the no-announcements case.
    assert parse_response(EMPTY) == []


def test_parse_response_malformed_rows_fail_soft() -> None:
    good = ROWS["result"]["data"][0]
    doc = {
        "code": 200,
        "message": "查詢成功",
        "result": {
            "companyId": "9917",
            "data": [
                ["9917", "虹橋精密"],  # too few cells → skipped
                ["9917", "虹橋精密", "115/06/01", "10:00:00", "   ", {}],  # blank subject
                "not-a-row",  # wrong type → skipped
                good,  # survives
            ],
        },
    }
    items = parse_response(doc)
    assert len(items) == 1
    assert items[0]["title"] == "說明媒體報導"


def test_parse_response_zero_yield_on_garbage() -> None:
    assert parse_response({}) == []
    assert parse_response({"code": 200, "result": None}) == []
    assert parse_response({"code": 200, "result": {"data": "not-a-list"}}) == []
    assert parse_response(None) == []
    assert parse_response([1, 2, 3]) == []


def test_parse_response_unparseable_date_keeps_item_undated() -> None:
    row = ["6451", "訊芯-KY", "garbage", "10:00:00", "公告本公司重大訊息測試用例文字", {}]
    items = parse_response({"code": 200, "result": {"data": [row]}})
    assert len(items) == 1
    assert items[0]["date"] is None  # downstream undated gate decides


# ── source_event_id ───────────────────────────────────────────────────


def test_source_event_id_uses_date_and_seq() -> None:
    a = MopsAnnouncementsAdapter()
    items = parse_response(ROWS)
    sid = a.source_event_id(items[0])
    assert sid == "mops:9917:2026-06-01:1"
    assert a.source_event_id(items[0]) == sid  # stable across calls
    # Two announcements on the same day get distinct seqs.
    assert a.source_event_id(items[1]) != a.source_event_id(items[2])


def test_source_event_id_distinct_when_serials_collide_on_display_date() -> None:
    """Regression (caught on live 6451 data): MOPS serialNumber is unique per
    *enterDate* (data-entry day), NOT per the displayed 發言日期. Two
    announcements both shown on 115/05/13 carried (enterDate 1150513, seq 1)
    and (enterDate 1150506, seq 1) — keying on display-date + seq would
    collide and silently drop the second announcement."""
    a = MopsAnnouncementsAdapter()
    detail_a = {"parameters": {"enterDate": "1150513", "serialNumber": "1"}}
    detail_b = {"parameters": {"enterDate": "1150506", "serialNumber": "1"}}
    doc = {
        "code": 200,
        "result": {
            "data": [
                ["6451", "訊芯-KY", "115/05/13", "15:51:43", "董事會決議股利分派", detail_a],
                ["6451", "訊芯-KY", "115/05/13", "16:17:14",
                 "公告本公司董事會通過2026年(115)第1季合併財務報告", detail_b],
            ],
        },
    }
    items = parse_response(doc)
    assert len(items) == 2
    sids = {a.source_event_id(it) for it in items}
    assert len(sids) == 2
    assert "mops:6451:2026-05-13:1" in sids
    assert "mops:6451:2026-05-06:1" in sids


def test_source_event_id_falls_back_to_hash_without_seq() -> None:
    a = MopsAnnouncementsAdapter()
    one = {"co_id": "6451", "title": "公告甲", "date": "2026-06-01T10:00:00+08:00", "seq": None}
    two = {"co_id": "6451", "title": "公告乙", "date": "2026-06-01T10:00:00+08:00", "seq": None}
    assert a.source_event_id(one) == a.source_event_id(one)
    assert a.source_event_id(one) != a.source_event_id(two)


# ── fetch: per-month fail-soft ────────────────────────────────────────


async def test_fetch_keeps_second_month_when_first_is_non_json(monkeypatch) -> None:
    """A WAF HTML block page (200 but non-JSON body) on the first month must
    not discard the second month's announcements — each month's request+parse
    is independently fail-soft (same per-URL pattern as hkex_newly_listed)."""
    calls: list[dict] = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(json.loads(req.content))
        if len(calls) == 1:
            return httpx.Response(200, text="<html>FOR SECURITY REASONS…</html>")
        return httpx.Response(200, json=ROWS)

    real_client_cls = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client_cls(*args, **kwargs)

    monkeypatch.setattr(mops_announcements.httpx, "AsyncClient", factory)

    result = await MopsAnnouncementsAdapter().fetch("9917")

    assert len(calls) == 2  # second month still attempted after month-1 garbage
    assert result.http_status == 200  # partial success, not a failed run
    assert len(result.items) == 6  # month 2's rows survive
    assert result.payload["error"]  # month-1 parse failure recorded as last_error


# ── integration: catalyst_sync wiring ─────────────────────────────────

_FRESH_ITEM = {
    # Frozen catalyst clock is 2026-06-04 (conftest) — keep within 21d gate.
    "title": "公告本公司取得AI推論晶片封裝大額訂單",
    "content": "公告本公司取得AI推論晶片封裝大額訂單（公開資訊觀測站重大訊息）",
    "link": None,
    "date": "2026-06-01T16:14:29+08:00",
    "source_label": "公開資訊觀測站",
    "co_id": "6451",
    "enter_date": "2026-06-01",
    "seq": "1",
}


class _MopsStub(MopsAnnouncementsAdapter):
    def __init__(self, items: list[dict] | None = None) -> None:
        super().__init__()
        self.calls: list[str] = []
        self._items = list(items or [])

    async def fetch(self, target: str) -> FetchResult:
        self.calls.append(target)
        return FetchResult(
            source_name=self.source_name,
            schema_name=self.schema_name,
            source_url=self.source_url,
            http_status=200,
            payload={"co_id": target, "row_count": len(self._items)},
            items=self._items,
        )


class _MopsRaisingStub(MopsAnnouncementsAdapter):
    async def fetch(self, target: str) -> FetchResult:
        raise RuntimeError("boom")


class _EmptyEodhdStub(EodhdNewsAdapter):
    def __init__(self) -> None:
        super().__init__(api_key="stub")

    async def fetch(self, target: str) -> FetchResult:
        return FetchResult(
            source_name="eodhd_news",
            schema_name="eodhd.news.v1",
            source_url=f"https://eodhd.test/news?s={target}",
            http_status=200,
            payload=[],
            items=[],
        )


class _FakeClassifier:
    model = "fake-model"
    configured = True

    def __init__(self) -> None:
        self.calls = 0

    async def classify(
        self, *, company_name: str, symbol: str, title: str, content: str
    ) -> ClassificationResult:
        self.calls += 1
        return ClassificationResult(
            "completed",
            {
                "is_company_critical": True,
                "event_subtype": "major_partnership",
                "importance": "high",
                "confidence": 0.9,
                "expected_impact": "New revenue line.",
                "summary": "Large AI packaging order.",
                "why_it_matters": "Material order win.",
                "suggested_action": "research",
                "ignore_reason": None,
            },
            response_id="resp_mops",
        )


async def _seed_tw_company(session: AsyncSession) -> None:
    session.add(
        TrackedCompany(
            symbol="6451", exchange="TWO", company_name="訊芯-KY", source="manual"
        )
    )
    await session.commit()


async def test_mops_sweep_feeds_classifier_and_alerts(
    db_session: AsyncSession, monkeypatch
) -> None:
    from catalyst_radar.config import settings

    monkeypatch.setattr(settings, "catalyst_mops_news_enabled", True)
    await _seed_tw_company(db_session)
    mops = _MopsStub([_FRESH_ITEM])
    clf = _FakeClassifier()

    s = await sync_catalysts(
        db_session,
        news_adapter=_EmptyEodhdStub(),
        classifier=clf,
        mops_adapter=mops,
    )

    assert mops.calls == ["6451"]
    assert clf.calls == 1
    assert s.mops_items == 1
    assert s.autosent == 1

    events = (await db_session.execute(select(Event))).scalars().all()
    assert len(events) == 1
    ev = events[0]
    assert ev.source_name == "mops_announcements"
    assert ev.source_event_id == "mops:6451:2026-06-01:1"
    assert ev.symbol == "6451" and ev.exchange == "TWO"
    notifs = (await db_session.execute(select(Notification))).scalars().all()
    assert len(notifs) == 1

    # Idempotent: same item on the next sweep is a no-op (no second LLM call).
    s2 = await sync_catalysts(
        db_session,
        news_adapter=_EmptyEodhdStub(),
        classifier=clf,
        mops_adapter=_MopsStub([_FRESH_ITEM]),
    )
    assert clf.calls == 1
    assert s2.autosent == 0
    assert len((await db_session.execute(select(Event))).scalars().all()) == 1


async def test_mops_sweep_skipped_when_flag_off(db_session: AsyncSession) -> None:
    # conftest pins catalyst_mops_news_enabled=False for the suite.
    await _seed_tw_company(db_session)
    mops = _MopsStub([_FRESH_ITEM])

    s = await sync_catalysts(
        db_session,
        news_adapter=_EmptyEodhdStub(),
        classifier=_FakeClassifier(),
        mops_adapter=mops,
    )
    assert mops.calls == []
    assert s.mops_items == 0
    assert (await db_session.execute(select(Event))).scalars().all() == []


async def test_mops_sweep_ignores_non_taiwan_companies(
    db_session: AsyncSession, monkeypatch
) -> None:
    from catalyst_radar.config import settings

    monkeypatch.setattr(settings, "catalyst_mops_news_enabled", True)
    db_session.add(
        TrackedCompany(symbol="AAPL", exchange="US", company_name="Apple", source="manual")
    )
    await db_session.commit()
    mops = _MopsStub([_FRESH_ITEM])

    await sync_catalysts(
        db_session,
        news_adapter=_EmptyEodhdStub(),
        classifier=_FakeClassifier(),
        mops_adapter=mops,
    )
    assert mops.calls == []


async def test_mops_fetch_failure_fails_soft(
    db_session: AsyncSession, monkeypatch
) -> None:
    from catalyst_radar.config import settings

    monkeypatch.setattr(settings, "catalyst_mops_news_enabled", True)
    await _seed_tw_company(db_session)

    # A raising adapter must not abort the sync — counted as an error.
    s = await sync_catalysts(
        db_session,
        news_adapter=_EmptyEodhdStub(),
        classifier=_FakeClassifier(),
        mops_adapter=_MopsRaisingStub(),
    )
    assert s.errors == 1
    assert s.mops_items == 0
