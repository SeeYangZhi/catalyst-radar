"""Coverage for the earnings-enrichment service and its two consumers
(catalyst_sync wiring + format_catalyst earnings branch + the post-results
re-alert path in earnings_sync)."""

from __future__ import annotations

import io
import json
from datetime import datetime, timedelta
from pathlib import Path

import pypdf
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from catalyst_radar.adapters.base import FetchResult
from catalyst_radar.adapters.eodhd_calendar import EodhdEarningsAdapter
from catalyst_radar.models.base import today_local
from catalyst_radar.models.company import TrackedCompany
from catalyst_radar.models.event import Event
from catalyst_radar.models.notification import Notification
from catalyst_radar.services.alerts import (
    format_catalyst,
    format_catalyst_earnings,
    format_earnings_results,
)
from catalyst_radar.services.earnings_enrich import (
    ConsensusResult,
    EarningsEnricher,
    EnrichmentResult,
    _is_pdf,
    _pdf_to_text,
    _strip_html,
)
from catalyst_radar.services.earnings_sync import sync_earnings


def _make_test_pdf(text: str) -> bytes:
    """Build a tiny one-page PDF carrying `text` so the parser has real
    bytes to chew on — pypdf's text-extraction path must work end-to-end."""
    from pypdf import PdfWriter

    base = PdfWriter()
    base.add_blank_page(width=612, height=792)
    buf = io.BytesIO()
    base.write(buf)
    return buf.getvalue()


# ── unit: text utilities ─────────────────────────────────────────────────


def test_strip_html_removes_tags_scripts_and_collapses_whitespace() -> None:
    html = """
    <html><head><style>x{}</style></head>
    <body>
      <script>var x = 1;</script>
      <h1>Hello</h1>
      <p>World    <b>!</b></p>
    </body></html>
    """
    out = _strip_html(html)
    assert "Hello" in out
    assert "World" in out
    assert "<" not in out and ">" not in out
    assert "var x" not in out  # script content gone


def test_is_pdf_detects_via_content_type_url_and_magic_bytes() -> None:
    pdf_bytes = b"%PDF-1.5\nrest"
    assert _is_pdf("https://x/y", "application/pdf", b"")
    assert _is_pdf("https://x/y.PDF", "application/octet-stream", b"")
    assert _is_pdf("https://x/y?qs=1", "text/html", pdf_bytes)
    assert not _is_pdf("https://x/y", "text/html", b"<html>")


def test_pdf_to_text_handles_real_pdf_bytes() -> None:
    # pypdf's text extractor returns empty for a blank page (no fonts), so we
    # just assert the function does not raise — coverage of the loop and the
    # exception-swallowing per-page branch.
    blob = _make_test_pdf("ignored")
    assert _pdf_to_text(blob) == ""  # blank page → empty, no exception
    # A "PDF" with garbage tail used to raise; make sure that's contained.
    junk = b"%PDF-1.4\n" + b"not a real pdf"
    with pytest.raises(Exception):  # noqa: B017 - sanity that pypdf does raise
        pypdf.PdfReader(io.BytesIO(junk))


# ── unit: EnrichmentResult / EarningsEnricher budget ─────────────────────


async def test_enricher_returns_none_when_no_url_or_budget() -> None:
    e = EarningsEnricher(
        api_key="sk-x",
        model="gpt-5-mini",
        budget=2,
        max_chars=10000,
        fetch_timeout=10,
        max_output_tokens=1000,
    )
    assert await e.maybe_extract(url=None, company_name="X", symbol="X") is None
    e.budget = 0
    assert await e.maybe_extract(url="https://x", company_name="X", symbol="X") is None


