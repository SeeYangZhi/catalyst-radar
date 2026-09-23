import json
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from catalyst_radar.adapters.sec_edgar import SecEdgarAdapter, extract_summary
from catalyst_radar.models.base import utcnow
from catalyst_radar.models.event import Event
from catalyst_radar.services.edgar_enrich import enrich_us_ipos
from catalyst_radar.services.ipo_summarizer import IpoSummary, IpoWebDescription

FX = Path(__file__).parent / "fixtures"
S1_HTML = (FX / "sec_s1_summary.html").read_text()
FTS = json.loads((FX / "sec_edgar_fts.json").read_text())
SUB = json.loads((FX / "sec_edgar_submissions.json").read_text())


class _Resp:
    def __init__(self, *, js: object = None, text: str = "") -> None:
        self._js = js
        self.text = text
        self.status_code = 200

    def json(self) -> object:
        return self._js


def test_extract_summary_is_bounded_and_nonempty() -> None:
    out = extract_summary(S1_HTML, max_chars=8000)
    assert 0 < len(out) <= 8000
    assert "<" not in out  # tags stripped
    assert any(w in out.lower() for w in ("company", "business", "prospectus"))


async def test_search_cik_and_latest_filing(monkeypatch) -> None:
    a = SecEdgarAdapter()

    async def fake_get(url: str, *, params=None):
        return _Resp(js=FTS) if "search-index" in url else _Resp(js=SUB)

    monkeypatch.setattr(a, "_get", fake_get)
    cik = await a.search_cik("BW Industrial Holdings Inc.")
    assert cik == "2080841"  # ciks[0] from the fixture, leading zeros stripped

    filing = await a.latest_filing(cik)
    assert filing is not None
    assert filing["form"].startswith("S-1")
    assert "/Archives/edgar/data/2080841/" in filing["doc_url"]
    assert filing["doc_url"].endswith(".htm")


class _StubAdapter(SecEdgarAdapter):
    async def fetch_summary(self, company_name: str) -> dict | None:
        return {
            "summary": "Acme builds industrial robots.",
            "filing_url": "https://sec.gov/x.htm",
            "form": "S-1",
            "filing_date": "2026-03-17",
            "cik": "1",
        }


class _StubSummarizer:
    configured = True

    async def summarize(self, *, company_name: str, symbol: str, summary_text: str):
        return IpoSummary(description="Builds industrial robots.", market_cap_usd=4.2e8)

    async def describe_via_web(self, *, company_name, symbol, exchange=None, listing_date=None):
        raise AssertionError("describe_via_web should not run when summarize succeeded")


class _EmptyDescSummarizer:
    """S-1 found but no usable description — websearch fallback fills in."""

    configured = True
    web_called: list[tuple] = []

    async def summarize(self, *, company_name: str, symbol: str, summary_text: str):
        return IpoSummary(description=None, market_cap_usd=None)

    async def describe_via_web(self, *, company_name, symbol, exchange=None, listing_date=None):
        _EmptyDescSummarizer.web_called.append((company_name, symbol, exchange))
        return IpoWebDescription(
            "completed",
            description="A U.S.-based EPC for industrial process systems.",
            sources=[{"name": "Reuters", "url": "https://reuters.com/x"}],
        )


async def test_enrich_us_ipos_writes_profile_and_is_idempotent(
    db_session: AsyncSession,
) -> None:
    db_session.add(
        Event(
            event_type="ipo",
            source_name="eodhd",
            source_event_id="eodhd:ipo:ACME.US",
            dedup_key="acme",
            symbol="ACME",
            exchange="NASDAQ",
            country="US",
            company_name="Acme Robotics Inc.",
            event_date=utcnow(),
            payload={"offer_price": 20, "shares": 1_000_000},
        )
    )
    await db_session.commit()

    s1 = await enrich_us_ipos(db_session, adapter=_StubAdapter(), summarizer=_StubSummarizer())
    assert (s1.candidates, s1.enriched, s1.described) == (1, 1, 1)

    ev = (await db_session.execute(select(Event).where(Event.symbol == "ACME"))).scalar_one()
    prof = ev.payload["profile"]
    assert prof["description"] == "Builds industrial robots."
    assert prof["market_cap"] == 4.2e8
    assert prof["deal_size"] == 20 * 1_000_000  # computed from price × shares
    assert prof["source"] == "sec_edgar" and prof["checked"] is True

    # Idempotent: already checked -> not a candidate again.
    s2 = await enrich_us_ipos(db_session, adapter=_StubAdapter(), summarizer=_StubSummarizer())
    assert (s2.candidates, s2.enriched) == (0, 0)


