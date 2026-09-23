from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from catalyst_radar.adapters.hkex_prospectus import (
    HkexProspectusAdapter,
    _slice_summary,
    extract_summary_from_pdf,
    parse_new_listings,
)
from catalyst_radar.models.base import utcnow
from catalyst_radar.models.event import Event
from catalyst_radar.services.hk_ipo_enrich import enrich_hk_ipos
from catalyst_radar.services.ipo_summarizer import IpoSummary, IpoWebDescription

NLI = (Path(__file__).parent / "fixtures" / "hkex_new_listings.html").read_text()


def test_parse_new_listings_maps_code_to_prospectus() -> None:
    rows = parse_new_listings(NLI)
    assert "9872" in rows  # Lumora, normalised (no leading zero)
    t = rows["9872"]
    assert "Lumora" in t["name"]
    assert t["prospectus_url"].endswith(".pdf")
    assert t["prospectus_url"].startswith("https://example.com/listedco/")
    # prospectus link must differ from the announcement link
    assert t["prospectus_url"] != t["announcement_url"]


def test_slice_summary_anchors_and_bounds() -> None:
    text = "Cover page TOC ... OVERVIEW OF OUR business we make widgets. " * 50
    out = _slice_summary(text, max_chars=120)
    assert 0 < len(out) <= 120
    assert out.lower().startswith("overview of our")


def test_extract_summary_handles_bad_pdf() -> None:
    assert extract_summary_from_pdf(b"not a pdf at all") == ""


class _StubAdapter(HkexProspectusAdapter):
    async def fetch_prospectus(self, stock_code: str) -> dict | None:
        return {
            "summary": "Lumora develops antibiotics for drug-resistant infections.",
            "filing_url": "https://www1.hkexnews.hk/x.pdf",
            "doc_type": "prospectus",
        }


class _StubSummarizer:
    configured = True

    async def summarize(self, *, company_name: str, symbol: str, summary_text: str):
        return IpoSummary(
            description="Develops antibiotics for drug-resistant infections.",
            market_cap_usd=None,
        )

    async def describe_via_web(self, *, company_name, symbol, exchange=None, listing_date=None):
        # Should never be called when summarize() returns a description.
        raise AssertionError("describe_via_web should not run when prospectus succeeded")


class _EmptyAdapter(HkexProspectusAdapter):
    """Prospectus PDF returns a non-business text slice (e.g. TOC)."""

    async def fetch_prospectus(self, stock_code: str) -> dict | None:
        return {
            "summary": "Table of contents page 1 page 2 page 3...",
            "filing_url": "https://www1.hkexnews.hk/x.pdf",
            "doc_type": "prospectus",
        }


class _EmptyDescSummarizer:
    """Prospectus call yields no description (LLM saw TOC/junk); web fallback
    is what saves us."""

    configured = True
    web_called: list[tuple] = []

    async def summarize(self, *, company_name: str, symbol: str, summary_text: str):
        return IpoSummary(description=None, market_cap_usd=None)

    async def describe_via_web(self, *, company_name, symbol, exchange=None, listing_date=None):
        _EmptyDescSummarizer.web_called.append((company_name, symbol, exchange, listing_date))
        return IpoWebDescription(
            "completed",
            description="Designs and sells consumer 3D printing hardware globally.",
            sources=[
                {"name": "HKEXnews Prospectus", "url": "https://www1.hkexnews.hk/x.pdf"},
                {"name": "Reuters profile", "url": "https://reuters.com/x"},
            ],
        )


class _NoFetchAdapter(HkexProspectusAdapter):
    """Sentinel adapter that records (and rejects) any fetch_prospectus call —
    used to prove the second-run path skips the slow PDF download. The call
    counter is checked by the test because the helper swallows exceptions
    raised inside fetch_prospectus, so a bare raise alone would not fail."""

    calls: int = 0

    async def fetch_prospectus(self, stock_code: str) -> dict | None:
        _NoFetchAdapter.calls += 1
        raise AssertionError("fetch_prospectus called on already-attempted event")