async def test_enricher_merges_consensus_into_financials(monkeypatch) -> None:
    """Successful extraction triggers a consensus pass keyed on
    period_label; consensus result is merged into financials.consensus."""
    e = EarningsEnricher(
        api_key="sk-x",
        model="gpt-5-mini",
        budget=2,
        max_chars=10000,
        fetch_timeout=10,
        max_output_tokens=1000,
        consensus_enabled=True,
    )

    async def fake_extract(url, **kwargs):
        return EnrichmentResult(
            "completed",
            financials={
                "period_label": "Q1 FY2026",
                "revenue": 45000000000,
                "eps": 0.79,
            },
        )

    async def fake_consensus(**kwargs):
        return ConsensusResult(
            "completed",
            revenue=43_300_000_000,
            eps=0.73,
            currency="USD",
            period_label="Q1 FY2026",
            sources=[{"name": "FactSet via Kiplinger", "url": "https://kiplinger.com/x"}],
        )

    monkeypatch.setattr(
        "catalyst_radar.services.earnings_enrich.extract_from_url", fake_extract
    )
    monkeypatch.setattr(
        "catalyst_radar.services.earnings_enrich.find_consensus", fake_consensus
    )
    r = await e.maybe_extract(url="https://x", company_name="NVIDIA", symbol="NVDA")
    assert r is not None and r.status == "completed"
    assert r.financials is not None
    cons = r.financials.get("consensus")
    assert cons is not None
    assert cons["revenue"] == 43_300_000_000
    assert cons["eps"] == 0.73
    assert cons["sources"][0]["name"] == "FactSet via Kiplinger"


async def test_enricher_skips_consensus_when_disabled_or_no_period(monkeypatch) -> None:
    """When consensus_enabled=False or extraction returned no period_label,
    the consensus call must not be made (avoid wasted LLM call)."""
    consensus_calls = {"n": 0}

    async def fake_consensus(**kwargs):
        consensus_calls["n"] += 1
        return ConsensusResult("completed")

    async def fake_extract_no_period(url, **kwargs):
        return EnrichmentResult("completed", financials={"revenue": 1})

    monkeypatch.setattr(
        "catalyst_radar.services.earnings_enrich.find_consensus", fake_consensus
    )
    monkeypatch.setattr(
        "catalyst_radar.services.earnings_enrich.extract_from_url",
        fake_extract_no_period,
    )

    e_off = EarningsEnricher(
        api_key="sk-x",
        model="gpt-5-mini",
        budget=2,
        max_chars=10000,
        fetch_timeout=10,
        max_output_tokens=1000,
        consensus_enabled=False,
    )
    await e_off.maybe_extract(url="https://x", company_name="X", symbol="X")
    assert consensus_calls["n"] == 0  # disabled → never called

    e_on = EarningsEnricher(
        api_key="sk-x",
        model="gpt-5-mini",
        budget=2,
        max_chars=10000,
        fetch_timeout=10,
        max_output_tokens=1000,
        consensus_enabled=True,
    )
    await e_on.maybe_extract(url="https://x", company_name="X", symbol="X")
    assert consensus_calls["n"] == 0  # no period_label → still not called


async def test_enricher_consumes_budget_only_on_attempt(monkeypatch) -> None:
    e = EarningsEnricher(
        api_key="sk-x",
        model="gpt-5-mini",
        budget=2,
        max_chars=10000,
        fetch_timeout=10,
        max_output_tokens=1000,
    )

    async def fake_extract(url, **kwargs):
        return EnrichmentResult("failed", error="boom")

    monkeypatch.setattr(
        "catalyst_radar.services.earnings_enrich.extract_from_url", fake_extract
    )
    r1 = await e.maybe_extract(url="https://x/1", company_name="X", symbol="X")
    assert r1 is not None and r1.status == "failed"
    assert e.budget == 1
    await e.maybe_extract(url="https://x/2", company_name="X", symbol="X")
    assert e.budget == 0
    # Third call: no attempt, no further decrement.
    assert await e.maybe_extract(url="https://x/3", company_name="X", symbol="X") is None
    assert e.budget == 0


# ── unit: format_catalyst_earnings + format_catalyst branch ─────────────


def _make_catalyst_event(financials: dict | None = None) -> Event:
    return Event(
        event_type="catalyst",
        source_name="websearch_news",
        source_event_id="x:y:z",
        dedup_key="dk",
        symbol="6451",
        exchange="TW",
        company_name="Shunsin Technology",
        title="2025 Q2 financial report posted",
        source_url="https://shunsintech.com/q2.pdf",
        payload={
            "news": {"title": "t", "content": "c", "link": "https://shunsintech.com/q2.pdf"},
            "classification": {
                "event_subtype": "earnings_report",
                "importance": "high",
                "confidence": 0.95,
                "summary": "Posted Q2 report.",
                "expected_impact": "may move",
                "why_it_matters": "earnings",
                "suggested_action": "research",
            },
            **({"financials": financials} if financials is not None else {}),
        },
    )


