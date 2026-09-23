import json
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from catalyst_radar.adapters.base import FetchResult
from catalyst_radar.adapters.eastmoney_news import EastmoneyNewsAdapter
from catalyst_radar.adapters.eodhd_news import EodhdNewsAdapter, to_news_code
from catalyst_radar.adapters.websearch_news import WebSearchNewsAdapter, WebSearchResult
from catalyst_radar.dedup import normalize_url
from catalyst_radar.models.classifier import ClassifierRun
from catalyst_radar.models.company import TrackedCompany
from catalyst_radar.models.event import Event
from catalyst_radar.models.notification import Notification, TelegramChat
from catalyst_radar.services.catalyst_prefilter import prefilter_reason
from catalyst_radar.services.catalyst_sync import sync_catalysts
from catalyst_radar.services.dispatch import deliver_pending_notifications
from catalyst_radar.services.openai_classifier import ClassificationResult
from tests.test_telegram import FakeTelegramClient

NEWS = json.loads((Path(__file__).parent / "fixtures" / "eodhd_news.json").read_text())


def test_prefilter_blocks_noise_keeps_substance() -> None:
    assert prefilter_reason("Company hosts a hackathon", "fun event") is not None
    assert prefilter_reason("Co to attend a career fair", "recruiting") is not None
    assert prefilter_reason("x", "y") == "too_short"
    assert (
        prefilter_reason(
            "Company launches major new product line",
            "A substantial announcement with real business impact and detail.",
        )
        is None
    )


def test_prefilter_blocks_clickbait_headlines() -> None:
    """Yahoo / Insider Monkey / Motley Fool listicle headlines that
    recycle real corporate facts as commentary must die at the
    prefilter — they're the dominant false-positive class on $MU."""
    body = "A long enough substantive article body to clear too_short check."
    clickbait = [
        "Here's Why Acme Memory (ACMR) is Among the 12 High Growth Stocks to Buy",
        "Acme Memory (ACMR) is a Great Momentum Stock: Should You Buy?",
        "Is Acme Memory (ACMR) a Solid Growth Stock? 4 Reasons to Think So",
        "Acme vs. Globex: Which Memory Stock Is the Better Pick?",
        "Prediction: Acme Memory Stock Will Be Worth at Least $900 in 2 Years.",
        "4 Ridiculously Cheap Chip Stocks That Could Double Your Money",
        "Is Acme Destined to Join the Trillion-Dollar Club Next Year?",
        "Acme's 40% Problem: Why a Chip Shortage Is Rewarding Shareholders",
        "Missed Out on the Early Chip Rally? These 3 Names Are Just Warming Up.",
        "Is ACMR Stock Still Undervalued After This Rally?",
    ]
    for title in clickbait:
        r = prefilter_reason(title, body)
        assert r is not None and r.startswith("clickbait_title:"), title


def test_prefilter_keeps_real_news_headlines() -> None:
    """Reuters / press-wire / IR-style headlines must pass the prefilter
    even when adjacent to the same body text."""
    body = "A substantive article body about company operations and financials."
    real = [
        "Acme Memory Announces Q3 Fiscal 2026 Earnings Results",
        "Acme Memory Begins Volume Production of Next-Gen DRAM at Its Ohio Fab",
        "Apple raises full-year guidance after strong iPhone sales",
        "Globex Photonics signs $400M datacenter optical-component contract",
        "Initech Semiconductor files 6-K with the SEC on quarterly results",
    ]
    for title in real:
        assert prefilter_reason(title, body) is None, title


def test_autosend_demoted_for_aggregator_domains() -> None:
    """High-confidence classifications from aggregator/opinion domains
    drop to review (the user still sees them, Telegram doesn't fire)."""
    from catalyst_radar.services.catalyst_sync import _autosend_blocked_by_domain

    demoted = [
        "https://finance.yahoo.com/markets/stocks/articles/why-micron-mu-among-15.html",
        "https://finance.yahoo.com/news/foo-bar.html",
        "https://www.fool.com/investing/2026/05/25/why-micron-is-a-buy/",
        "https://www.insidermonkey.com/blog/why-micron-buy/",
        "https://www.zacks.com/stock/news/2026/05/25/micron-buy/",
    ]
    for u in demoted:
        assert _autosend_blocked_by_domain(u) is not None, u

    passes = [
        "https://www.reuters.com/business/micron-announces-earnings-2026-05-25/",
        "https://investor.micron.com/news-releases/2026/q3-earnings",
        "https://www.sec.gov/Archives/edgar/data/723125/000072312526000123/0.txt",
        "https://www.bloomberg.com/news/articles/2026-05-25/micron-q3",
        "https://www.ft.com/content/abc-def",
    ]
    for u in passes:
        assert _autosend_blocked_by_domain(u) is None, u


def test_to_news_code_maps_us_listings_to_country_suffix() -> None:
    # Listing exchanges from company_reference must collapse onto EODHD's
    # news country code; otherwise /news returns [].
    assert to_news_code("MU", "NASDAQ") == "MU.US"
    assert to_news_code("LITE", "NYSE") == "LITE.US"
    assert to_news_code("X", "AMEX") == "X.US"
    assert to_news_code("Y", "OTCQB") == "Y.US"
    # Already-correct suffixes pass through.
    assert to_news_code("AAPL", "US") == "AAPL.US"
    assert to_news_code("0700", "HK") == "0700.HK"
    assert to_news_code("005930", "KO") == "005930.KO"
    # Unknown markets pass through (best-effort; logged-empty on miss).
    assert to_news_code("6451", "TW") == "6451.TW"
    # Case + whitespace tolerant.
    assert to_news_code("MU", " nasdaq ") == "MU.US"


def test_news_dedup_key_prefers_link() -> None:
    a = EodhdNewsAdapter(api_key="x")
    k1 = a.dedup_key(NEWS[0])
    k2 = a.dedup_key({**NEWS[0], "title": "different headline"})
    assert k1 == k2  # same link => same dedup key
    assert a.source_event_id(NEWS[0]) == a.source_event_id(NEWS[0])