async def test_enrich_us_falls_back_to_websearch_when_summarizer_empty(
    db_session: AsyncSession,
) -> None:
    """When the S-1 was fetched but the LLM extracted no description from
    the summary slice, the web_search fallback must fill it in and the
    citation must land in profile.description_sources."""
    db_session.add(
        Event(
            event_type="ipo",
            source_name="eodhd",
            source_event_id="eodhd:ipo:BWGC.US",
            dedup_key="bwgc",
            symbol="BWGC",
            exchange="NYSE",
            country="US",
            company_name="BW Industrial Holdings Inc.",
            event_date=utcnow(),
            payload={"offer_price": 8, "shares": 2_000_000},
        )
    )
    await db_session.commit()
    _EmptyDescSummarizer.web_called = []

    s = await enrich_us_ipos(
        db_session, adapter=_StubAdapter(), summarizer=_EmptyDescSummarizer()
    )
    assert (s.candidates, s.enriched, s.described) == (1, 1, 1)
    assert len(_EmptyDescSummarizer.web_called) == 1

    ev = (await db_session.execute(select(Event).where(Event.symbol == "BWGC"))).scalar_one()
    prof = ev.payload["profile"]
    assert prof["description"].startswith("A U.S.-based EPC")
    assert prof["description_source"] == "websearch"
    assert prof["description_sources"] == [{"name": "Reuters", "url": "https://reuters.com/x"}]
    # S-1 fields preserved from the EDGAR path.
    assert prof["filing_url"] == "https://sec.gov/x.htm"
    assert prof["form"] == "S-1"


class _SummaryReadyAdapter(SecEdgarAdapter):
    """An S-1 IS available — proves an imminent event still goes web-first
    (the helper) rather than prospectus-first (the old block)."""

    async def fetch_summary(self, company_name: str) -> dict | None:
        return {
            "summary": "Acme builds industrial robots.",
            "filing_url": "https://sec.gov/x.htm",
            "form": "S-1",
            "filing_date": "2026-03-17",
            "cik": "1",
        }


class _WebFirstSummarizer:
    """summarize() would yield a description, but for an imminent event the
    web path must run FIRST and succeed — so summarize() must never run."""

    configured = True
    web_called: list[tuple] = []
    summarize_called: int = 0

    async def summarize(self, *, company_name: str, symbol: str, summary_text: str):
        _WebFirstSummarizer.summarize_called += 1
        return IpoSummary(description="From the S-1.", market_cap_usd=None)

    async def describe_via_web(self, *, company_name, symbol, exchange=None, listing_date=None):
        _WebFirstSummarizer.web_called.append((company_name, symbol, exchange, listing_date))
        return IpoWebDescription("completed", description="A robotics company.")


async def test_imminent_us_ipo_gets_websearch_provisional(
    db_session: AsyncSession,
) -> None:
    """An imminent US IPO (a pending notification is about to dispatch) gets a
    web_search provisional FIRST, so its first alert carries a blurb even when
    the S-1 path would also have produced one."""
    from catalyst_radar.models.notification import Notification

    db_session.add(
        Event(
            event_type="ipo",
            source_name="eodhd",
            source_event_id="eodhd:ipo:ROBO.US",
            dedup_key="robo",
            symbol="ROBO",
            exchange="NASDAQ",
            country="US",
            company_name="Robo Co",
            event_date=utcnow(),
            payload={"offer_price": 10, "shares": 1_000_000},
        )
    )
    await db_session.commit()
    event = (
        await db_session.execute(select(Event).where(Event.symbol == "ROBO"))
    ).scalar_one()

    db_session.add(
        Notification(event_id=event.id, dedup_key=f"robo:n:{event.id}", status="pending")
    )
    await db_session.commit()
    _WebFirstSummarizer.web_called = []
    _WebFirstSummarizer.summarize_called = 0

    s = await enrich_us_ipos(
        db_session, adapter=_SummaryReadyAdapter(), summarizer=_WebFirstSummarizer()
    )
    assert (s.candidates, s.enriched, s.described) == (1, 1, 1)
    assert len(_WebFirstSummarizer.web_called) == 1
    # Web-first: the S-1 summarizer must not run when web wins.
    assert _WebFirstSummarizer.summarize_called == 0

    ev = (await db_session.execute(select(Event).where(Event.symbol == "ROBO"))).scalar_one()
    prof = ev.payload["profile"]
    assert prof["description_source"] == "websearch"
    assert prof["description"] == "A robotics company."


async def test_enrich_disabled(db_session: AsyncSession) -> None:
    from catalyst_radar.repositories.config_repository import ConfigRepository

    await ConfigRepository(db_session).set("sec_edgar_enrich_enabled", False)
    await db_session.commit()
    out = await enrich_us_ipos(db_session, adapter=_StubAdapter(), summarizer=_StubSummarizer())
    assert (out.candidates, out.enriched) == (0, 0)