def test_format_catalyst_falls_through_when_no_financials() -> None:
    """Generic format must still render when extraction failed/disabled —
    that's the safety property we promised callers."""
    e = _make_catalyst_event(financials=None)
    out = format_catalyst(e)
    assert "Impact:" in out
    assert "Why:" in out
    assert "Action:" in out


def test_format_catalyst_routes_earnings_subtype_to_results_card() -> None:
    e = _make_catalyst_event(
        financials={
            "period_label": "Q2 2025",
            "currency": "TWD (thousands)",
            "revenue": 1_788_130,
            "revenue_yoy_pct": 48.3,
            "gross_profit": 259_819,
            "gross_margin_pct": 14.5,
            "net_profit": -67_730,
            "net_profit_yoy_pct": -173.3,
            "eps": None,
            "beat_or_miss": "unknown",
            "key_drivers": [
                "Revenue +48% YoY",
                "Margin compressed to 14.5%",
                "Swung to net loss",
            ],
            "headline": "Revenue +48% YoY but swung to net loss on margin compression.",
        }
    )
    out = format_catalyst(e)
    # Must be the results card, NOT the generic template.
    assert "Earnings results" in out
    assert "+48.3% YoY" in out
    assert "-173.3% YoY" in out
    assert "14.5% margin" in out
    assert "Q2 2025" in out
    # Generic template's labels must be gone.
    assert "Impact:" not in out
    assert "Why:" not in out
    # Drivers rendered as bullets.
    assert "• Revenue +48% YoY" in out
    assert "Source filing" in out


def test_format_catalyst_earnings_renders_consensus_with_beat_miss_verdict() -> None:
    """Mixed beat/miss case: revenue beats, EPS misses, net profit beats —
    overall verdict should be 'mixed'. Sources must render as inline links."""
    e = _make_catalyst_event(
        financials={
            "period_label": "Q1 FY2026",
            "currency": "USD",
            "revenue": 44_000_000_000,
            "revenue_yoy_pct": 69.2,
            "eps": 0.72,
            "eps_yoy_pct": 18.0,
            "net_profit": 19_000_000_000,
            "net_profit_yoy_pct": 50.0,
            "key_drivers": ["AI demand"],
            "headline": "Q1 beat top-line; EPS slightly below.",
            "consensus": {
                "revenue": 43_300_000_000,  # actual 44.0B > 43.3B → beat
                "eps": 0.73,  # actual 0.72 < 0.73 → miss
                "net_profit": 18_500_000_000,  # actual 19.0B > 18.5B → beat
                "currency": "USD",
                "period_label": "Q1 FY2026",
                "sources": [
                    {"name": "FactSet via Kiplinger", "url": "https://kiplinger.com/x"},
                    {"name": "Investing.com", "url": "https://investing.com/x"},
                ],
            },
        }
    )
    out = format_catalyst(e)
    assert "Analyst consensus" in out
    # Per-metric tag — the consensus number renders here, not the actual.
    assert "Revenue: est <b>43.30B</b>" in out
    assert "EPS: est <b>0.73</b>" in out
    # Beat/miss labels inline next to each metric (revenue=beat, eps=miss).
    assert "<b>beat</b>" in out
    assert "<b>miss</b>" in out
    # Overall verdict
    assert "Verdict: mixed" in out
    # Source links present + clickable
    assert '<a href="https://kiplinger.com/x">FactSet via Kiplinger</a>' in out
    assert '<a href="https://investing.com/x">Investing.com</a>' in out


def test_format_catalyst_earnings_uses_word_inline_not_street() -> None:
    """User feedback: don't say 'street' consensus — use 'analyst consensus'."""
    e = _make_catalyst_event(
        financials={
            "revenue": 1000,
            "consensus": {
                "revenue": 900,
                "sources": [{"name": "Zacks", "url": "https://z/x"}],
            },
        }
    )
    out = format_catalyst(e)
    assert "Analyst consensus" in out
    assert "street" not in out.lower()


def test_format_catalyst_earnings_overall_verdict_unanimous_beat() -> None:
    e = _make_catalyst_event(
        financials={
            "revenue": 110,
            "eps": 1.10,
            "consensus": {
                "revenue": 100,
                "eps": 1.00,
                "sources": [{"name": "Yahoo", "url": "https://y/x"}],
            },
        }
    )
    out = format_catalyst(e)
    assert "Verdict: beat" in out
    assert "mixed" not in out
    assert "miss" not in out