async def test_enrich_hk_ipos_writes_profile_and_is_idempotent(
    db_session: AsyncSession,
) -> None:
    db_session.add(
        Event(
            event_type="ipo",
            source_name="aastocks",
            source_event_id="hkex:ipo:09872",
            dedup_key="t",
            symbol="09872",
            exchange="HKSE",
            country="HK",
            company_name="Lumora Therap-B",
            event_date=utcnow(),
            payload={"code": "09872"},
        )
    )
    await db_session.commit()

    s1 = await enrich_hk_ipos(db_session, adapter=_StubAdapter(), summarizer=_StubSummarizer())
    assert (s1.candidates, s1.enriched, s1.described) == (1, 1, 1)
    ev = (await db_session.execute(select(Event).where(Event.symbol == "09872"))).scalar_one()
    prof = ev.payload["profile"]
    assert prof["description"].startswith("Develops antibiotics")
    assert prof["source"] == "hkexnews" and prof["currency"] == "HKD"
    assert prof["checked"] is True

    s2 = await enrich_hk_ipos(db_session, adapter=_StubAdapter(), summarizer=_StubSummarizer())
    assert (s2.candidates, s2.enriched) == (0, 0)


async def test_enrich_hk_falls_back_to_websearch_when_prospectus_empty(
    db_session: AsyncSession,
) -> None:
    """The Shunsin/Creality failure mode: prospectus PDF fetched + parsed
    but the LLM saw a TOC page and returned no description. Web fallback
    fills it in and stores citation."""
    db_session.add(
        Event(
            event_type="ipo",
            source_name="aastocks",
            source_event_id="hkex:ipo:03388",
            dedup_key="creality",
            symbol="03388",
            exchange="HKSE",
            country="HK",
            company_name="Creality",
            event_date=utcnow(),
            payload={"code": "03388"},
        )
    )
    await db_session.commit()
    _EmptyDescSummarizer.web_called = []

    s = await enrich_hk_ipos(
        db_session, adapter=_EmptyAdapter(), summarizer=_EmptyDescSummarizer()
    )
    assert (s.candidates, s.enriched, s.described) == (1, 1, 1)
    assert len(_EmptyDescSummarizer.web_called) == 1
    company, symbol, exchange, _listing_date = _EmptyDescSummarizer.web_called[0]
    assert (company, symbol, exchange) == ("Creality", "03388", "HKSE")

    ev = (await db_session.execute(select(Event).where(Event.symbol == "03388"))).scalar_one()
    prof = ev.payload["profile"]
    assert prof["description"].startswith("Designs and sells consumer 3D printing")
    assert prof["description_source"] == "websearch"
    assert len(prof["description_sources"]) == 2
    assert prof["description_sources"][0]["name"] == "HKEXnews Prospectus"
    # Prospectus profile fields preserved.
    assert prof["filing_url"] == "https://www1.hkexnews.hk/x.pdf"
    assert prof["doc_type"] == "prospectus"


async def test_enrich_hk_skips_prospectus_refetch_on_second_run(
    db_session: AsyncSession,
) -> None:
    """When a row already had its prospectus attempt (prospectus_attempted)
    in a previous run, the (slow) prospectus PDF must not be re-downloaded —
    the canonical fetch is once-only; only the web fallback runs again."""
    db_session.add(
        Event(
            event_type="ipo",
            source_name="aastocks",
            source_event_id="hkex:ipo:02723",
            dedup_key="deepzero",
            symbol="02723",
            exchange="HKSE",
            country="HK",
            company_name="DeepZero Tech",
            event_date=utcnow(),
            payload={
                "code": "02723",
                "profile": {
                    "checked": True,
                    "prospectus_attempted": True,
                    "source": "hkexnews",
                    "currency": "HKD",
                    "filing_url": "https://www1.hkexnews.hk/x.pdf",
                    "doc_type": "prospectus",
                    # description deliberately missing — prior run failed
                },
            },
        )
    )
    await db_session.commit()
    _EmptyDescSummarizer.web_called = []
    _NoFetchAdapter.calls = 0

    # _NoFetchAdapter records (and raises on) any re-fetch; the helper swallows
    # the raise, so the counter is the real guarantee that the PDF was skipped.
    s = await enrich_hk_ipos(
        db_session, adapter=_NoFetchAdapter(), summarizer=_EmptyDescSummarizer()
    )
    assert _NoFetchAdapter.calls == 0  # canonical prospectus PDF NOT re-fetched
    assert (s.candidates, s.enriched, s.described) == (1, 1, 1)
    assert len(_EmptyDescSummarizer.web_called) == 1

    ev = (await db_session.execute(select(Event).where(Event.symbol == "02723"))).scalar_one()
    prof = ev.payload["profile"]
    assert prof["description_source"] == "websearch"
    # filing_url from prior run preserved.
    assert prof["filing_url"] == "https://www1.hkexnews.hk/x.pdf"