class _NewsStub(EodhdNewsAdapter):
    def __init__(self) -> None:
        super().__init__(api_key="stub")

    async def fetch(self, target: str) -> FetchResult:
        return FetchResult(
            source_name="eodhd_news",
            schema_name="eodhd.news.v1",
            source_url=f"https://eodhd.test/news?s={target}",
            http_status=200,
            payload=NEWS,
            items=NEWS,
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
        t = title.lower()
        if "ai inference chip" in t:
            out = {
                "is_company_critical": True,
                "event_subtype": "product_launch",
                "importance": "high",
                "confidence": 0.9,
                "expected_impact": "New revenue line.",
                "summary": "Apple new AI chip.",
                "why_it_matters": "Expands TAM.",
                "suggested_action": "research",
                "ignore_reason": None,
            }
        elif "cloud partnership" in t:
            out = {
                "is_company_critical": True,
                "event_subtype": "major_partnership",
                "importance": "medium",
                "confidence": 0.7,
                "expected_impact": "Medium-term services revenue.",
                "summary": "Cloud partnership.",
                "why_it_matters": "Strategic.",
                "suggested_action": "watch",
                "ignore_reason": None,
            }
        else:  # market wrap / passing mention
            out = {
                "is_company_critical": False,
                "event_subtype": None,
                "importance": "low",
                "confidence": 0.4,
                "expected_impact": "",
                "summary": "Passing mention.",
                "why_it_matters": "",
                "suggested_action": "ignore",
                "ignore_reason": "casual_mention",
            }
        return ClassificationResult("completed", out, response_id="resp_x")


async def _seed_company(session: AsyncSession) -> None:
    session.add(
        TrackedCompany(symbol="AAPL", exchange="US", company_name="Apple Inc", source="manual")
    )
    await session.commit()


class _EmptyNewsStub(EodhdNewsAdapter):
    """EODHD returns nothing — the case for gap markets like Taiwan."""

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


class _WebSearchStub(WebSearchNewsAdapter):
    def __init__(self) -> None:
        super().__init__(api_key="stub")

    async def fetch(self, *, company_name: str, symbol: str, exchange: str) -> WebSearchResult:
        return WebSearchResult(
            "completed",
            [
                {
                    "title": "Shunsin wins major AI inference chip packaging order",
                    "content": "A substantial new customer win with real revenue impact.",
                    "link": "https://example.tw/shunsin-ai-order",
                    "date": "2026-05-20",
                }
            ],
        )


_SHARED_LINK = "https://news.example/shunsin-ai-inference-chip-order"
_AI_ITEM = {
    "title": "Shunsin wins major AI inference chip packaging order",
    "content": "A substantial new customer win with real revenue impact and detail.",
    "link": _SHARED_LINK,
    "date": "2026-05-20",
}


class _EodhdOneItemStub(EodhdNewsAdapter):
    def __init__(self) -> None:
        super().__init__(api_key="stub")

    async def fetch(self, target: str) -> FetchResult:
        return FetchResult(
            source_name="eodhd_news",
            schema_name="eodhd.news.v1",
            source_url=f"https://eodhd.test/news?s={target}",
            http_status=200,
            payload=[_AI_ITEM],
            items=[_AI_ITEM],
        )


class _WebSearchSameUrlStub(WebSearchNewsAdapter):
    def __init__(self) -> None:
        super().__init__(api_key="stub")

    async def fetch(self, *, company_name: str, symbol: str, exchange: str) -> WebSearchResult:
        # Same canonical URL as EODHD, surfaced a few utm params later.
        return WebSearchResult(
            "completed",
            [{**_AI_ITEM, "link": _SHARED_LINK + "?utm_source=yahoo"}],
        )


async def test_websearch_does_not_double_alert_same_url_as_eodhd(
    db_session: AsyncSession, monkeypatch
) -> None:
    from catalyst_radar.config import settings

    monkeypatch.setattr(settings, "catalyst_websearch_enabled", True)
    # A gap-market company that EODHD *also* happens to surface (same story).
    db_session.add(
        TrackedCompany(
            symbol="6451", exchange="TW", company_name="Shunsin Technology", source="manual"
        )
    )
    await db_session.commit()

    s = await sync_catalysts(
        db_session,
        news_adapter=_EodhdOneItemStub(),
        classifier=_FakeClassifier(),
        websearch_adapter=_WebSearchSameUrlStub(),
    )
    assert s.autosent == 1  # EODHD alert fires
    assert s.deduped == 1  # web_search hit on the same URL is suppressed

    events = (await db_session.execute(select(Event))).scalars().all()
    assert len(events) == 1  # no second event row
    notifs = (await db_session.execute(select(Notification))).scalars().all()
    assert len(notifs) == 1  # exactly one alert
    also = (events[0].payload or {}).get("also_seen_in") or []
    assert any(x.get("source") == "websearch_news" for x in also)  # provenance recorded


_AI_A = {
    "title": "Company unveils new AI inference chip",
    "content": "Substantial product announcement with real business impact and detail.",
    "link": "https://a.example/ai-chip",
    "date": "2026-05-20",
}
_AI_B = {
    "title": "Firm launches AI inference chip platform",
    "content": "Substantial product announcement with real business impact and detail.",
    "link": "https://b.example/ai-chip",  # different URL, same story
    "date": "2026-05-20",
}


class _TwoAiItemsStub(EodhdNewsAdapter):
    def __init__(self) -> None:
        super().__init__(api_key="stub")

    async def fetch(self, target: str) -> FetchResult:
        return FetchResult(
            source_name="eodhd_news",
            schema_name="eodhd.news.v1",
            source_url=f"https://eodhd.test/news?s={target}",
            http_status=200,
            payload=[_AI_A, _AI_B],
            items=[_AI_A, _AI_B],
        )


class _JudgeClassifier(_FakeClassifier):
    def __init__(self, result: bool) -> None:
        super().__init__()
        self.judge_result = result
        self.judge_calls = 0

    async def same_event(
        self, *, a_title: str, a_summary: str, b_title: str, b_summary: str
    ) -> bool:
        self.judge_calls += 1
        return self.judge_result


def test_similarity_uses_best_of_title_and_summary() -> None:
    from catalyst_radar.services.catalyst_sync import _similarity

    cand = Event(
        event_type="catalyst",
        source_name="eodhd_news",
        source_event_id="x",
        dedup_key="x",
        symbol="AAPL",
        title="Apple unveils new AI processor",
        payload={
            "classification": {
                "summary": "Apple announced a new AI chip.",
                "event_subtype": "product_launch",
            }
        },
    )
    # Paraphrased headline, but the classifier summaries align → high score.
    assert _similarity("Apple launches AI chip", "Apple announced a new AI chip.", cand) >= 88


async def test_semantic_merge_same_story_different_url(db_session: AsyncSession) -> None:
    await _seed_company(db_session)  # AAPL US, web_search off
    s = await sync_catalysts(
        db_session, news_adapter=_TwoAiItemsStub(), classifier=_FakeClassifier()
    )
    assert s.autosent == 1  # first item alerts
    assert s.deduped == 1  # second (same story, different URL) merged

    events = (await db_session.execute(select(Event))).scalars().all()
    assert len(events) == 1
    notifs = (await db_session.execute(select(Notification))).scalars().all()
    assert len(notifs) == 1
    also = (events[0].payload or {}).get("also_seen_in") or []
    assert also and also[0]["via"] == "semantic"


async def test_grey_zone_judge_merges_when_yes(db_session: AsyncSession, monkeypatch) -> None:
    from catalyst_radar.config import settings

    # Force the grey zone: nothing auto-merges, everything same-subtype asks the judge.
    monkeypatch.setattr(settings, "catalyst_dedup_title_threshold", 101)
    monkeypatch.setattr(settings, "catalyst_dedup_grey_threshold", 1)
    await _seed_company(db_session)
    clf = _JudgeClassifier(result=True)

    s = await sync_catalysts(db_session, news_adapter=_TwoAiItemsStub(), classifier=clf)
    assert clf.judge_calls == 1
    assert s.deduped == 1
    assert len((await db_session.execute(select(Event))).scalars().all()) == 1


class _SubtypeDriftClassifier(_FakeClassifier):
    """Returns *different* event_subtypes for two articles about the same story.

    Mimics the real-world failure mode where the JSON-prompted classifier
    labels two articles about the same catalyst as e.g. ``manufacturing_expansion``
    vs ``product_launch`` (or even ``manufacturing expansion`` with a literal
    space). Strongly similar title/summary → still the same story."""

    def __init__(self) -> None:
        super().__init__()
        self._subtypes = ["manufacturing_expansion", "product_launch"]

    async def classify(
        self, *, company_name: str, symbol: str, title: str, content: str
    ) -> ClassificationResult:
        self.calls += 1
        # Rotate through the configured subtypes, but otherwise return a
        # company-critical, high-confidence product-launch-shaped payload.
        sub = self._subtypes[(self.calls - 1) % len(self._subtypes)]
        return ClassificationResult(
            "completed",
            {
                "is_company_critical": True,
                "event_subtype": sub,
                "importance": "high",
                "confidence": 0.9,
                "expected_impact": "New revenue line.",
                "summary": "Substantial product announcement with real business impact.",
                "why_it_matters": "Expands TAM.",
                "suggested_action": "research",
                "ignore_reason": None,
            },
            response_id="resp_x",
        )


async def test_semantic_merge_ignores_subtype_mismatch(db_session: AsyncSession) -> None:
    # Two articles about the same story for the same company can come back from
    # the JSON-prompted classifier with *different* event_subtypes. Title +
    # summary similarity must still merge them — subtype is signal, not gate.
    await _seed_company(db_session)  # AAPL US
    clf = _SubtypeDriftClassifier()
    s = await sync_catalysts(db_session, news_adapter=_TwoAiItemsStub(), classifier=clf)

    assert clf.calls == 2  # both items reach the classifier
    assert s.autosent == 1  # first item alerts
    assert s.deduped == 1  # second merged despite subtype mismatch
    events = (await db_session.execute(select(Event))).scalars().all()
    assert len(events) == 1
    notifs = (await db_session.execute(select(Notification))).scalars().all()
    assert len(notifs) == 1
    also = (events[0].payload or {}).get("also_seen_in") or []
    assert also and also[0]["via"] == "semantic"


class _CnIpoClusterWebSearchStub(WebSearchNewsAdapter):
    """Two CN-press-style hits about the same CXMT/Unitree-class IPO
    approval, surfaced via web_search (the only news path that runs for
    pre-IPO CN tickers). Distinct URLs, completely different surface
    text — fuzz on titles/summaries scores well under the standard grey
    bar. The dedup must still merge via the subtype-fallback path."""

    _ITEMS = [
        {
            "title": "星河存储科创板首发申请获上市委审议通过",
            "content": "上市委会议审议通过星河存储的科创板首发申请，拟募资规模居前。",
            "link": "https://sse.example/cn/a-approval",
            "date": "2026-05-27",
        },
        {
            "title": "Memory maker wins listing-committee nod for large STAR Market IPO",
            "content": "The chipmaker passed the Shanghai STAR Market review, per local reports.",
            "link": "https://en.example/cxmt-star-cleared",
            "date": "2026-05-28",
        },
    ]

    def __init__(self) -> None:
        super().__init__(api_key="stub")

    async def fetch(
        self, *, company_name: str, symbol: str, exchange: str
    ) -> WebSearchResult:
        return WebSearchResult("completed", list(self._ITEMS))


class _UplistingJudgeClassifier(_FakeClassifier):
    """Classifies every item as high-importance ``uplisting`` with a
    DIFFERENT English-normalized summary per call (rotates through a
    short pool of real-shape paraphrases), and answers the same_event
    judge True. The differing summaries are essential: identical
    summaries would let fuzz alone clear the grey bar at score 100 and
    the test wouldn't actually exercise the subtype-fallback path.

    No story_key is emitted, so the story_key fast-path doesn't fire —
    this test focuses on the fuzz + subtype-fallback layers."""

    # Each paraphrase is plausibly what the real LLM normalizes the
    # same event into when reading different outlets. They share the
    # `$<symbol>` prefix and topic words but vary enough that pairwise
    # token_set_ratio lands under 55 (verified by the script that
    # diagnosed the original CXMT cluster).
    _SUMMARIES = [
        "${0} cleared a key review hurdle for its Shanghai listing.",
        "${0}'s domestic IPO application has been approved by the exchange.",
        "Memory-chip maker ${0} won committee approval and is set to raise capital.",
    ]

    def __init__(self, judge_result: bool = True) -> None:
        super().__init__()
        self.judge_calls = 0
        self.judge_targets: list[str] = []
        self.judge_result = judge_result

    async def classify(
        self, *, company_name: str, symbol: str, title: str, content: str
    ) -> ClassificationResult:
        self.calls += 1
        summary = self._SUMMARIES[(self.calls - 1) % len(self._SUMMARIES)].replace(
            "${0}", f"${symbol}"
        )
        return ClassificationResult(
            "completed",
            {
                "is_company_critical": True,
                "event_subtype": "uplisting",
                "importance": "high",
                "confidence": 0.95,
                "expected_impact": "Listing pipeline clears.",
                "summary": summary,
                "why_it_matters": "Material listing milestone.",
                "suggested_action": "research",
                "ignore_reason": "",
            },
            response_id="resp_x",
        )

    async def same_event(
        self, *, a_title: str, a_summary: str, b_title: str, b_summary: str
    ) -> bool:
        self.judge_calls += 1
        self.judge_targets.append(b_title)
        return self.judge_result


class _StoryKeyClassifier(_FakeClassifier):
    """Returns matching ``story_key`` for both articles in the cluster
    but DIFFERENT event_subtypes and paraphrased English summaries.
    Designed so neither fuzz (summaries score <55) nor subtype-fallback
    (subtypes disagree) can save the merge — only the story_key
    fast-path can. Tracks whether the LLM judge was ever asked."""

    _SUBTYPES = ["uplisting", "financing"]
    _SUMMARIES = [
        "${0} cleared a key review hurdle for its Shanghai listing.",
        "Memory-chip maker ${0} won committee approval and is set to raise capital.",
    ]

    def __init__(self, story_key: str = "a25310_star_ipo_approval_2026-05-28") -> None:
        super().__init__()
        self.story_key = story_key
        self.judge_calls = 0

    async def classify(
        self, *, company_name: str, symbol: str, title: str, content: str
    ) -> ClassificationResult:
        self.calls += 1
        idx = (self.calls - 1) % len(self._SUBTYPES)
        return ClassificationResult(
            "completed",
            {
                "is_company_critical": True,
                "event_subtype": self._SUBTYPES[idx],
                "importance": "high",
                "confidence": 0.95,
                "expected_impact": "Listing milestone.",
                "summary": self._SUMMARIES[idx].replace("${0}", f"${symbol}"),
                "why_it_matters": "Material.",
                "suggested_action": "research",
                "ignore_reason": "",
                "story_key": self.story_key,
            },
            response_id="resp_x",
        )

    async def same_event(
        self, *, a_title: str, a_summary: str, b_title: str, b_summary: str
    ) -> bool:
        self.judge_calls += 1
        return False  # judge would refuse — fast-path must run first


async def test_story_key_fastpath_merges_without_judge(
    db_session: AsyncSession, monkeypatch
) -> None:
    """The story_key path catches duplicates that NEITHER fuzz NOR
    subtype-fallback can save: cross-language titles, paraphrased
    summaries (fuzz <55), AND classifier-disagreed event_subtypes
    (subtype-fallback skips). Only the LLM-emitted canonical event
    identifier collapses them. No judge call should fire — exact-match
    is deterministic."""
    from catalyst_radar.config import settings

    monkeypatch.setattr(settings, "catalyst_dedup_title_threshold", 101)
    monkeypatch.setattr(settings, "catalyst_dedup_grey_threshold", 55)
    monkeypatch.setattr(settings, "catalyst_websearch_enabled", True)
    monkeypatch.setattr(
        settings, "catalyst_websearch_gap_exchanges", "SSE,SZSE,TW"
    )
    db_session.add(
        TrackedCompany(
            symbol="A25310",
            exchange="SSE",
            country="CN",
            company_name="长鑫科技",
            source="manual",
        )
    )
    await db_session.commit()
    clf = _StoryKeyClassifier()

    s = await sync_catalysts(
        db_session,
        news_adapter=_EmptyNewsStub(),
        classifier=clf,
        websearch_adapter=_CnIpoClusterWebSearchStub(),
    )
    assert clf.calls == 2
    assert clf.judge_calls == 0  # fast-path bypasses the judge entirely
    assert s.deduped == 1
    events = (await db_session.execute(select(Event))).scalars().all()
    assert len(events) == 1
    notifs = (await db_session.execute(select(Notification))).scalars().all()
    assert len(notifs) == 1
    # Provenance of the second source is recorded on the first event.
    also = (events[0].payload or {}).get("also_seen_in") or []
    assert also and also[0]["via"] == "semantic"


class _DateDriftStoryKeyClassifier(_FakeClassifier):
    """First call emits a v2-shaped dated story_key
    (`..._2026-05-27`); second call emits the v3 date-less form
    (`...`) — same underlying event, two outlets, different dates.
    The server-side normalizer must strip the trailing date and
    collapse them. Disagreed subtypes + fuzz <55 summaries mean ONLY
    the (normalized) story_key fast-path can save this merge."""

    _SUBTYPES = ["uplisting", "financing"]
    _SUMMARIES = [
        "${0} cleared a key review hurdle for its Shanghai listing.",
        "Memory-chip maker ${0} won committee approval and is set to raise capital.",
    ]
    _KEYS = [
        "a25310_ipo_review_pass_2026-05-27",  # legacy v2 — dated
        "a25310_ipo_review_pass",              # new v3 — bare
    ]

    def __init__(self) -> None:
        super().__init__()
        self.judge_calls = 0

    async def classify(
        self, *, company_name: str, symbol: str, title: str, content: str
    ) -> ClassificationResult:
        self.calls += 1
        idx = (self.calls - 1) % len(self._KEYS)
        return ClassificationResult(
            "completed",
            {
                "is_company_critical": True,
                "event_subtype": self._SUBTYPES[idx],
                "importance": "high",
                "confidence": 0.95,
                "expected_impact": "Listing milestone.",
                "summary": self._SUMMARIES[idx].replace("${0}", f"${symbol}"),
                "why_it_matters": "Material.",
                "suggested_action": "research",
                "ignore_reason": "",
                "story_key": self._KEYS[idx],
            },
            response_id="resp_x",
        )

    async def same_event(
        self, *, a_title: str, a_summary: str, b_title: str, b_summary: str
    ) -> bool:
        self.judge_calls += 1
        return False


class _TwoDistinctEventsSameSlugStub(WebSearchNewsAdapter):
    """Two articles about distinct events but with the same canonical
    counterparty — e.g. a hypothetical pair of NVIDIA × A26029
    partnerships 14 days apart. They share story_key
    ``a26029_partnership_nvda`` but have event_dates well outside the
    proximity gate. The fast-path MUST NOT collapse them; both should
    alert."""

    _ITEMS = [
        {
            "title": "NVIDIA × Unitree announce Isaac GR00T reference robot",
            "content": (
                "NVIDIA today named Unitree as the reference "
                "humanoid robot partner for Isaac GR00T."
            ),
            "link": "https://nvidia.example/groot-launch",
            "date": "2026-05-15",
        },
        {
            "title": "NVIDIA leads $200M investment in Unitree Robotics",
            "content": "NVIDIA is reported to have led a $200M Series-C round in Unitree Robotics.",
            "link": "https://reuters.example/unitree-funding",
            "date": "2026-05-29",
        },
    ]

    def __init__(self) -> None:
        super().__init__(api_key="stub")

    async def fetch(
        self, *, company_name: str, symbol: str, exchange: str
    ) -> WebSearchResult:
        return WebSearchResult("completed", list(self._ITEMS))


class _PartnershipSlugClassifier(_FakeClassifier):
    """Always emits subtype=partnership + story_key=a26029_partnership_nvda
    — modelling the v3 canonical slug for any NVIDIA × Unitree deal.
    The proximity gate (not the slug) is what must distinguish
    distinct-events-same-counterparty."""

    async def classify(
        self, *, company_name: str, symbol: str, title: str, content: str
    ) -> ClassificationResult:
        self.calls += 1
        return ClassificationResult(
            "completed",
            {
                "is_company_critical": True,
                "event_subtype": "partnership",
                "importance": "high",
                "confidence": 0.95,
                "expected_impact": "Partnership deepens.",
                "summary": f"${symbol} and NVIDIA expand their partnership.",
                "why_it_matters": "Material strategic relationship.",
                "suggested_action": "research",
                "ignore_reason": "",
                "story_key": "a26029_partnership_nvda",
            },
            response_id="resp_x",
        )

    async def same_event(
        self, *, a_title: str, a_summary: str, b_title: str, b_summary: str
    ) -> bool:
        # If the proximity gate fails and code falls through to grey/judge,
        # the judge correctly says "different events" — but the gate alone
        # should make the merge fail FIRST. This stub asserts we never get
        # here (we count calls in the test).
        return False


async def test_story_key_proximity_guard_separates_distinct_events(
    db_session: AsyncSession, monkeypatch
) -> None:
    """Two partnership announcements 14 days apart, same canonical slug
    (`a26029_partnership_nvda`), must NOT collapse — the event-date
    proximity gate on the story_key fast-path treats them as distinct
    events. Regression for Layer-2 (over-collapse of follow-on
    deals with the same counterparty)."""
    from catalyst_radar.config import settings

    monkeypatch.setattr(settings, "catalyst_dedup_title_threshold", 101)
    monkeypatch.setattr(settings, "catalyst_dedup_grey_threshold", 101)
    monkeypatch.setattr(settings, "catalyst_story_key_max_date_gap_days", 7)
    monkeypatch.setattr(settings, "catalyst_websearch_enabled", True)
    monkeypatch.setattr(
        settings, "catalyst_websearch_gap_exchanges", "SSE,SZSE,TW"
    )
    db_session.add(
        TrackedCompany(
            symbol="A26029",
            exchange="SSE",
            country="CN",
            company_name="宇树科技",
            source="manual",
        )
    )
    await db_session.commit()
    clf = _PartnershipSlugClassifier()

    s = await sync_catalysts(
        db_session,
        news_adapter=_EmptyNewsStub(),
        classifier=clf,
        websearch_adapter=_TwoDistinctEventsSameSlugStub(),
    )
    assert clf.calls == 2
    # Proximity gate fails → no merge → both items become events + alerts.
    assert s.deduped == 0
    events = (await db_session.execute(select(Event))).scalars().all()
    assert len(events) == 2
    notifs = (await db_session.execute(select(Notification))).scalars().all()
    assert len(notifs) == 2


async def test_story_key_dates_are_stripped_for_dedup(
    db_session: AsyncSession, monkeypatch
) -> None:
    """v3 rollout safety: a v2 event in DB with `..._2026-05-27` MUST
    still dedup against a fresh v3 event with the bare slug. The
    server-side regex normalizer (catalyst_sync._canonical_story_key)
    strips trailing `_YYYY-MM-DD` so cross-version keys collapse.
    Regression for."""
    from catalyst_radar.config import settings

    monkeypatch.setattr(settings, "catalyst_dedup_title_threshold", 101)
    monkeypatch.setattr(settings, "catalyst_dedup_grey_threshold", 55)
    monkeypatch.setattr(settings, "catalyst_websearch_enabled", True)
    monkeypatch.setattr(
        settings, "catalyst_websearch_gap_exchanges", "SSE,SZSE,TW"
    )
    db_session.add(
        TrackedCompany(
            symbol="A25310",
            exchange="SSE",
            country="CN",
            company_name="长鑫科技",
            source="manual",
        )
    )
    await db_session.commit()
    clf = _DateDriftStoryKeyClassifier()

    s = await sync_catalysts(
        db_session,
        news_adapter=_EmptyNewsStub(),
        classifier=clf,
        websearch_adapter=_CnIpoClusterWebSearchStub(),
    )
    assert clf.calls == 2
    assert clf.judge_calls == 0  # date-stripping is deterministic — no judge
    assert s.deduped == 1
    events = (await db_session.execute(select(Event))).scalars().all()
    assert len(events) == 1
    notifs = (await db_session.execute(select(Notification))).scalars().all()
    assert len(notifs) == 1


async def test_story_key_fastpath_merges_beyond_fuzzy_window(
    db_session: AsyncSession,
) -> None:
    """A late straggler from a slow source (web_search rediscovering a
    days-old story) carries the SAME canonical story_key as an event
    ingested >72h earlier. The fuzzy/judge candidate scan is bounded to
    the 72h dedup window, but the deterministic story_key fast-path
    scans a wider horizon (>= story_key_max_date_gap_days) so the
    duplicate still merges instead of minting a second event.

    Regression: two identical-headline MU board-appointment catalysts
    (eodhd ingested 2026-06-10, web_search rediscovered 2026-06-13 —
    ~74h later) both sat in the review queue because the original had
    aged just past the 72h window, hiding it from every dedup layer."""
    from datetime import UTC, datetime, timedelta

    from catalyst_radar.models.event import Event
    from catalyst_radar.repositories.event_repository import EventRepository
    from catalyst_radar.services.catalyst_sync import _Dedup, _find_semantic_twin

    now = datetime(2026, 6, 13, 4, 0, tzinfo=UTC)
    story_key = "mu_board_appointment_alexis_black_bjorlin"
    headline = "Micron Appoints Alexis Black Björlin to Board of Directors"
    # Prior event ingested 74h earlier — just past the 72h fuzzy window,
    # so it is invisible to the fuzz/judge layers (which only scan 72h).
    prior = Event(
        source_name="eodhd_news",
        source_event_id="eodhd:mu:board:1",
        dedup_key="mu-board-1",
        event_type="catalyst",
        symbol="MU",
        exchange="US",
        title=headline,
        event_date=datetime(2026, 6, 9, tzinfo=UTC),
        status="review",
        payload={
            "classification": {
                "story_key": story_key,
                "event_subtype": "management_change",
                "summary": "Micron names Alexis Black Björlin to its board.",
            }
        },
        created_at=now - timedelta(hours=74),
    )
    db_session.add(prior)
    await db_session.commit()

    dedup = _Dedup(
        window_hours=72,
        title_threshold=88,
        grey_threshold=55,
        judge_enabled=True,
        story_key_max_date_gap_days=7,
    )
    twin = await _find_semantic_twin(
        EventRepository(db_session),
        _FakeClassifier(),
        dedup,
        symbol="MU",
        exchange="US",
        subtype="management_change",
        title=headline,
        summary="Micron appoints Alexis Black Björlin to its board.",
        story_key=story_key,
        event_date=datetime(2026, 6, 9, tzinfo=UTC),
        now=now,
    )
    # Same canonical story + event_dates 0 days apart → must merge onto
    # the prior event even though it aged past the fuzzy window.
    assert twin is not None
    assert twin.id == prior.id


async def test_subtype_fallback_judges_when_fuzz_misses_cross_language(
    db_session: AsyncSession, monkeypatch
) -> None:
    """The CXMT/Unitree CN-press duplicates that fuzz scored <55 must
    still get dedup'd: when no candidate clears the grey bar but there
    is a same-(symbol, subtype) candidate in window, the judge fires
    against that candidate. Regression for the 9h-of-dupes incident."""
    from catalyst_radar.config import settings

    # Keep the title bar high (no auto-merge); keep grey at production
    # default. Fuzz on these two unrelated-looking headlines should land
    # well under 55 — only the subtype fallback can save the merge.
    monkeypatch.setattr(settings, "catalyst_dedup_title_threshold", 101)
    monkeypatch.setattr(settings, "catalyst_dedup_grey_threshold", 55)
    monkeypatch.setattr(settings, "catalyst_websearch_enabled", True)
    monkeypatch.setattr(
        settings, "catalyst_websearch_gap_exchanges", "SSE,SZSE,TW"
    )
    db_session.add(
        TrackedCompany(
            symbol="A25310",
            exchange="SSE",
            country="CN",
            company_name="长鑫科技",
            source="manual",
        )
    )
    await db_session.commit()
    clf = _UplistingJudgeClassifier(judge_result=True)

    s = await sync_catalysts(
        db_session,
        news_adapter=_EmptyNewsStub(),
        classifier=clf,
        websearch_adapter=_CnIpoClusterWebSearchStub(),
    )
    assert clf.calls == 2
    assert clf.judge_calls == 1  # only the 2nd item triggers a judge
    assert s.deduped == 1
    events = (await db_session.execute(select(Event))).scalars().all()
    assert len(events) == 1  # second story merged into the first
    notifs = (await db_session.execute(select(Notification))).scalars().all()
    assert len(notifs) == 1


class _ThreeItemAscendingScoreStub(EodhdNewsAdapter):
    """Three items where the 3rd is most-similar to the 1st by fuzz, but
    arrives after the 2nd. The dedup must judge against the BEST candidate
    (the 1st), not whichever happens to be visited first."""

    _A = {
        "title": "Acme Corp launches new AI inference chip product line",
        "content": "Substantial product announcement with real business impact and detail.",
        "link": "https://x.example/1",
        "date": "2026-05-20",
    }
    _B = {
        "title": "Acme partners with hyperscaler on cloud services renewal",
        "content": "Substantial partnership announcement with multi-year revenue tail.",
        "link": "https://x.example/2",
        "date": "2026-05-20",
    }
    # ~identical text to _A → must score much higher than _B against _A
    _C = {
        "title": "Acme Corp launches new AI inference chip product platform",
        "content": "Substantial product announcement with real business impact and detail.",
        "link": "https://x.example/3",
        "date": "2026-05-20",
    }

    def __init__(self) -> None:
        super().__init__(api_key="stub")

    async def fetch(self, target: str) -> FetchResult:
        items = [self._A, self._B, self._C]
        return FetchResult(
            source_name="eodhd_news",
            schema_name="eodhd.news.v1",
            source_url=f"https://eodhd.test/news?s={target}",
            http_status=200,
            payload=items,
            items=items,
        )


class _RecordingJudgeClassifier(_FakeClassifier):
    """Returns same_event=True only when the judge is asked about the
    intended target (item A's summary). Lets us assert the judge ran
    against the highest-scoring candidate, not an arbitrary earlier one."""

    def __init__(self) -> None:
        super().__init__()
        self.judge_targets: list[str] = []

    async def classify(
        self, *, company_name: str, symbol: str, title: str, content: str
    ) -> ClassificationResult:
        self.calls += 1
        # Always company-critical so every item reaches the dedup path.
        return ClassificationResult(
            "completed",
            {
                "is_company_critical": True,
                "event_subtype": "product_launch",
                "importance": "high",
                "confidence": 0.95,
                "expected_impact": "New product line.",
                # Title-derived summary so each item has its own fingerprint.
                "summary": title,
                "why_it_matters": "Material.",
                "suggested_action": "research",
                "ignore_reason": None,
            },
            response_id="resp_x",
        )

    async def same_event(
        self, *, a_title: str, a_summary: str, b_title: str, b_summary: str
    ) -> bool:
        self.judge_targets.append(b_title)
        return True


async def test_highest_scoring_grey_candidate_wins_judge(
    db_session: AsyncSession, monkeypatch
) -> None:
    """When multiple in-window events sit in the grey zone, the judge
    must be asked about the candidate with the HIGHEST fuzz score. The
    old code judged the first match it found, which silently dropped
    merge recall once the grey bar was loose enough to admit several
    candidates per item."""
    from catalyst_radar.config import settings

    monkeypatch.setattr(settings, "catalyst_dedup_title_threshold", 101)
    monkeypatch.setattr(settings, "catalyst_dedup_grey_threshold", 55)
    await _seed_company(db_session)
    clf = _RecordingJudgeClassifier()

    await sync_catalysts(
        db_session, news_adapter=_ThreeItemAscendingScoreStub(), classifier=clf
    )
    # The 3rd item's judge should target the 1st (its near-twin), not the
    # 2nd (the partnership story, which would score lower).
    assert clf.judge_targets, "judge should have fired for the 3rd item"
    assert any("AI inference chip product line" in t for t in clf.judge_targets), (
        f"judge targeted wrong candidate: {clf.judge_targets!r}"
    )


async def test_grey_zone_judge_keeps_separate_when_no(
    db_session: AsyncSession, monkeypatch
) -> None:
    from catalyst_radar.config import settings

    monkeypatch.setattr(settings, "catalyst_dedup_title_threshold", 101)
    monkeypatch.setattr(settings, "catalyst_dedup_grey_threshold", 1)
    await _seed_company(db_session)
    clf = _JudgeClassifier(result=False)

    s = await sync_catalysts(db_session, news_adapter=_TwoAiItemsStub(), classifier=clf)
    assert clf.judge_calls == 1
    assert s.deduped == 0
    assert len((await db_session.execute(select(Event))).scalars().all()) == 2
    assert len((await db_session.execute(select(Notification))).scalars().all()) == 2


class _AiItemPerTargetStub(EodhdNewsAdapter):
    """Returns one AI-chip item whose link is unique per company (target)."""

    def __init__(self) -> None:
        super().__init__(api_key="stub")

    async def fetch(self, target: str) -> FetchResult:
        item = {**_AI_A, "link": f"https://x.example/{target}"}
        return FetchResult(
            source_name="eodhd_news",
            schema_name="eodhd.news.v1",
            source_url=f"https://eodhd.test/news?s={target}",
            http_status=200,
            payload=[item],
            items=[item],
        )


async def test_same_url_different_companies_both_alert(
    db_session: AsyncSession, monkeypatch
) -> None:
    # Two different tickers surfaced with the SAME article URL (via web_search)
    # must NOT collide on the dedup key — it includes symbol+exchange.
    from catalyst_radar.config import settings

    monkeypatch.setattr(settings, "catalyst_websearch_enabled", True)
    db_session.add(
        TrackedCompany(symbol="AAA", exchange="US", company_name="Aaa", source="manual")
    )
    db_session.add(
        TrackedCompany(symbol="BBB", exchange="US", company_name="Bbb", source="manual")
    )
    await db_session.commit()

    # EODHD surfaces the shared URL (AAA gets it; BBB's EODHD copy collides on the
    # link-based source_event_id), and web_search surfaces the same URL for BBB.
    # The per-company dedup key must let BBB alert instead of merging into AAA.
    await sync_catalysts(
        db_session,
        news_adapter=_EodhdOneItemStub(),
        classifier=_FakeClassifier(),
        websearch_adapter=_WebSearchSameUrlStub(),
    )
    events = (await db_session.execute(select(Event))).scalars().all()
    assert {e.symbol for e in events} == {"AAA", "BBB"}
    assert len((await db_session.execute(select(Notification))).scalars().all()) == 2


async def test_same_symbol_two_exchanges_not_merged(db_session: AsyncSession) -> None:
    # Same ticker on two exchanges is two distinct companies; the semantic twin
    # search is scoped by exchange, so similar stories must not cross-merge.
    db_session.add(
        TrackedCompany(symbol="SYM", exchange="US", company_name="Sym US", source="manual")
    )
    db_session.add(
        TrackedCompany(symbol="SYM", exchange="HK", company_name="Sym HK", source="manual")
    )
    await db_session.commit()

    s = await sync_catalysts(
        db_session, news_adapter=_AiItemPerTargetStub(), classifier=_FakeClassifier()
    )
    assert s.deduped == 0
    assert len((await db_session.execute(select(Event))).scalars().all()) == 2


async def test_pre_ipo_skipped_from_eodhd_routed_to_websearch(
    db_session: AsyncSession, monkeypatch
) -> None:
    """A tracked pre-IPO company (CSRC reservation code, no listing
    ticker yet) must NOT hit EODHD news — the news endpoint 404s on
    A-prefix codes — and MUST hit web_search news, which works off the
    company name. Regression for the CXMT/Unitree tracking flow."""
    from catalyst_radar.config import settings

    monkeypatch.setattr(settings, "catalyst_websearch_enabled", True)
    monkeypatch.setattr(settings, "catalyst_websearch_all_markets", False)
    monkeypatch.setattr(
        settings, "catalyst_websearch_gap_exchanges", "TW,SSE,SZSE"
    )

    db_session.add(
        TrackedCompany(
            symbol="A25310",
            exchange="SSE",
            country="CN",
            company_name="长鑫科技",
            source="manual",
        )
    )
    await db_session.commit()

    eodhd_calls: list[str] = []
    websearch_calls: list[str] = []

    class _RecordingEodhd(EodhdNewsAdapter):
        def __init__(self) -> None:
            super().__init__(api_key="stub")

        async def fetch(self, target: str) -> FetchResult:
            eodhd_calls.append(target)
            return FetchResult(
                source_name="eodhd_news",
                schema_name="eodhd.news.v1",
                source_url="https://eodhd.test",
                http_status=200,
                payload=[],
                items=[],
            )

    class _RecordingWebSearch(WebSearchNewsAdapter):
        def __init__(self) -> None:
            super().__init__(api_key="stub")

        async def fetch(
            self, *, company_name: str, symbol: str, exchange: str
        ) -> WebSearchResult:
            websearch_calls.append(symbol)
            return WebSearchResult("completed", [])

    await sync_catalysts(
        db_session,
        news_adapter=_RecordingEodhd(),
        classifier=_FakeClassifier(),
        websearch_adapter=_RecordingWebSearch(),
    )

    # EODHD was never asked about A25310.
    assert "A25310.SSE" not in eodhd_calls
    assert eodhd_calls == []
    # web_search WAS asked about it (SSE is in gap_exchanges).
    assert websearch_calls == ["A25310"]


async def test_websearch_catalysts_only_for_gap_markets(
    db_session: AsyncSession, monkeypatch
) -> None:
    from catalyst_radar.config import settings

    monkeypatch.setattr(settings, "catalyst_websearch_enabled", True)
    monkeypatch.setattr(settings, "catalyst_websearch_all_markets", False)
    # US name (EODHD-covered) must be skipped; TW name (gap) must be swept.
    db_session.add(
        TrackedCompany(symbol="AAPL", exchange="US", company_name="Apple Inc", source="manual")
    )
    db_session.add(
        TrackedCompany(
            symbol="6451", exchange="TW", company_name="Shunsin Technology", source="manual"
        )
    )
    await db_session.commit()

    s = await sync_catalysts(
        db_session,
        news_adapter=_EmptyNewsStub(),
        classifier=_FakeClassifier(),
        websearch_adapter=_WebSearchStub(),
    )
    # Only the Taiwan company yields a web_search item; EODHD is empty for both.
    assert s.fetched == 1
    assert s.classified == 1
    assert s.autosent == 1

    events = (await db_session.execute(select(Event))).scalars().all()
    assert [e.source_name for e in events] == ["websearch_news"]
    assert events[0].symbol == "6451"

    # Idempotent across runs.
    s2 = await sync_catalysts(
        db_session,
        news_adapter=_EmptyNewsStub(),
        classifier=_FakeClassifier(),
        websearch_adapter=_WebSearchStub(),
    )
    assert s2.classified == 0


async def test_catalyst_pipeline_hybrid_and_idempotent(
    db_session: AsyncSession,
) -> None:
    await _seed_company(db_session)
    clf = _FakeClassifier()

    s1 = await sync_catalysts(db_session, news_adapter=_NewsStub(), classifier=clf)
    assert s1.fetched == 4
    assert s1.prefiltered == 1  # "to attend" career fair
    assert s1.classified == 3  # the other three reach the LLM
    assert s1.autosent == 1  # high + 0.9 -> auto-send
    assert s1.review == 1  # medium + 0.7 -> review queue
    assert s1.skipped == 1  # not company-critical
    assert clf.calls == 3

    runs = (await db_session.execute(select(func.count()).select_from(ClassifierRun))).scalar_one()
    assert runs == 3  # one persisted classifier run per LLM call

    notifs = (await db_session.execute(select(Notification))).scalars().all()
    by_status = sorted(n.status for n in notifs)
    assert by_status == ["pending", "review"]  # 1 auto-send, 1 queued

    # Idempotent: re-run reuses source_event_ids, no new work.
    s2 = await sync_catalysts(db_session, news_adapter=_NewsStub(), classifier=_FakeClassifier())
    assert s2.classified == 0
    assert s2.catalysts == 0
    events = (await db_session.execute(select(func.count()).select_from(Event))).scalar_one()
    assert events == 4  # 2 catalysts + 1 prefiltered-ignored + 1 skipped-ignored


async def test_catalyst_autosend_only_delivers_pending(
    db_session: AsyncSession,
) -> None:
    await _seed_company(db_session)
    db_session.add(TelegramChat(chat_id="42", is_active=True))
    await db_session.commit()

    await sync_catalysts(db_session, news_adapter=_NewsStub(), classifier=_FakeClassifier())
    client = FakeTelegramClient()
    d = await deliver_pending_notifications(db_session, client=client)
    assert d.sent == 1  # only the auto-send catalyst goes out
    assert len(client.sent) == 1
    assert "AAPL" in client.sent[0][1]  # the auto-send catalyst card


class _StaleNewsStub(EodhdNewsAdapter):
    """One stale article (7 months old) and one fresh one. The pipeline
    must classify only the fresh one and drop the stale one BEFORE any
    LLM call (saves cost + prevents the user from being alerted about
    last year's events)."""

    STALE = {
        "title": "Shunsin Vietnam subsidiary factory construction",
        "content": "A substantial business development with real impact and detail.",
        "link": "https://news.example/shunsin-vietnam-factory",
        "date": "2025-10-30",
    }
    FRESH = {
        "title": "Shunsin wins major AI inference chip packaging order",
        "content": "A substantial new customer win with real revenue impact and detail.",
        "link": "https://news.example/shunsin-ai-order-fresh",
        "date": "2026-05-20",
    }

    def __init__(self) -> None:
        super().__init__(api_key="stub")

    async def fetch(self, target: str) -> FetchResult:
        items = [self.STALE, self.FRESH]
        return FetchResult(
            source_name="eodhd_news",
            schema_name="eodhd.news.v1",
            source_url=f"https://eodhd.test/news?s={target}",
            http_status=200,
            payload=items,
            items=items,
        )


async def test_stale_news_dropped_before_classification(
    db_session: AsyncSession,
) -> None:
    """The 7-month-old article must be ignored without any LLM call;
    only the fresh one reaches the classifier."""
    db_session.add(
        TrackedCompany(
            symbol="6451", exchange="TW", company_name="Shunsin Technology", source="manual"
        )
    )
    await db_session.commit()

    clf = _FakeClassifier()
    await sync_catalysts(
        db_session, news_adapter=_StaleNewsStub(), classifier=clf
    )

    # Only the FRESH item should have been classified.
    assert clf.calls == 1

    # Both items become Event rows; the stale one is status='ignored'
    # with ignore_reason='stale_news_*'.
    events = (await db_session.execute(select(Event))).scalars().all()
    by_link = {e.source_url: e for e in events if e.source_url}
    stale = by_link[_StaleNewsStub.STALE["link"]]
    fresh = by_link[_StaleNewsStub.FRESH["link"]]
    assert stale.status == "ignored"
    assert (stale.payload or {}).get("ignore_reason", "").startswith("stale_news_")
    # The fresh one passes through classification and produces a
    # notification (auto-send because the fake classifier returns
    # importance=high for the AI-chip-order title).
    assert fresh.status == "notified"


class _AlwaysAlertableClassifier(_FakeClassifier):
    """Always returns a high-importance product_launch — isolates URL-dedup
    tests from _FakeClassifier's title-keyword routing, which falls through
    to casual_mention on non-English titles."""

    async def classify(
        self, *, company_name: str, symbol: str, title: str, content: str
    ) -> ClassificationResult:
        self.calls += 1
        return ClassificationResult(
            "completed",
            {
                "is_company_critical": True,
                "event_subtype": "product_launch",
                "importance": "high",
                "confidence": 0.9,
                "expected_impact": "Material new product.",
                "summary": f"${symbol} launches a new product.",
                "why_it_matters": "Commercial momentum.",
                "suggested_action": "research",
                "ignore_reason": None,
            },
            response_id="resp_x",
        )


class _MobileDesktopVariantStub(WebSearchNewsAdapter):
    """Two ``items`` that are the SAME content served at mobile vs desktop
    URLs (real shape of the Unitree H2 PLUS launch dupes: /cn/mobile/H2plus/
    and /cn/H2plus/). Canonical URL normalization MUST drop the /mobile/
    segment so both items hash to the same sid and the second emission
    short-circuits at events.get_by_source_event_id before classify."""

    _ITEMS = [
        {
            "title": "Unitree H2 PLUS 人形机器人新品发布",
            "content": "Unitree announces H2 PLUS reference humanoid robot.",
            "link": "https://www.unitree.com/cn/mobile/H2plus/",
            "date": "2026-06-01",
        },
        {
            "title": "Unitree H2 PLUS 人形机器人新品发布",
            "content": "Unitree announces H2 PLUS reference humanoid robot.",
            "link": "https://www.unitree.com/cn/H2plus/",
            "date": "2026-06-01",
        },
    ]

    def __init__(self) -> None:
        super().__init__(api_key="stub")

    async def fetch(
        self, *, company_name: str, symbol: str, exchange: str
    ) -> WebSearchResult:
        return WebSearchResult("completed", list(self._ITEMS))


async def test_mobile_url_variant_collapses_via_canonical_url(
    db_session: AsyncSession, monkeypatch
) -> None:
    """Regression for the Unitree H2 PLUS triple-alert (follow-up).
    Mobile and desktop URLs of the same product page must produce ONE
    event/notification — the second item's sid collides with the first
    after normalize_url strips the /mobile/ path segment."""
    from catalyst_radar.config import settings

    monkeypatch.setattr(settings, "catalyst_websearch_enabled", True)
    monkeypatch.setattr(
        settings, "catalyst_websearch_gap_exchanges", "SSE,SZSE,TW"
    )
    db_session.add(
        TrackedCompany(
            symbol="A26029",
            exchange="SSE",
            country="CN",
            company_name="宇树科技",
            source="manual",
        )
    )
    await db_session.commit()
    clf = _AlwaysAlertableClassifier()

    await sync_catalysts(
        db_session,
        news_adapter=_EmptyNewsStub(),
        classifier=clf,
        websearch_adapter=_MobileDesktopVariantStub(),
    )

    # 2nd item's sid matches the 1st → short-circuit at
    # get_by_source_event_id → never reach classify.
    assert clf.calls == 1
    events = (await db_session.execute(select(Event))).scalars().all()
    assert len(events) == 1
    notifs = (await db_session.execute(select(Notification))).scalars().all()
    assert len(notifs) == 1


class _UndatedProductPageStub(WebSearchNewsAdapter):
    """Web_search return for a long-lived static product page — the
    pattern that produced the Unitree H2 / R1 pre-sale / DigitalServo
    false-fresh alerts. ``date=None`` is the only signal we have that
    the page wasn't dated."""

    _ITEM = {
        "title": "Unitree H2 humanoid robot product page",
        "content": "Full-size humanoid platform with 31 degrees of freedom.",
        "link": "https://www.unitree.com/cn/H2/",
        "date": None,
    }

    def __init__(self) -> None:
        super().__init__(api_key="stub")

    async def fetch(
        self, *, company_name: str, symbol: str, exchange: str
    ) -> WebSearchResult:
        return WebSearchResult("completed", [dict(self._ITEM)])


async def test_undated_news_dropped_before_classification(
    db_session: AsyncSession, monkeypatch
) -> None:
    """Items with ``date=None`` MUST be ignored without an LLM call. The
    stale-news age gate is silently skipped for undated items, so static
    product pages (Unitree H2/R1/DigitalServo on the company's own
    domain, long-lived but undated) used to slip through. Regression
    for the Cluster-B/C stale-launch alerts."""
    from catalyst_radar.config import settings

    monkeypatch.setattr(settings, "catalyst_websearch_enabled", True)
    monkeypatch.setattr(
        settings, "catalyst_websearch_gap_exchanges", "SSE,SZSE,TW"
    )
    monkeypatch.setattr(settings, "catalyst_drop_undated_news", True)
    db_session.add(
        TrackedCompany(
            symbol="A26029",
            exchange="SSE",
            country="CN",
            company_name="宇树科技",
            source="manual",
        )
    )
    await db_session.commit()
    clf = _FakeClassifier()

    await sync_catalysts(
        db_session,
        news_adapter=_EmptyNewsStub(),
        classifier=clf,
        websearch_adapter=_UndatedProductPageStub(),
    )

    assert clf.calls == 0
    events = (await db_session.execute(select(Event))).scalars().all()
    assert len(events) == 1
    assert events[0].status == "ignored"
    assert (events[0].payload or {}).get("ignore_reason") == "undated_news"
    notifs = (await db_session.execute(select(Notification))).scalars().all()
    assert notifs == []


async def test_undated_drop_can_be_disabled_via_config(
    db_session: AsyncSession, monkeypatch
) -> None:
    """Operator escape hatch: when ``catalyst_drop_undated_news`` is False
    the legacy behavior returns (undated items reach the classifier).
    Lets a future user opt back in to LLM-judging undated pages if the
    company has primary content with no in-page date."""
    from catalyst_radar.config import settings

    monkeypatch.setattr(settings, "catalyst_websearch_enabled", True)
    monkeypatch.setattr(
        settings, "catalyst_websearch_gap_exchanges", "SSE,SZSE,TW"
    )
    monkeypatch.setattr(settings, "catalyst_drop_undated_news", False)
    db_session.add(
        TrackedCompany(
            symbol="A26029",
            exchange="SSE",
            country="CN",
            company_name="宇树科技",
            source="manual",
        )
    )
    await db_session.commit()
    clf = _FakeClassifier()

    await sync_catalysts(
        db_session,
        news_adapter=_EmptyNewsStub(),
        classifier=clf,
        websearch_adapter=_UndatedProductPageStub(),
    )

    # Classifier IS called when the gate is disabled — restoring legacy
    # behavior. Whether it persists an event depends on the fake's output
    # for this title ("Unitree H2 humanoid robot product page" → falls
    # through to the casual_mention branch); we don't assert on that here.
    assert clf.calls == 1


def test_normalize_url_strips_mobile_and_amp_path_segments() -> None:
    """Direct unit test for the canonicalizer powering the mobile/desktop
    dedup. Both inputs must produce identical canonical strings — if this
    regresses, the integration tests above stay green only because of
    incidental URL shape, so guard it here too."""
    assert normalize_url("https://www.unitree.com/cn/mobile/H2plus/") == normalize_url(
        "https://www.unitree.com/cn/H2plus/"
    )
    assert normalize_url("https://news.example.com/amp/article-123") == normalize_url(
        "https://news.example.com/article-123"
    )
    # /mobile/ as the FIRST segment also strips (common pattern, e.g. m.x.com).
    assert normalize_url("https://example.com/mobile/page") == normalize_url(
        "https://example.com/page"
    )
    # Two genuinely-different URLs must NOT collapse.
    assert normalize_url("https://www.unitree.com/cn/H2/") != normalize_url(
        "https://www.unitree.com/cn/H2plus/"
    )


class _EastmoneyStub(EastmoneyNewsAdapter):
    """Per-ticker CN news, dated. Returns one alertable item per queried
    code so we can assert the sweep dated + persisted it."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def fetch(self, target: str) -> FetchResult:
        self.calls.append(target)
        item = {
            "title": f"{target} wins major AI inference chip packaging order",
            "content": "A substantial new customer win with real revenue impact.",
            "link": f"http://finance.eastmoney.com/a/{target}-order.html",
            "date": "2026-06-03T21:31:00+08:00",
            "source_label": "证券时报网",
        }
        return FetchResult(
            source_name="eastmoney_news",
            schema_name="akshare.stock_news_em.v1",
            source_url="https://so.eastmoney.com/news/s",
            http_status=200,
            payload={"code": target, "row_count": 1},
            items=[item],
        )


async def test_eastmoney_news_sweeps_cn_listed_with_real_dates(
    db_session: AsyncSession,
) -> None:
    """A tracked CN *listed* name gets swept by the dated Eastmoney path,
    and the resulting event carries the publish date (so the freshness gate
    works and undated_news never fires) — the whole point of this source."""
    db_session.add(
        TrackedCompany(
            symbol="688797",
            exchange="SSE",
            country="CN",
            company_name="臻宝科技",
            source="manual",
        )
    )
    await db_session.commit()
    em = _EastmoneyStub()

    s = await sync_catalysts(
        db_session,
        news_adapter=_EmptyNewsStub(),
        classifier=_FakeClassifier(),
        eastmoney_adapter=em,
    )

    assert em.calls == ["688797"]
    assert s.autosent == 1
    events = (await db_session.execute(select(Event))).scalars().all()
    assert [e.source_name for e in events] == ["eastmoney_news"]
    assert events[0].event_date is not None  # dated, not undated
    assert events[0].symbol == "688797"


async def test_eastmoney_covers_cn_listed_websearch_keeps_preipo_and_gaps(
    db_session: AsyncSession, monkeypatch
) -> None:
    """The routing contract: a CN *listed* name is owned by Eastmoney and
    must NOT be re-swept by web_search (the reduction in web_search
    reliance); a CN *pre-IPO* name (no listing code) and a non-CN gap name
    (TW) MUST still reach web_search."""
    from catalyst_radar.config import settings

    monkeypatch.setattr(settings, "catalyst_websearch_enabled", True)
    monkeypatch.setattr(settings, "catalyst_websearch_all_markets", True)

    db_session.add(  # CN listed → Eastmoney only
        TrackedCompany(
            symbol="688797", exchange="SSE", country="CN",
            company_name="臻宝科技", source="manual",
        )
    )
    db_session.add(  # CN pre-IPO (CSRC reservation) → web_search only
        TrackedCompany(
            symbol="A25310", exchange="SSE", country="CN",
            company_name="长鑫科技", source="manual",
        )
    )
    db_session.add(  # non-CN gap → web_search
        TrackedCompany(
            symbol="6451", exchange="TW",
            company_name="Shunsin Technology", source="manual",
        )
    )
    await db_session.commit()

    em = _EastmoneyStub()
    websearch_calls: list[str] = []

    class _RecordingWebSearch(WebSearchNewsAdapter):
        def __init__(self) -> None:
            super().__init__(api_key="stub")

        async def fetch(
            self, *, company_name: str, symbol: str, exchange: str
        ) -> WebSearchResult:
            websearch_calls.append(symbol)
            return WebSearchResult("completed", [])

    await sync_catalysts(
        db_session,
        news_adapter=_EmptyNewsStub(),
        classifier=_FakeClassifier(),
        websearch_adapter=_RecordingWebSearch(),
        eastmoney_adapter=em,
    )

    # Eastmoney swept only the listed CN name (pre-IPO has no listing code).
    assert em.calls == ["688797"]
    # web_search swept the pre-IPO CN name + the TW gap name, NOT the listed
    # CN name (688797 is now Eastmoney's).
    assert sorted(websearch_calls) == ["6451", "A25310"]


async def test_eastmoney_sweep_disabled_via_config(
    db_session: AsyncSession, monkeypatch
) -> None:
    """Operator kill switch: with the flag off, no CN listed name is swept
    by Eastmoney and web_search reverts to covering it."""
    from catalyst_radar.config import settings

    monkeypatch.setattr(settings, "catalyst_eastmoney_news_enabled", False)
    monkeypatch.setattr(settings, "catalyst_websearch_enabled", True)
    monkeypatch.setattr(settings, "catalyst_websearch_all_markets", True)

    db_session.add(
        TrackedCompany(
            symbol="688797", exchange="SSE", country="CN",
            company_name="臻宝科技", source="manual",
        )
    )
    await db_session.commit()

    em = _EastmoneyStub()
    websearch_calls: list[str] = []

    class _RecordingWebSearch(WebSearchNewsAdapter):
        def __init__(self) -> None:
            super().__init__(api_key="stub")

        async def fetch(
            self, *, company_name: str, symbol: str, exchange: str
        ) -> WebSearchResult:
            websearch_calls.append(symbol)
            return WebSearchResult("completed", [])

    await sync_catalysts(
        db_session,
        news_adapter=_EmptyNewsStub(),
        classifier=_FakeClassifier(),
        websearch_adapter=_RecordingWebSearch(),
        eastmoney_adapter=em,
    )

    assert em.calls == []  # sweep skipped entirely
    assert websearch_calls == ["688797"]  # web_search picks it back up


# ── step 3: repeat-alert window ───────────────────────────────


class _TwoStoriesSameSubtypeStub(EodhdNewsAdapter):
    """Two genuinely DIFFERENT stories (distinct story_keys, dissimilar
    text) that share (symbol, subtype) and land within hours of each
    other — the bundled-re-report leak the repeat window must quiet."""

    def __init__(self, dates: tuple[str, str] = ("2026-06-02", "2026-06-03")) -> None:
        super().__init__(api_key="stub")
        self._items = [
            {
                "title": "Acme signs multi-year supply agreement with MegaCorp",
                "content": "Substantial partnership announcement with real revenue impact.",
                "link": "https://x.example/megacorp-deal",
                "date": dates[0],
            },
            {
                "title": "OtherCo and Acme to co-develop optical packaging line",
                "content": "A second, different partnership announced the next day.",
                "link": "https://y.example/otherco-deal",
                "date": dates[1],
            },
        ]

    async def fetch(self, target: str) -> FetchResult:
        return FetchResult(
            source_name="eodhd_news",
            schema_name="eodhd.news.v1",
            source_url=f"https://eodhd.test/news?s={target}",
            http_status=200,
            payload=self._items,
            items=self._items,
        )


class _DistinctPartnershipClassifier(_FakeClassifier):
    """Same subtype for both items but DISTINCT story_keys and dissimilar
    summaries; the judge answers 'different'. Nothing in the dedup stack
    merges them — only the repeat window can quiet the second alert."""

    _KEYS = ["acme_partnership_megacorp", "acme_partnership_otherco"]
    _SUMMARIES = [
        "$AAPL signs a multi-year supply agreement with MegaCorp.",
        "A second co-development line with OtherCo was announced.",
    ]

    def __init__(self) -> None:
        super().__init__()
        self.judge_calls = 0

    async def classify(
        self, *, company_name: str, symbol: str, title: str, content: str
    ) -> ClassificationResult:
        self.calls += 1
        idx = (self.calls - 1) % 2
        return ClassificationResult(
            "completed",
            {
                "is_company_critical": True,
                "event_subtype": "partnership",
                "importance": "high",
                "confidence": 0.95,
                "expected_impact": "New revenue line.",
                "summary": self._SUMMARIES[idx],
                "why_it_matters": "Material.",
                "suggested_action": "research",
                "ignore_reason": "",
                "story_key": self._KEYS[idx],
            },
            response_id="resp_x",
        )

    async def same_event(
        self, *, a_title: str, a_summary: str, b_title: str, b_summary: str
    ) -> bool:
        self.judge_calls += 1
        return False


async def test_repeat_window_demotes_second_same_subtype_alert(
    db_session: AsyncSession,
) -> None:
    """step 3: a second autosend-grade catalyst for the same
    (symbol, subtype) within the repeat window is demoted to review —
    the event + notification still exist (nothing is lost), but Telegram
    only fires once."""
    await _seed_company(db_session)
    clf = _DistinctPartnershipClassifier()

    s = await sync_catalysts(
        db_session, news_adapter=_TwoStoriesSameSubtypeStub(), classifier=clf
    )
    assert s.deduped == 0  # both are real, distinct events
    assert s.autosent == 1
    assert s.review == 1

    events = (await db_session.execute(select(Event))).scalars().all()
    assert len(events) == 2
    by_status = {e.status: e for e in events}
    assert set(by_status) == {"notified", "review"}
    demoted = by_status["review"]
    marker = ((demoted.payload or {}).get("classification") or {}).get(
        "autosend_demoted_by_repeat_window"
    )
    assert marker == by_status["notified"].id

    notifs = (await db_session.execute(select(Notification))).scalars().all()
    assert sorted(n.status for n in notifs) == ["pending", "review"]


async def test_repeat_window_exempts_distant_event_dates(
    db_session: AsyncSession,
) -> None:
    """Two same-subtype stories whose event_dates are further apart than
    the proximity gap are clearly distinct events (a second deal weeks
    later) — both must alert despite landing in the same sync run."""
    await _seed_company(db_session)
    clf = _DistinctPartnershipClassifier()

    s = await sync_catalysts(
        db_session,
        news_adapter=_TwoStoriesSameSubtypeStub(dates=("2026-05-20", "2026-06-03")),
        classifier=clf,
    )
    assert s.autosent == 2
    notifs = (await db_session.execute(select(Notification))).scalars().all()
    assert sorted(n.status for n in notifs) == ["pending", "pending"]


async def test_repeat_window_disabled_via_config(
    db_session: AsyncSession, monkeypatch
) -> None:
    from catalyst_radar.config import settings

    monkeypatch.setattr(settings, "catalyst_repeat_alert_window_hours", 0)
    await _seed_company(db_session)

    s = await sync_catalysts(
        db_session,
        news_adapter=_TwoStoriesSameSubtypeStub(),
        classifier=_DistinctPartnershipClassifier(),
    )
    assert s.autosent == 2  # gate off → both fire


# ── step 4: ignored-row retention sweep ───────────────────────


async def test_stale_ignored_catalysts_are_purged(db_session: AsyncSession) -> None:
    """Ignored catalyst rows are kept short-term (they're the idempotency
    marker that stops re-classification while the article is still in the
    feed) and purged once old enough that the freshness gate would re-drop
    a re-encounter without an LLM call. Non-catalyst, non-ignored, and
    recent rows must survive."""
    from datetime import UTC, datetime

    from catalyst_radar.repositories.event_repository import EventRepository

    old = datetime(2026, 1, 1, tzinfo=UTC)
    cutoff = datetime(2026, 5, 1, tzinfo=UTC)

    def _evt(sid: str, *, etype: str, status: str, created: datetime) -> Event:
        return Event(
            event_type=etype,
            source_name="eodhd_news",
            source_event_id=sid,
            dedup_key=f"k:{sid}",
            symbol="AAPL",
            exchange="US",
            title=sid,
            status=status,
            created_at=created,
        )

    purgeable = _evt("old-ignored", etype="catalyst", status="ignored", created=old)
    fresh_ignored = _evt(
        "fresh-ignored", etype="catalyst", status="ignored",
        created=datetime(2026, 6, 1, tzinfo=UTC),
    )
    old_notified = _evt("old-notified", etype="catalyst", status="notified", created=old)
    old_ipo = _evt("old-ipo-ignored", etype="ipo", status="ignored", created=old)
    db_session.add_all([purgeable, fresh_ignored, old_notified, old_ipo])
    await db_session.commit()

    deleted = await EventRepository(db_session).delete_stale_ignored_catalysts(cutoff)
    await db_session.commit()
    assert deleted == 1

    survivors = {
        e.source_event_id
        for e in (await db_session.execute(select(Event))).scalars().all()
    }
    assert survivors == {"fresh-ignored", "old-notified", "old-ipo-ignored"}


async def test_websearch_lookback_days_is_runtime_editable(
    db_session: AsyncSession, monkeypatch
) -> None:
    """The adapter snapshots settings.catalyst_websearch_lookback_days at
    construction; the sweep must re-apply the effective() value per run so
    a Settings edit lands without a restart."""
    from catalyst_radar.config import settings

    monkeypatch.setattr(settings, "catalyst_websearch_enabled", True)
    monkeypatch.setattr(settings, "catalyst_websearch_lookback_days", 3)
    await _seed_company(db_session)

    adapter = _WebSearchStub()
    assert adapter.lookback_days == 3  # construction already sees settings…
    adapter.lookback_days = 7  # …simulate a stale pre-edit snapshot
    await sync_catalysts(
        db_session,
        news_adapter=_EmptyNewsStub(),
        classifier=_FakeClassifier(),
        websearch_adapter=adapter,
    )
    assert adapter.lookback_days == 3  # effective() value re-applied per run


async def test_gap_always_on_covers_taiwan_when_master_off(
    db_session: AsyncSession, monkeypatch
) -> None:
    """with the master web_search switch OFF, gap exchanges (Taiwan)
    are still swept — web_search is their *only* news source, so a tracked
    TW name still gets catalyst coverage by default."""
    from catalyst_radar.config import settings

    monkeypatch.setattr(settings, "catalyst_websearch_enabled", False)
    monkeypatch.setattr(settings, "catalyst_websearch_gap_always_on", True)
    monkeypatch.setattr(settings, "catalyst_websearch_gap_exchanges", "TW")
    db_session.add(
        TrackedCompany(
            symbol="6451", exchange="TW", country="TW",
            company_name="Shunsin", source="manual",
        )
    )
    await db_session.commit()

    await sync_catalysts(
        db_session,
        news_adapter=_EmptyNewsStub(),
        classifier=_FakeClassifier(),
        websearch_adapter=_WebSearchStub(),
    )
    events = (await db_session.execute(select(Event))).scalars().all()
    assert len(events) == 1
    notifs = (await db_session.execute(select(Notification))).scalars().all()
    assert len(notifs) == 1


async def test_gap_always_on_off_skips_taiwan(
    db_session: AsyncSession, monkeypatch
) -> None:
    """With gap-always-on disabled AND the master switch off, the sweep is
    skipped entirely — the TW name gets no web_search coverage."""
    from catalyst_radar.config import settings

    monkeypatch.setattr(settings, "catalyst_websearch_enabled", False)
    monkeypatch.setattr(settings, "catalyst_websearch_gap_always_on", False)
    monkeypatch.setattr(settings, "catalyst_websearch_gap_exchanges", "TW")
    db_session.add(
        TrackedCompany(
            symbol="6451", exchange="TW", country="TW",
            company_name="Shunsin", source="manual",
        )
    )
    await db_session.commit()

    await sync_catalysts(
        db_session,
        news_adapter=_EmptyNewsStub(),
        classifier=_FakeClassifier(),
        websearch_adapter=_WebSearchStub(),
    )
    assert (await db_session.execute(select(Event))).scalars().all() == []


def test_canonical_story_key_strips_trailing_dates() -> None:
    """lynchpin: the normalizer that lets re-reporting on day N+1
    dedup against day N. Trailing _YYYY / _YYYY-MM / _YYYY-MM-DD are stripped
    so dated legacy-v2 keys collapse onto the bare v3 key; lower/strip
    canonicalise; a non-trailing date is left intact."""
    from catalyst_radar.services.catalyst_sync import _canonical_story_key

    base = "a25310_ipo_review_pass"
    assert _canonical_story_key(f"{base}_2026-05-27") == base
    assert _canonical_story_key(f"{base}_2026-05") == base
    assert _canonical_story_key(f"{base}_2026") == base
    assert _canonical_story_key(base) == base
    # Case + surrounding whitespace are normalised.
    assert _canonical_story_key("  A25310_IPO_Review_Pass  ") == base
    # Empty / missing → None (no key to dedup on).
    assert _canonical_story_key(None) is None
    assert _canonical_story_key("") is None
    assert _canonical_story_key("   ") is None
    # A date that is NOT a trailing suffix is preserved (only the tail is a
    # date-fragment artifact worth stripping).
    assert _canonical_story_key("2026_partnership_nvda") == "2026_partnership_nvda"


class _RecordingNewsStub(EodhdNewsAdapter):
    """Records every EODHD /news code it's asked to fetch, returns nothing."""

    def __init__(self) -> None:
        super().__init__(api_key="stub")
        self.codes: list[str] = []

    async def fetch(self, target: str) -> FetchResult:
        self.codes.append(target)
        return FetchResult(
            source_name="eodhd_news",
            schema_name="eodhd.news.v1",
            source_url="https://eodhd.test/news",
            http_status=200,
            payload=[],
            items=[],
        )


async def test_eodhd_news_skipped_for_gap_exchanges(db_session: AsyncSession, monkeypatch) -> None:
    """Quota fix: EODHD /news (weight 5) is NOT called for exchanges it has
    no coverage for (Taiwan, CN-listed). US-listed names are still fetched."""
    from catalyst_radar.config import settings

    monkeypatch.setattr(settings, "catalyst_eodhd_news_skip_exchanges", "TW,SSE")
    db_session.add_all(
        [
            TrackedCompany(symbol="AAPL", exchange="US", company_name="Apple", source="manual"),
            TrackedCompany(symbol="6451", exchange="TW", company_name="Shunsin", source="manual"),
            TrackedCompany(symbol="600519", exchange="SSE", company_name="Moutai", source="manual"),
        ]
    )
    await db_session.commit()

    news = _RecordingNewsStub()
    s = await sync_catalysts(db_session, news_adapter=news, classifier=_FakeClassifier())

    # US-listed fetched; TW + SSE skipped (no wasted 5-unit EODHD calls).
    assert news.codes == ["AAPL.US"]
    assert s.eodhd_news_skipped == 2


async def test_eodhd_news_skip_disabled_fetches_all(db_session: AsyncSession, monkeypatch) -> None:
    """Empty skip list restores the old behaviour: every tracked name is
    fetched (so the skip is opt-out-able)."""
    from catalyst_radar.config import settings

    monkeypatch.setattr(settings, "catalyst_eodhd_news_skip_exchanges", "")
    db_session.add_all(
        [
            TrackedCompany(symbol="AAPL", exchange="US", company_name="Apple", source="manual"),
            TrackedCompany(symbol="6451", exchange="TW", company_name="Shunsin", source="manual"),
        ]
    )
    await db_session.commit()

    news = _RecordingNewsStub()
    s = await sync_catalysts(db_session, news_adapter=news, classifier=_FakeClassifier())
    assert sorted(news.codes) == ["6451.TW", "AAPL.US"]
    assert s.eodhd_news_skipped == 0


def test_eodhd_skip_exchanges_all_have_a_fallback() -> None:
    """Invariant: every exchange skipped from EODHD news must be covered by a
    fallback sweep — Eastmoney's CN set OR the web_search gap set — else a
    skipped name silently gets zero news. Reads the shipped class defaults
    (not the conftest-pinned values) so drift between the three lists fails
    here instead of in production."""
    from catalyst_radar.adapters.eastmoney_news import _CN_EXCHANGES
    from catalyst_radar.config import Settings

    def _set(spec: str) -> set[str]:
        return {x.strip().upper() for x in spec.split(",") if x.strip()}

    skip = _set(Settings.model_fields["catalyst_eodhd_news_skip_exchanges"].default)
    gap = _set(Settings.model_fields["catalyst_websearch_gap_exchanges"].default)
    eastmoney = {e.upper() for e in _CN_EXCHANGES}
    uncovered = skip - gap - eastmoney
    assert not uncovered, f"EODHD-skipped exchanges with no fallback source: {uncovered}"