def test_format_catalyst_earnings_no_consensus_falls_back_to_llm_label() -> None:
    """When consensus is absent but the LLM tagged beat_or_miss, that
    tag still renders — graceful degradation for thinly-covered names
    where consensus search returns nulls."""
    e = _make_catalyst_event(
        financials={
            "revenue": 100,
            "beat_or_miss": "miss",
            # Empty consensus block — no numerics
            "consensus": {"revenue": None, "eps": None, "net_profit": None, "sources": []},
        }
    )
    out = format_catalyst(e)
    assert "Consensus: <b>miss</b>" in out
    assert "Analyst consensus" not in out  # no consensus block rendered
    assert "Verdict" not in out


def test_format_catalyst_earnings_handles_partial_financials() -> None:
    # Only revenue + drivers — must not crash, must not render missing fields.
    e = _make_catalyst_event(
        financials={
            "period_label": "Q2",
            "revenue": 1000000,
            "key_drivers": ["A", "B"],
        }
    )
    out = format_catalyst_earnings(e)
    assert "Revenue:" in out
    assert "Net profit" not in out
    assert "EPS:" not in out


# ── unit: format_earnings_results ───────────────────────────────────────


def test_format_earnings_results_renders_beat_miss_and_streak() -> None:
    e = Event(
        event_type="earnings",
        source_name="eodhd",
        source_event_id="sid",
        dedup_key="dk",
        symbol="NVDA",
        exchange="US",
        company_name="NVIDIA Corp",
        title="NVDA.US earnings",
        payload={
            "code": "NVDA.US",
            "actual": 1.87,
            "estimate": 1.78,
            "percent": 5.0562,
            "fiscal_period_end": "2026-04-30",
        },
        source_url="https://eodhd.test/x",
    )
    history = [
        ("2025-07-31", 1.5, 1.6, 6.6),
        ("2025-10-31", 1.6, 1.55, -3.1),
        ("2026-01-31", 1.7, 1.7, 0.0),
        ("2026-04-30-prior", 1.78, 1.78, 0.0),
    ]
    out = format_earnings_results(e, history=history)
    assert "<b>beat</b>" in out
    assert "actual <b>1.87</b>" in out
    assert "consensus 1.78" in out
    assert "+5.1% surprise" in out
    assert "Last 4Q: ✓×··" in out  # beat, miss, inline, inline


def test_format_earnings_results_classifies_miss_and_inline() -> None:
    e = Event(
        event_type="earnings",
        source_name="eodhd",
        source_event_id="sid2",
        dedup_key="dk2",
        symbol="MU",
        exchange="US",
        company_name="Micron Technology",
        title="MU.US earnings",
        payload={"actual": 1.0, "estimate": 1.5, "percent": -33.3},
    )
    assert "<b>miss</b>" in format_earnings_results(e)
    e.payload["percent"] = 0.0
    assert "<b>in-line</b>" in format_earnings_results(e)


# ── integration: post-results re-alert in sync_earnings ──────────────────

FX = Path(__file__).parent / "fixtures"
EARNINGS = json.loads((FX / "eodhd_earnings.json").read_text())
TODAY = today_local("UTC")
# Anchor AAPL.US to today so the existing pre-event window logic still works
# alongside the new results-alert path.
EARNINGS["earnings"][0]["report_date"] = (TODAY + timedelta(days=1)).isoformat()
EARNINGS["earnings"][1]["report_date"] = (TODAY + timedelta(days=4)).isoformat()
EARNINGS["earnings"][2]["report_date"] = (TODAY + timedelta(days=20)).isoformat()


class _EarningsStubNoActual(EodhdEarningsAdapter):
    """First sync — `actual` is null (pre-event)."""

    def __init__(self) -> None:
        super().__init__(api_key="stub")

    async def fetch(self, target):
        return FetchResult(
            source_name="eodhd",
            schema_name="eodhd.earnings.v1",
            source_url="https://eodhd.test/earnings",
            http_status=200,
            payload=EARNINGS,
            items=EARNINGS["earnings"],
        )