class _ProspectusReadyAdapter(HkexProspectusAdapter):
    """A prospectus IS available — proves an imminent event still goes
    web-first (the helper) rather than prospectus-first (the old block)."""

    async def fetch_prospectus(self, stock_code: str) -> dict | None:
        return {
            "summary": "Robo Co builds industrial robots.",
            "filing_url": "https://www1.hkexnews.hk/x.pdf",
            "doc_type": "prospectus",
        }


class _WebFirstSummarizer:
    """summarize() would yield a description, but for an imminent event the
    web path must run FIRST and succeed — so summarize() must never run."""

    configured = True
    web_called: list[tuple] = []
    summarize_called: int = 0

    async def summarize(self, *, company_name: str, symbol: str, summary_text: str):
        _WebFirstSummarizer.summarize_called += 1
        return IpoSummary(description="From the prospectus.", market_cap_usd=None)

    async def describe_via_web(self, *, company_name, symbol, exchange=None, listing_date=None):
        _WebFirstSummarizer.web_called.append((company_name, symbol, exchange, listing_date))
        return IpoWebDescription("completed", description="A robotics company.")


async def test_imminent_hk_ipo_gets_websearch_provisional(
    db_session: AsyncSession,
) -> None:
    """An imminent HK IPO (a pending notification is about to dispatch) gets a
    web_search provisional FIRST, so its first alert carries a blurb even when
    no prospectus has been filed yet."""
    from catalyst_radar.models.notification import Notification

    db_session.add(
        Event(
            event_type="ipo",
            source_name="aastocks",
            source_event_id="hkex:ipo:09999",
            dedup_key="robo",
            symbol="09999",
            exchange="HKSE",
            country="HK",
            company_name="Robo Co",
            event_date=utcnow(),
            payload={"code": "09999"},
        )
    )
    await db_session.commit()
    event = (
        await db_session.execute(select(Event).where(Event.symbol == "09999"))
    ).scalar_one()

    db_session.add(
        Notification(event_id=event.id, dedup_key=f"robo:n:{event.id}", status="pending")
    )
    await db_session.commit()
    _WebFirstSummarizer.web_called = []
    _WebFirstSummarizer.summarize_called = 0

    s = await enrich_hk_ipos(
        db_session, adapter=_ProspectusReadyAdapter(), summarizer=_WebFirstSummarizer()
    )
    assert (s.candidates, s.enriched, s.described) == (1, 1, 1)
    assert len(_WebFirstSummarizer.web_called) == 1
    # Web-first: the prospectus summarizer must not run when web wins.
    assert _WebFirstSummarizer.summarize_called == 0

    ev = (await db_session.execute(select(Event).where(Event.symbol == "09999"))).scalar_one()
    prof = ev.payload["profile"]
    assert prof["description_source"] == "websearch"
    assert prof["description"] == "A robotics company."


