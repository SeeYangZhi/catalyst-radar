"""Characterization tests for services/alerts.py Telegram formatters.

Formatters are pure functions ``Event -> HTML string`` (Telegram parse mode
HTML, whitelisted tag subset, clipped to ``_MAX_LEN``); no DB or network.
Relative-date wording (``_rel``) is pinned by monkeypatching the module's
``today_local`` to 2026-06-04 so assertions never rot with the wall clock.
"""

import re
from datetime import UTC, date, datetime, timedelta

import pytest

from catalyst_radar.models.event import Event
from catalyst_radar.services import alerts

FROZEN_TODAY = date(2026, 6, 4)

NASTY = 'A&B "Corp" <X>'
NASTY_ESCAPED = "A&amp;B &quot;Corp&quot; &lt;X&gt;"

# Telegram-HTML whitelist per the module docstring: <b> <i> <u> <s> <code>
# <pre> <blockquote> <a>. Anything else raw in the output would make
# Telegram reject the message.
_TAG_RE = re.compile(r'</?(?:b|i|u|s|code|pre|blockquote)>|<a href="[^"]*">|</a>')


@pytest.fixture(autouse=True)
def _freeze_alerts_today(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(alerts, "today_local", lambda tz: FROZEN_TODAY)


def _event(**over) -> Event:
    base = dict(
        event_type="catalyst",
        source_name="test",
        source_event_id="test:1",
        dedup_key="test:dedup:1",
        title="Test title",
        payload={},
    )
    base.update(over)
    return Event(**base)


def _assert_telegram_safe(text: str) -> None:
    """After stripping whitelisted tags, no raw angle brackets may remain."""
    residue = _TAG_RE.sub("", text)
    assert "<" not in residue, f"unescaped '<' in output: {residue!r}"
    assert ">" not in residue, f"unescaped '>' in output: {residue!r}"


# ---------------------------------------------------------------------------
# format_earnings (pre-report card)
# ---------------------------------------------------------------------------


def test_format_earnings_happy_path():
    ev = _event(
        event_type="earnings",
        symbol="MU.US",
        company_name="Micron Technology",
        event_date=datetime(2026, 6, 7, tzinfo=UTC),
        payload={
            "code": "MU",
            "report_date": "2026-06-07",
            "before_after_market": "AfterMarket",
            "fiscal_period_end": "2026-05-31",
            "estimate": 1.6,
        },
    )
    text = alerts.format_earnings(ev)
    assert "<b>Micron Technology</b>" in text
    assert "<code>$MU</code>" in text  # payload code wins over event.symbol
    assert "Reports <b>2026-06-07</b> (<b>in 3d</b>) · AfterMarket" in text
    assert "Fiscal period end: 2026-05-31" in text
    assert "Consensus EPS: <b>1.6</b>" in text
    assert text.endswith("<i>Tracked-company earnings.</i>")
    _assert_telegram_safe(text)


def test_format_earnings_missing_optionals():
    ev = _event(event_type="earnings", payload={})
    text = alerts.format_earnings(ev)
    assert text  # no crash, non-empty
    assert "<b>—</b>" in text  # name placeholder
    assert "<code>—</code>" in text  # ticker placeholder
    assert "Reports <b>—</b>" in text  # no report_date
    assert "Fiscal period end" not in text
    assert "Consensus EPS" not in text
    assert "None" not in text
    _assert_telegram_safe(text)


@pytest.mark.parametrize(
    "offset,word",
    [(0, "today"), (1, "tomorrow"), (-1, "yesterday"), (3, "in 3d"), (-3, "3d ago")],
)
def test_relative_date_words(offset: int, word: str):
    when = datetime(2026, 6, 4, tzinfo=UTC) + timedelta(days=offset)
    ev = _event(event_type="earnings", event_date=when, payload={"report_date": "x"})
    assert f"(<b>{word}</b>)" in alerts.format_earnings(ev)


# ---------------------------------------------------------------------------
# format_earnings_results (post-report card)
# ---------------------------------------------------------------------------


def test_format_earnings_results_happy_path():
    ev = _event(
        event_type="earnings",
        symbol="MU",
        company_name="Micron",
        source_url="https://eodhd.com/x?a=1&b=2",
        payload={
            "code": "MU",
            "actual": 1.91,
            "estimate": 1.60,
            "percent": 19.4,
            "fiscal_period_end": "2026-05-31",
        },
    )
    history = [
        ("2025-08-31", 1.0, 1.2, 20.0),  # beat -> ✓
        ("2025-11-30", 1.0, 0.8, -20.0),  # miss -> ×
        ("2026-02-28", 1.0, 1.0, 0.0),  # in-line -> ·
        ("2026-05-31", 1.0, None, None),  # unknown -> ·
    ]
    text = alerts.format_earnings_results(ev, history=history)
    assert "<b>Micron</b>" in text
    assert "<i>Earnings results — <b>beat</b></i>" in text
    assert "EPS: actual <b>1.91</b> vs consensus 1.6 (+19.4% surprise)" in text
    assert "Period: 2026-05-31" in text
    assert "Last 4Q: ✓×··" in text
    assert '<a href="https://eodhd.com/x?a=1&amp;b=2">Source</a>' in text
    _assert_telegram_safe(text)


@pytest.mark.parametrize(
    "percent,word",
    [(-5.0, "miss"), (0.2, "in-line"), (None, "in-line"), ("garbage", "in-line")],
)
def test_format_earnings_results_surprise_word(percent, word):
    ev = _event(event_type="earnings", payload={"actual": 1.0, "estimate": 1.0, "percent": percent})
    text = alerts.format_earnings_results(ev)
    assert f"<b>{word}</b>" in text
    if percent is None or percent == "garbage":
        assert "surprise" not in text  # no surprise suffix without a numeric percent


def test_format_earnings_results_missing_optionals():
    ev = _event(event_type="earnings", payload={})
    text = alerts.format_earnings_results(ev)
    assert text
    assert "actual <b>—</b> vs consensus —" in text
    assert "Period:" not in text
    assert "Last" not in text  # no history -> no streak line
    assert "<a " not in text  # no source_url -> no link
    _assert_telegram_safe(text)


# ---------------------------------------------------------------------------
# format_ipo — listing stage
# ---------------------------------------------------------------------------


def test_format_ipo_happy_path_listing():
    ev = _event(
        event_type="ipo",
        symbol="ABCD",
        company_name="Acme Corp",
        country="US",
        event_date=datetime(2026, 6, 5, tzinfo=UTC),
        source_url="https://example.com/prospectus",
        payload={
            "offer_price": 10,
            "shares": 5000000,
            "deal_type": "IPO",
            "filing_date": "2026-05-01",
            "amended_date": "2026-05-20",
            "exchange": "NASDAQ",
            "profile": {
                "description": "Makes widgets",
                "market_cap": 1_720_000_000,
                "deal_size": 50_000_000,
            },
        },
    )
    text = alerts.format_ipo(ev)
    assert "<b>Acme Corp</b>" in text
    assert "<code>$ABCD</code>" in text
    assert "<blockquote>Makes widgets</blockquote>" in text
    assert "Lists <b>2026-06-05</b> (<b>tomorrow</b>)" in text
    assert "$10 · Mkt cap <b>$1.72B</b> · Deal $50.00M · Shares: <b>5000000</b>" in text
    assert "Type: <b>IPO</b>" in text
    assert "Filed: 2026-05-01 · Amended: 2026-05-20 · Exchange: NASDAQ" in text
    assert '<a href="https://example.com/prospectus">Prospectus / source</a>' in text
    _assert_telegram_safe(text)


def test_format_ipo_price_range_and_computed_deal_size_hk():
    ev = _event(
        event_type="ipo",
        symbol="01234.HK",
        company_name="HK Co",
        country="HK",
        payload={
            "price_from": 8,
            "price_to": 10,
            "shares": 1_000_000,
            "app_close": "2026-06-02",
            "list_date": "2026-06-09",
        },
    )
    text = alerts.format_ipo(ev)
    assert "HK$8–10" in text  # range fallback when no offer_price
    assert "Deal HK$9.00M" in text  # midpoint 9 × 1M shares, country-derived HK$
    assert "Lists <b>2026-06-09</b>" in text  # list_date fallback when no event_date
    assert "Subscription closes 2026-06-02" in text
    _assert_telegram_safe(text)


def test_format_ipo_missing_optionals():
    ev = _event(event_type="ipo", payload={})
    text = alerts.format_ipo(ev)
    assert text
    assert "Lists <b>—</b>" in text  # no date anywhere
    assert "Mkt cap" not in text
    assert "Deal" not in text
    assert "Subscription closes" not in text
    assert "<a " not in text
    assert "blockquote" not in text  # no profile description
    assert "None" not in text
    _assert_telegram_safe(text)


def test_format_ipo_websearch_description_citations():
    ev = _event(
        event_type="ipo",
        company_name="Cited Co",
        payload={
            "profile": {
                "description": "Chip maker",
                "description_source": "websearch",
                "description_sources": [
                    {"name": "Reuters", "url": "https://r.example/a"},
                    {"url": "https://no-name.example"},  # dropped: no name
                    "not-a-dict",  # dropped: wrong type
                ],
            }
        },
    )
    text = alerts.format_ipo(ev)
    assert "<blockquote>Chip maker</blockquote>" in text
    assert '<i>Description from web:</i> <a href="https://r.example/a">Reuters</a>' in text
    assert "no-name.example" not in text
    _assert_telegram_safe(text)


# ---------------------------------------------------------------------------
# format_ipo — CSRC review stage (payload["stage"] == "review")
# ---------------------------------------------------------------------------


def test_format_ipo_review_stage_happy_path():
    ev = _event(
        event_type="ipo",
        symbol="A12345",
        company_name="Shenzhen Chips",
        country="CN",
        event_date=datetime(2026, 6, 11, tzinfo=UTC),
        source_url="http://csrc.example/cal",
        payload={
            "stage": "review",
            "status": "approved",
            "board": "STAR",
            "underwriter": "CITIC",
            "deal_size_cny": 1_500_000_000,
            "profile": {"description": "Chip maker"},
        },
    )
    text = alerts.format_ipo(ev)
    assert "<b>Shenzhen Chips</b>" in text
    assert "<code>A12345</code>" in text  # review code rendered without $ prefix
    assert "<b>CSRC review: Approved</b>" in text
    assert "<blockquote>Chip maker</blockquote>" in text
    assert "Hearing <b>2026-06-11</b> (<b>in 7d</b>)" in text
    assert "Board: <b>STAR</b> · Sponsor: CITIC · Proposed deal ¥1.50B" in text
    assert '<a href="http://csrc.example/cal">Company on Eastmoney</a>' in text
    _assert_telegram_safe(text)


def test_format_ipo_review_unknown_status_falls_back_to_cn_label():
    ev = _event(
        event_type="ipo",
        symbol="A00001",
        payload={"stage": "review", "status": None, "status_cn": "已受理", "meeting_date": "TBD"},
    )
    text = alerts.format_ipo(ev)
    assert "<b>CSRC review: 已受理</b>" in text
    assert "Hearing <b>TBD</b>" in text  # meeting_date fallback when no event_date
    _assert_telegram_safe(text)


# ---------------------------------------------------------------------------
# format_catalyst (generic news card)
# ---------------------------------------------------------------------------


def test_format_catalyst_happy_path():
    ev = _event(
        symbol="0700.HK",
        company_name="Tencent",
        source_url="https://news.example/article",
        payload={
            "classification": {
                "event_subtype": "regulatory_approval",
                "importance": "high",
                "confidence": 0.9,
                "summary": "Approved by regulator",
                "expected_impact": "positive",
                "why_it_matters": "Unlocks revenue",
                "suggested_action": "watch",
            }
        },
    )
    text = alerts.format_catalyst(ev)
    assert "<b>Tencent</b>" in text
    assert "<code>$0700.HK</code>" in text
    assert "<i>regulatory_approval · importance high · confidence 0.9</i>" in text
    assert "<blockquote>Approved by regulator</blockquote>" in text
    assert "Impact: positive" in text
    assert "Why: Unlocks revenue" in text
    assert "Action: watch" in text
    assert '<a href="https://news.example/article">Source</a>' in text
    _assert_telegram_safe(text)


def test_format_catalyst_summary_falls_back_to_title():
    ev = _event(title="Fallback headline", payload={"classification": {}})
    text = alerts.format_catalyst(ev)
    assert "<blockquote>Fallback headline</blockquote>" in text


def test_format_catalyst_missing_classification_degrades():
    ev = _event(title=None, payload={})
    text = alerts.format_catalyst(ev)
    assert text
    assert "<i>— · importance — · confidence —</i>" in text
    assert "blockquote" not in text  # no summary, no title
    assert "Impact:" not in text
    assert "Why:" not in text
    assert "Action:" not in text
    _assert_telegram_safe(text)


def test_format_catalyst_routes_earnings_report_with_financials():
    ev = _event(
        payload={
            "classification": {"event_subtype": "Earnings_Report"},  # case-insensitive route
            "financials": {"revenue": 1000},
        }
    )
    text = alerts.format_catalyst(ev)
    assert "<i>Earnings results</i>" in text  # results-card render
    assert "Revenue: <b>1.00K</b>" in text
    assert "importance" not in text  # generic template skipped


def test_format_catalyst_earnings_report_without_financials_stays_generic():
    ev = _event(payload={"classification": {"event_subtype": "earnings_report"}})
    text = alerts.format_catalyst(ev)
    assert "importance" in text  # generic template
    assert "<i>Earnings results</i>" not in text


def test_format_catalyst_affected_section():
    ev = _event(
        company_name="Prime Co",
        symbol="AAA",
        payload={
            "classification": {"summary": "s"},
            "affected": [
                {"role": "primary", "ticker": "AAA", "name": "Prime Co", "importance": "high"},
                {
                    "role": "subsidiary",
                    "ticker": "BBB",
                    "name": "Sub Co",
                    "importance": "medium",
                    "reason": "supplies <parts> & more",
                    "hop_distance": 2,
                },
            ],
        },
    )
    text = alerts.format_catalyst(ev)
    assert "<b>Affects your watchlist:</b>" in text
    assert "<code>$BBB</code> Sub Co — subsidiary (indirect) · medium" in text
    assert "<i>supplies &lt;parts&gt; &amp; more</i>" in text  # reason escaped
    _assert_telegram_safe(text)


def test_format_catalyst_affected_assessment_failed_without_rows():
    ev = _event(payload={"affected": [], "affected_assessment": {"status": "failed"}})
    text = alerts.format_catalyst(ev)
    assert "<i>(related-company impact assessment unavailable)</i>" in text


def test_format_catalyst_affected_primary_only_omitted():
    ev = _event(
        payload={"affected": [{"role": "primary", "ticker": "AAA", "importance": "high"}]}
    )
    text = alerts.format_catalyst(ev)
    assert "Affects your watchlist" not in text  # single primary row adds no info


# ---------------------------------------------------------------------------
# format_catalyst_earnings (structured-financials results card)
# ---------------------------------------------------------------------------


def test_format_catalyst_earnings_happy_path():
    ev = _event(
        symbol="MU",
        company_name="Micron",
        source_url="https://filings.example/10q",
        payload={
            "financials": {
                "period_label": "Q1 FY2026",
                "currency": "USD",
                "headline": "Record quarter",
                "revenue": 8_000_000_000,
                "revenue_yoy_pct": 38.3,
                "gross_profit": 3_000_000_000,
                "gross_margin_pct": 37.5,
                "operating_profit": 2_000_000_000,
                "net_profit": 1_790_000_000,
                "net_profit_yoy_pct": 48.27,
                "eps": 1.41,
                "eps_yoy_pct": 50.0,
                "guidance_change": "raised",
                "key_drivers": ["HBM demand", "Pricing"],
                "consensus": {
                    "revenue": 7_500_000_000,
                    "eps": 1.30,
                    "net_profit": None,
                    "sources": [{"name": "Reuters", "url": "https://r.example"}],
                },
            }
        },
    )
    text = alerts.format_catalyst_earnings(ev)
    assert "<b>Micron</b>" in text
    assert "<i>Earnings results</i> · Q1 FY2026" in text
    assert "<blockquote>Record quarter</blockquote>" in text
    assert "Revenue: <b>8.00B</b> (+38.3% YoY)" in text
    assert "Gross profit: <b>3.00B</b> · 37.5% margin" in text
    assert "Operating profit: <b>2.00B</b>" in text
    assert "Net profit: <b>1.79B</b> (+48.3% YoY)" in text
    assert "EPS: <b>1.41</b> (+50.0% YoY)" in text
    assert "<i>Currency: USD</i>" in text
    assert "<i>Analyst consensus:</i>" in text
    assert "• Revenue: est <b>7.50B</b> → <b>beat</b> (+6.7%)" in text
    assert "• EPS: est <b>1.3</b> → <b>beat</b> (+8.5%)" in text
    assert "Net profit: est" not in text  # None consensus metric skipped
    assert 'Sources: <a href="https://r.example">Reuters</a>' in text
    assert "<b>Verdict: beat</b>" in text
    assert "Guidance: raised" in text
    assert "• HBM demand" in text
    assert "• Pricing" in text
    assert '<a href="https://filings.example/10q">Source filing</a>' in text
    _assert_telegram_safe(text)


def test_format_catalyst_earnings_consensus_fallback_label():
    ev = _event(payload={"financials": {"revenue": 100, "beat_or_miss": " Beat "}})
    text = alerts.format_catalyst_earnings(ev)
    assert "Consensus: <b>beat</b>" in text  # LLM self-declared label, normalized
    assert "Analyst consensus" not in text


def test_format_catalyst_earnings_mixed_verdict():
    ev = _event(
        payload={
            "financials": {
                "revenue": 110,
                "eps": 0.9,
                "consensus": {"revenue": 100, "eps": 1.0},
            }
        }
    )
    text = alerts.format_catalyst_earnings(ev)
    assert "→ <b>beat</b> (+10.0%)" in text  # revenue
    assert "→ <b>miss</b> (-10.0%)" in text  # eps
    assert "<b>Verdict: mixed</b>" in text


def test_format_catalyst_earnings_empty_financials():
    ev = _event(company_name=None, symbol=None, title=None, payload={"financials": {}})
    text = alerts.format_catalyst_earnings(ev)
    assert text == "<b>—</b>  <code>—</code>\n<i>Earnings results</i>"


def test_format_catalyst_earnings_period_label_escaped_once():
    """period_label must be HTML-escaped exactly once — double-escaping
    renders a literal '&amp;' to the user instead of '&'."""
    ev = _event(payload={"financials": {"period_label": "H1 & H2"}})
    text = alerts.format_catalyst_earnings(ev)
    assert "H1 &amp; H2" in text
    assert "&amp;amp;" not in text


# ---------------------------------------------------------------------------
# format_unlock (share-unlock card)
# ---------------------------------------------------------------------------


def test_format_unlock_happy_path():
    ev = _event(
        symbol="300750.SZ",
        company_name="CATL",
        event_date=datetime(2026, 6, 18, tzinfo=UTC),
        payload={
            "unlock": {
                "float_date": "2026-06-18",
                "total_share": 12_500_000,
                "total_ratio": 4.5678,
                "share_type": "定向增发机构配售股份",
                "holders": [
                    {"name": "Holder D", "ratio": 0.1},
                    {"name": "Holder A", "ratio": 2.5},
                    {"name": "Holder B", "ratio": 1.25},
                    {"name": "Holder C", "ratio": 0.5},
                ],
            },
            "classification": {"importance": "medium"},
        },
    )
    text = alerts.format_unlock(ev)
    assert "<b>CATL</b>" in text
    assert "<code>$300750.SZ</code>" in text
    assert "<i>lockup_unlock · importance medium</i>" in text
    assert (
        "<blockquote>Share unlock: 12.50M shares (4.57% of float) "
        "on 2026-06-18 (in 14d).</blockquote>" in text
    )
    assert "Type: 定向增发机构配售股份" in text
    assert "Top holders:" in text
    # Top 3 by ratio, sorted descending; tail collapsed into "+N more".
    assert "  · Holder A — 2.50%" in text
    assert "  · Holder B — 1.25%" in text
    assert "  · Holder C — 0.50%" in text
    assert "Holder D" not in text
    assert "  · +1 more" in text
    _assert_telegram_safe(text)


def test_format_unlock_missing_optionals():
    ev = _event(payload={})
    text = alerts.format_unlock(ev)
    assert text
    assert "Share unlock: — shares (— of float) on —." in text
    assert "Type:" not in text
    assert "Top holders" not in text
    assert "None" not in text
    _assert_telegram_safe(text)


# ---------------------------------------------------------------------------
# HTML escaping — shared edge cases
# ---------------------------------------------------------------------------


def test_title_html_metacharacters_escaped():
    ev = _event(title='Acme <script>&"co', payload={"classification": {}})
    text = alerts.format_catalyst(ev)
    assert "<script>" not in text
    assert "&lt;script&gt;" in text
    assert "&amp;" in text
    assert "&quot;" in text
    _assert_telegram_safe(text)


@pytest.mark.parametrize(
    "formatter",
    [
        alerts.format_earnings,
        alerts.format_earnings_results,
        alerts.format_ipo,
        alerts.format_catalyst,
        alerts.format_catalyst_earnings,
        alerts.format_unlock,
    ],
    ids=lambda f: f.__name__,
)
def test_company_name_metacharacters_escaped_in_every_formatter(formatter):
    ev = _event(company_name=NASTY, title=None, payload={})
    text = formatter(ev)
    assert "<X>" not in text
    assert NASTY_ESCAPED in text
    _assert_telegram_safe(text)


def test_payload_field_metacharacters_escaped():
    ev = _event(
        event_type="ipo",
        payload={
            "exchange": "NAS<DAQ>",
            "deal_type": 'IPO & "spin-off"',
            "profile": {"description": 'Sells <gadgets> & "gizmos"'},
        },
    )
    text = alerts.format_ipo(ev)
    assert "<gadgets>" not in text
    assert "<DAQ>" not in text
    assert "Exchange: NAS&lt;DAQ&gt;" in text
    assert "Type: <b>IPO &amp; &quot;spin-off&quot;</b>" in text
    assert "<blockquote>Sells &lt;gadgets&gt; &amp; &quot;gizmos&quot;</blockquote>" in text
    _assert_telegram_safe(text)


def test_source_url_metacharacters_escaped_in_href():
    ev = _event(source_url='https://e.example/x?a=1&b="q"', payload={"classification": {}})
    text = alerts.format_catalyst(ev)
    assert '<a href="https://e.example/x?a=1&amp;b=&quot;q&quot;">Source</a>' in text
    _assert_telegram_safe(text)


# ---------------------------------------------------------------------------
# Telegram-limit clipping
# ---------------------------------------------------------------------------

_HUGE = "x" * 6000


@pytest.mark.parametrize(
    "formatter,payload",
    [
        (alerts.format_earnings, {"report_date": _HUGE}),
        (alerts.format_earnings_results, {"actual": _HUGE}),
        (alerts.format_ipo, {"profile": {"description": _HUGE}}),
        (alerts.format_catalyst, {"classification": {"summary": _HUGE}}),
        (alerts.format_catalyst_earnings, {"financials": {"headline": _HUGE}}),
        (alerts.format_unlock, {"unlock": {"share_type": _HUGE}}),
    ],
    ids=lambda v: v.__name__ if callable(v) else None,
)
def test_oversized_output_clipped_to_telegram_limit(formatter, payload):
    text = formatter(_event(payload=payload))
    assert len(text) == alerts._MAX_LEN  # clipped to exactly the safety limit
    assert text.endswith("…")
    assert alerts._MAX_LEN < 4096  # safety margin under Telegram's hard cap