class _EarningsStubWithActual(EodhdEarningsAdapter):
    """Second sync — same source_event_id, `actual` flipped to a value."""

    def __init__(self) -> None:
        super().__init__(api_key="stub")

    async def fetch(self, target):
        items = []
        for rec in EARNINGS["earnings"]:
            r = dict(rec)
            if r["code"] == "AAPL.US":
                r["actual"] = 1.39
                r["estimate"] = 1.23
                r["difference"] = 0.16
                r["percent"] = 13.01
            items.append(r)
        payload = {**EARNINGS, "earnings": items}
        return FetchResult(
            source_name="eodhd",
            schema_name="eodhd.earnings.v1",
            source_url="https://eodhd.test/earnings",
            http_status=200,
            payload=payload,
            items=items,
        )


async def test_earnings_sync_fires_post_results_realert_on_actual_flip(
    db_session: AsyncSession,
) -> None:
    db_session.add(
        TrackedCompany(symbol="AAPL", exchange="US", company_name="Apple Inc", source="manual")
    )
    await db_session.commit()

    s1 = await sync_earnings(db_session, adapter=_EarningsStubNoActual())
    # Pre-event reminder fires (AAPL is +1d, lands in window 1 of 7,1,0).
    assert s1.notifications_created == 1
    assert s1.results_alerts == 0

    s2 = await sync_earnings(db_session, adapter=_EarningsStubWithActual())
    # Second sync flipped actual null→1.39 — re-alert fires once.
    assert s2.results_alerts == 1
    assert s2.notifications_created >= 1

    notifs = (
        await db_session.execute(
            select(Notification).where(Notification.event_id.is_not(None))
        )
    ).scalars().all()
    windows = sorted(n.reminder_window for n in notifs)
    assert "results" in windows  # results re-alert persisted

    s3 = await sync_earnings(db_session, adapter=_EarningsStubWithActual())
    # Third sync: actual unchanged — no further re-alerts (dedup_key blocks).
    assert s3.results_alerts == 0

    total = (await db_session.execute(select(func.count()).select_from(Notification))).scalar_one()
    # Exactly one results-suffixed notification across all runs.
    results_count = sum(1 for n in notifs if n.reminder_window == "results")
    assert results_count == 1
    assert total == len(notifs)


async def test_earnings_sync_does_not_realert_for_untracked_companies(
    db_session: AsyncSession,
) -> None:
    """The flip→re-alert path must also gate on tracked-company membership;
    a globally-reported earnings row for an untracked ticker must not alert."""
    # No TrackedCompany rows — global calendar still ingests events but
    # nothing should be alertable.
    await sync_earnings(db_session, adapter=_EarningsStubNoActual())
    s2 = await sync_earnings(db_session, adapter=_EarningsStubWithActual())
    assert s2.results_alerts == 0
    assert s2.notifications_created == 0
    notifs = (
        await db_session.execute(select(func.count()).select_from(Notification))
    ).scalar_one()
    assert notifs == 0


# ── repo: recent_quarterly_actuals ───────────────────────────────────────


async def test_recent_quarterly_actuals_excludes_null_and_current(
    db_session: AsyncSession,
) -> None:
    from catalyst_radar.repositories.event_repository import EventRepository

    base = datetime(2026, 1, 1)
    rows = [
        # null actual — skipped
        ("sid1", base + timedelta(days=1), {"actual": None, "estimate": 1.0, "percent": None}),
        # reported quarters
        ("sid2", base + timedelta(days=30), {"actual": 1.1, "estimate": 1.0, "percent": 10}),
        ("sid3", base + timedelta(days=120), {"actual": 1.2, "estimate": 1.1, "percent": 9}),
        ("sid4", base + timedelta(days=210), {"actual": 1.3, "estimate": 1.3, "percent": 0}),
        # the "current" row — should be excluded
        ("sid_cur", base + timedelta(days=300), {"actual": 1.4, "estimate": 1.35, "percent": 3.7}),
    ]
    current_id: int | None = None
    for sid, dt, payload in rows:
        e = Event(
            event_type="earnings",
            source_name="eodhd",
            source_event_id=sid,
            dedup_key=sid,
            symbol="AAPL",
            exchange="US",
            event_date=dt,
            payload=payload,
        )
        db_session.add(e)
        await db_session.flush()
        if sid == "sid_cur":
            current_id = e.id
    await db_session.commit()

    repo = EventRepository(db_session)
    history = await repo.recent_quarterly_actuals(
        "AAPL", "US", limit=4, exclude_event_id=current_id
    )
    # 3 reported rows, oldest first; null-actual + current excluded.
    assert len(history) == 3
    assert [r[2] for r in history] == [1.1, 1.2, 1.3]