def test_format_ipo_renders_websearch_description_citations() -> None:
    """When the description came from web_search, the alert must cite the
    URLs so the trader can audit them. Filing-derived descriptions don't
    show 'Description from web' — only show it when the source warrants it."""
    from catalyst_radar.services.alerts import format_ipo

    e = Event(
        event_type="ipo",
        source_name="aastocks",
        source_event_id="t",
        dedup_key="t",
        symbol="03388",
        exchange="HKSE",
        country="HK",
        company_name="Creality",
        event_date=utcnow(),
        payload={
            "code": "03388",
            "profile": {
                "description": "Designs and sells consumer 3D printing hardware.",
                "description_source": "websearch",
                "description_sources": [
                    {"name": "HKEXnews Prospectus", "url": "https://w1.hkexnews.hk/x.pdf"},
                    {"name": "Reuters", "url": "https://reuters.com/x"},
                ],
                "currency": "HKD",
            },
        },
    )
    out = format_ipo(e)
    assert "Designs and sells consumer 3D printing hardware." in out
    assert "Description from web:" in out
    assert '<a href="https://w1.hkexnews.hk/x.pdf">HKEXnews Prospectus</a>' in out
    assert '<a href="https://reuters.com/x">Reuters</a>' in out


def test_format_ipo_omits_citation_for_prospectus_description() -> None:
    """When description came from the prospectus path, no 'Description from
    web' line — the prospectus link is already shown elsewhere as Filing."""
    from catalyst_radar.services.alerts import format_ipo

    e = Event(
        event_type="ipo",
        source_name="aastocks",
        source_event_id="t",
        dedup_key="t",
        symbol="09872",
        exchange="HKSE",
        country="HK",
        company_name="Lumora Therap-B",
        event_date=utcnow(),
        payload={
            "code": "09872",
            "profile": {
                "description": "Develops antibiotics for drug-resistant infections.",
                "description_source": "prospectus",
                "currency": "HKD",
            },
        },
    )
    out = format_ipo(e)
    assert "Develops antibiotics" in out
    assert "Description from web" not in out


async def test_enrich_hk_disabled(db_session: AsyncSession) -> None:
    from catalyst_radar.repositories.config_repository import ConfigRepository

    await ConfigRepository(db_session).set("hkex_prospectus_enrich_enabled", False)
    await db_session.commit()
    out = await enrich_hk_ipos(db_session, adapter=_StubAdapter(), summarizer=_StubSummarizer())
    assert (out.candidates, out.enriched) == (0, 0)


async def test_pending_notification_event_wins_the_enrich_budget(
    db_session: AsyncSession, monkeypatch
) -> None:
    """Regression for the HQVT/01392 blurbless alert: an AAStocks recovery
    mints several IPOs at once; the event whose notification is pending
    dispatch must be enriched even when earlier-dated backlog rows would win
    a plain [:budget] clip (rows are ordered by event_date)."""
    from datetime import timedelta

    from catalyst_radar.config import settings
    from catalyst_radar.models.notification import Notification

    monkeypatch.setattr(settings, "hkex_prospectus_max_items_per_run", 1)

    def _hk(symbol: str, days: int) -> Event:
        return Event(
            event_type="ipo",
            source_name="aastocks",
            source_event_id=f"hkex:ipo:{symbol}",
            dedup_key=f"t:{symbol}",
            symbol=symbol,
            exchange="HKSE",
            country="HK",
            company_name=f"Co {symbol}",
            event_date=utcnow() + timedelta(days=days),
            payload={"code": symbol},
        )

    backlog = _hk("01111", days=1)  # earlier date → wins a naive clip
    about_to_alert = _hk("01392", days=11)
    db_session.add(backlog)
    db_session.add(about_to_alert)
    await db_session.commit()
    await db_session.refresh(about_to_alert)

    db_session.add(
        Notification(event_id=about_to_alert.id, dedup_key=f"t:n:{about_to_alert.id}")
    )
    await db_session.commit()

    await enrich_hk_ipos(db_session, adapter=_StubAdapter(), summarizer=_StubSummarizer())

    ev = (
        await db_session.execute(select(Event).where(Event.symbol == "01392"))
    ).scalar_one()
    assert (ev.payload.get("profile") or {}).get("description")
    other = (
        await db_session.execute(select(Event).where(Event.symbol == "01111"))
    ).scalar_one()
    assert not ((other.payload or {}).get("profile") or {}).get("description")
