"""Telegram message formatting (HTML parse mode).

Telegram supports a limited rich subset: <b> <i> <u> <s> <code> <pre>
<blockquote> <a>. These formatters lean on that subset for scannable
cards — no emoji, no flags. The optional ``payload["profile"]`` dict
({description, market_cap, deal_size, currency}) is populated later by
the filings adapters (SEC EDGAR / HKEX prospectus); fields render only
when present.
"""

import html
from typing import Any

from catalyst_radar.config import get_settings
from catalyst_radar.models.base import today_local
from catalyst_radar.models.event import Event

_MAX_LEN = 3500  # Telegram hard limit is 4096; keep margin for safety.

_CCY = {"US": "$", "HK": "HK$", "CN": "¥", "KR": "₩"}


def _esc(value: Any) -> str:
    return html.escape(str(value)) if value is not None else "—"


def _ticker(value: Any) -> str:
    """Display a stock ticker with a leading ``$`` (e.g. ``$AAPL``)."""
    if value is None:
        return "—"
    s = str(value).strip()
    if not s:
        return "—"
    if not s.startswith("$"):
        s = f"${s}"
    return html.escape(s)


def _clip(text: str) -> str:
    return text if len(text) <= _MAX_LEN else text[: _MAX_LEN - 1] + "…"


def _ccy(event: Event) -> str:
    p = event.payload or {}
    return str(p.get("currency") or _CCY.get(str(event.country or "").upper(), "$"))


def _rel(event_date: Any) -> str | None:
    """Human countdown relative to today, e.g. 'today', 'in 3d', '2d ago'."""
    if not event_date or not hasattr(event_date, "date"):
        return None
    days = (event_date.date() - today_local(get_settings().alert_timezone)).days
    if days == 0:
        return "today"
    if days == 1:
        return "tomorrow"
    if days == -1:
        return "yesterday"
    return f"in {days}d" if days > 1 else f"{-days}d ago"


def _money(amount: Any, symbol: str = "$") -> str | None:
    """Compact money: $1.72B / HK$627M / $4.2M."""
    try:
        n = float(amount)
    except (TypeError, ValueError):
        return None
    if n <= 0:
        return None
    for div, suf in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(n) >= div:
            return f"{symbol}{n / div:.2f}{suf}"
    return f"{symbol}{n:.0f}"


def _ipo_price(p: dict[str, Any], ccy: str) -> str | None:
    if p.get("offer_price") not in (None, "", 0, "0"):
        return f"{ccy}{_esc(p.get('offer_price'))}"
    if p.get("price_from") or p.get("price_to"):
        return f"{ccy}{_esc(p.get('price_from'))}–{_esc(p.get('price_to'))}"
    return None


def _deal_size(p: dict[str, Any]) -> float | None:
    """Computable fallback for market cap: price × shares offered."""
    shares = p.get("shares")
    price = p.get("offer_price")
    if not price and (p.get("price_from") or p.get("price_to")):
        try:
            lo = float(p.get("price_from") or p.get("price_to"))
            hi = float(p.get("price_to") or p.get("price_from"))
            price = (lo + hi) / 2
        except (TypeError, ValueError):
            price = None
    try:
        return float(price) * float(shares) if price and shares else None
    except (TypeError, ValueError):
        return None


def format_earnings(event: Event) -> str:
    p = event.payload or {}
    sym = _ticker(p.get("code") or event.symbol)
    name = _esc(event.company_name or event.symbol)
    when = _esc(p.get("report_date"))
    rel = _rel(event.event_date)
    lines = [
        f"<b>{name}</b>  <code>{sym}</code>",
        f"Reports <b>{when}</b>"
        + (f" (<b>{rel}</b>)" if rel else "")
        + (f" · {_esc(p.get('before_after_market'))}" if p.get("before_after_market") else ""),
    ]
    if p.get("fiscal_period_end"):
        lines.append(f"Fiscal period end: {_esc(p.get('fiscal_period_end'))}")
    if p.get("estimate") is not None:
        lines.append(f"Consensus EPS: <b>{_esc(p.get('estimate'))}</b>")
    lines.append("<i>Tracked-company earnings.</i>")
    return _clip("\n".join(lines))


def format_earnings_results(
    event: Event,
    *,
    history: list[tuple[Any, Any, Any, Any]] | None = None,
) -> str:
    """Post-report results card — fires when EODHD's calendar refreshes a
    previously-pre-event earnings row from ``actual=null`` to a populated
    value. Renders consensus vs actual, surprise %, and an optional
    last-4-quarters beat streak (history rows are
    ``(fiscal_period_end, estimate, actual, percent)``)."""
    p = event.payload or {}
    sym = _ticker(p.get("code") or event.symbol)
    name = _esc(event.company_name or event.symbol)
    actual = p.get("actual")
    estimate = p.get("estimate")
    percent = p.get("percent")

    surprise_word = "in-line"
    try:
        pct = float(percent) if percent is not None else 0.0
        if pct > 0.5:
            surprise_word = "beat"
        elif pct < -0.5:
            surprise_word = "miss"
    except (TypeError, ValueError):
        pass

    surprise_disp = ""
    if percent is not None:
        try:
            surprise_disp = f" ({float(percent):+.1f}% surprise)"
        except (TypeError, ValueError):
            surprise_disp = ""

    lines = [
        f"<b>{name}</b>  <code>{sym}</code>",
        f"<i>Earnings results — <b>{surprise_word}</b></i>",
        f"EPS: actual <b>{_esc(actual)}</b> vs consensus {_esc(estimate)}{surprise_disp}",
    ]

    if p.get("fiscal_period_end"):
        lines.append(f"Period: {_esc(p.get('fiscal_period_end'))}")

    # Beat-streak line: last 4 quarters in chronological order, oldest first,
    # using ✓ for beats, × for misses, · for inline or unknown. Skips the
    # current row by limit/order in the caller.
    if history:
        marks: list[str] = []
        for _fpe, _est, _act, pct_row in history:
            try:
                p2 = float(pct_row) if pct_row is not None else None
            except (TypeError, ValueError):
                p2 = None
            if p2 is None:
                marks.append("·")
            elif p2 > 0.5:
                marks.append("✓")
            elif p2 < -0.5:
                marks.append("×")
            else:
                marks.append("·")
        if marks:
            lines.append(f"Last {len(marks)}Q: {''.join(marks)}")

    if event.source_url:
        lines.append(f'<a href="{_esc(event.source_url)}">Source</a>')
    return _clip("\n".join(lines))


_REVIEW_STATUS_LABEL = {
    "approved": "Approved",
    "rejected": "Rejected",
    "scheduled": "Scheduled",
    "postponed": "Postponed",
    "cancelled": "Cancelled",
}


def _profile_desc_lines(profile: dict[str, Any]) -> list[str]:
    """Company-description blockquote (+ web-source citations when the
    description came from the web_search fallback). Shared by the listing
    and CSRC-review IPO cards — review-stage alerts were going out without
    the business description even though cn_ipo_enrich had filled it in."""
    desc = profile.get("description")
    if not desc:
        return []
    lines = [f"<blockquote>{_esc(desc)}</blockquote>"]
    # Cite the web sources when the description was filled in by the
    # web_search fallback (filing path doesn't need it — the prospectus
    # link is already shown lower as "Filing").
    if profile.get("description_source") == "websearch":
        ws_sources = profile.get("description_sources") or []
        if isinstance(ws_sources, list) and ws_sources:
            cited = [
                f'<a href="{_esc(s.get("url"))}">{_esc(s.get("name"))}</a>'
                for s in ws_sources[:3]
                if isinstance(s, dict) and s.get("url") and s.get("name")
            ]
            if cited:
                lines.append("<i>Description from web:</i> " + " · ".join(cited))
    return lines


def _format_cn_ipo_review(event: Event) -> str:
    """Render CN review-stage events (CSRC IPO review committee).

    Distinct shape from listing alerts: the row represents a hearing,
    not a listing — the "ticker" is a CSRC reservation code (A-prefix),
    not a final 6-digit ticker, and the date is the hearing date. We
    surface status, board, underwriter, and proposed deal size so the
    user can judge significance at a glance."""
    p = event.payload or {}
    name = _esc(event.company_name or p.get("short_name") or p.get("code"))
    code = _esc(event.symbol or p.get("code"))
    status_label = _REVIEW_STATUS_LABEL.get(str(p.get("status")), _esc(p.get("status_cn")))

    lines = [f"<b>{name}</b>  <code>{code}</code>"]
    lines.append(f"<b>CSRC review: {status_label}</b>")
    lines.extend(_profile_desc_lines(p.get("profile") or {}))

    when = event.event_date
    date_s = (
        when.date().isoformat()
        if when and hasattr(when, "date")
        else _esc(p.get("meeting_date"))
    )
    rel = _rel(when)
    lines.append(f"Hearing <b>{date_s}</b>" + (f" (<b>{rel}</b>)" if rel else ""))

    meta: list[str] = []
    if p.get("board"):
        meta.append(f"Board: <b>{_esc(p['board'])}</b>")
    if p.get("underwriter"):
        meta.append(f"Sponsor: {_esc(p['underwriter'])}")
    ds = _money(p.get("deal_size_cny"), "¥")
    if ds:
        meta.append(f"Proposed deal {ds}")
    if meta:
        lines.append(" · ".join(meta))

    if event.source_url:
        lines.append(f'<a href="{_esc(event.source_url)}">Company on Eastmoney</a>')
    return _clip("\n".join(lines))


def format_ipo(event: Event) -> str:
    p = event.payload or {}
    if str(p.get("stage")) == "review":
        return _format_cn_ipo_review(event)
    profile = p.get("profile") or {}
    sym = _ticker(event.symbol or p.get("code"))
    name = _esc(event.company_name or p.get("code"))
    ccy = _ccy(event)

    lines = [f"<b>{name}</b>  <code>{sym}</code>"]
    lines.extend(_profile_desc_lines(profile))

    when = event.event_date
    date_s = (
        when.date().isoformat()
        if when and hasattr(when, "date")
        else _esc(p.get("start_date") or p.get("list_date"))
    )
    rel = _rel(when)
    lines.append(f"Lists <b>{date_s}</b>" + (f" (<b>{rel}</b>)" if rel else ""))

    money: list[str] = []
    price = _ipo_price(p, ccy)
    if price:
        money.append(price)
    mc = _money(profile.get("market_cap"), ccy)
    if mc:
        money.append(f"Mkt cap <b>{mc}</b>")
    ds = _money(profile.get("deal_size") or _deal_size(p), ccy)
    if ds:
        money.append(f"Deal {ds}")
    if p.get("shares"):
        money.append(f"Shares: <b>{_esc(p['shares'])}</b>")
    if p.get("deal_type"):
        money.append(f"Type: <b>{_esc(p['deal_type'])}</b>")
    if money:
        lines.append(" · ".join(money))

    meta: list[str] = []
    if p.get("filing_date"):
        meta.append(f"Filed: {_esc(p['filing_date'])}")
    if p.get("amended_date"):
        meta.append(f"Amended: {_esc(p['amended_date'])}")
    if p.get("exchange"):
        meta.append(f"Exchange: {_esc(p['exchange'])}")
    if meta:
        lines.append(" · ".join(meta))

    if p.get("app_close"):
        lines.append(f"Subscription closes {_esc(p.get('app_close'))}")
    if event.source_url:
        lines.append(f'<a href="{_esc(event.source_url)}">Prospectus / source</a>')
    return _clip("\n".join(lines))


_ROLE_LABEL = {
    "primary": "primary",
    "subsidiary": "subsidiary",
    "parent": "parent",
    "jv_partner": "JV partner",
    "shareholder_of": "shareholder",
    "related": "related",
}


def _format_affected_section(payload: dict[str, Any]) -> list[str]:
    """Render the "Affects your watchlist" block from the LLM-assessed
    spillover rows baked into ``event.payload['affected']`` by the
    catalyst sync. Rows are already sorted (primary → importance desc →
    hop asc). When the assessment ran but produced no affected rows
    beyond the primary, the block is omitted. When the assessment
    failed, a one-line footnote is appended so the user knows."""
    rows = payload.get("affected") or []
    meta = payload.get("affected_assessment") or {}
    # If only a single primary row exists, the section adds no info — skip.
    has_non_primary = any(r.get("role") != "primary" for r in rows)
    if not rows or (not has_non_primary and len(rows) == 1):
        if str(meta.get("status")) == "failed":
            return ["<i>(related-company impact assessment unavailable)</i>"]
        return []
    lines = ["", "<b>Affects your watchlist:</b>"]
    for r in rows:
        sym = _ticker(r.get("ticker"))
        name = _esc(r.get("name") or r.get("ticker") or "—")
        role = _ROLE_LABEL.get(str(r.get("role") or ""), str(r.get("role") or "—"))
        importance = _esc(r.get("importance"))
        # Hop > 1 means the candidate is reached through an intermediary
        # (e.g. subsidiary of a JV partner). Mark it so the user knows
        # the relationship is indirect.
        hop = r.get("hop_distance") or 0
        indirect = " (indirect)" if isinstance(hop, int) and hop > 1 else ""
        lines.append(
            f"  · <code>{sym}</code> {name} — {role}{indirect} · {importance}"
        )
        reason = r.get("reason")
        if reason:
            lines.append(f"     <i>{_esc(reason)}</i>")
    if str(meta.get("status")) == "failed":
        lines.append("<i>(some related-company impacts unavailable)</i>")
    return lines


def format_catalyst(event: Event) -> str:
    p = event.payload or {}
    c = p.get("classification", {}) if isinstance(p, dict) else {}
    subtype = str(c.get("event_subtype") or "").strip().lower()
    # Earnings reports with extracted financials get a results-card render
    # instead of the generic "Impact / Why / Action" template — the only
    # way the trader sees revenue/EPS/YoY at a glance. Falls through to the
    # generic format when financials are absent (extraction failed / disabled).
    if subtype == "earnings_report" and isinstance(p, dict) and p.get("financials"):
        return format_catalyst_earnings(event)
    sym = _ticker(event.symbol)
    name = _esc(event.company_name or event.symbol)
    lines = [
        f"<b>{name}</b>  <code>{sym}</code>",
        f"<i>{_esc(c.get('event_subtype'))} · importance "
        f"{_esc(c.get('importance'))} · confidence {_esc(c.get('confidence'))}</i>",
    ]
    summary = c.get("summary") or event.title
    if summary:
        lines.append(f"<blockquote>{_esc(summary)}</blockquote>")
    if c.get("expected_impact"):
        lines.append(f"Impact: {_esc(c.get('expected_impact'))}")
    if c.get("why_it_matters"):
        lines.append(f"Why: {_esc(c.get('why_it_matters'))}")
    if c.get("suggested_action"):
        lines.append(f"Action: {_esc(c.get('suggested_action'))}")
    if isinstance(p, dict):
        lines.extend(_format_affected_section(p))
    if event.source_url:
        lines.append(f'<a href="{_esc(event.source_url)}">Source</a>')
    return _clip("\n".join(lines))


def _signed_pct(value: Any) -> str | None:
    """Format a YoY % with explicit sign and one decimal: '+48.3%' / '-12.7%'."""
    try:
        n = float(value)
    except (TypeError, ValueError):
        return None
    return f"{n:+.1f}%"


def _num_compact(value: Any) -> str | None:
    """Compact numeric (no currency prefix): 1.79M / 67,730 / -190.7K."""
    try:
        n = float(value)
    except (TypeError, ValueError):
        return None
    sign = "-" if n < 0 else ""
    an = abs(n)
    for div, suf in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if an >= div:
            return f"{sign}{an / div:.2f}{suf}"
    return f"{sign}{an:.0f}"


def format_catalyst_earnings(event: Event) -> str:
    """Results-card render for an earnings_report catalyst with structured
    financials populated by ``services/earnings_enrich.py``.

    The point of this template is the *numbers* — headline, revenue + YoY,
    margin, net profit + YoY, EPS, drivers — not the meta-commentary the
    generic catalyst format produces."""
    p = event.payload or {}
    f = p.get("financials") if isinstance(p, dict) else {}
    f = f or {}
    sym = _ticker(event.symbol)
    name = _esc(event.company_name or event.symbol)

    period = _esc(f.get("period_label")) if f.get("period_label") else None
    ccy = _esc(f.get("currency")) if f.get("currency") else None
    header_bits = ["<i>Earnings results</i>"]
    if period and period != "—":
        header_bits.append(period)  # already escaped above; re-escaping doubles entities

    lines = [
        f"<b>{name}</b>  <code>{sym}</code>",
        " · ".join(header_bits),
    ]

    headline = f.get("headline")
    if headline:
        lines.append(f"<blockquote>{_esc(headline)}</blockquote>")

    # Numeric body — one line per metric where we have a value, with YoY
    # right next to it so the trader doesn't have to do mental arithmetic.
    metrics: list[str] = []
    rev = _num_compact(f.get("revenue"))
    rev_yoy = _signed_pct(f.get("revenue_yoy_pct"))
    if rev:
        metrics.append(f"Revenue: <b>{rev}</b>" + (f" ({rev_yoy} YoY)" if rev_yoy else ""))
    gm = _signed_pct(f.get("gross_margin_pct")) if f.get("gross_margin_pct") is not None else None
    gp = _num_compact(f.get("gross_profit"))
    if gp or gm:
        gm_disp = (
            f"{float(f['gross_margin_pct']):.1f}% margin"
            if f.get("gross_margin_pct") is not None
            else None
        )
        parts = [x for x in [f"Gross profit: <b>{gp}</b>" if gp else None, gm_disp] if x]
        if parts:
            metrics.append(" · ".join(parts))
    op = _num_compact(f.get("operating_profit"))
    if op:
        metrics.append(f"Operating profit: <b>{op}</b>")
    np_ = _num_compact(f.get("net_profit"))
    np_yoy = _signed_pct(f.get("net_profit_yoy_pct"))
    if np_:
        metrics.append(f"Net profit: <b>{np_}</b>" + (f" ({np_yoy} YoY)" if np_yoy else ""))
    eps = f.get("eps")
    eps_yoy = _signed_pct(f.get("eps_yoy_pct"))
    if eps is not None:
        metrics.append(f"EPS: <b>{_esc(eps)}</b>" + (f" ({eps_yoy} YoY)" if eps_yoy else ""))
    if metrics:
        if ccy:
            metrics.append(f"<i>Currency: {ccy}</i>")
        lines.append("\n".join(metrics))

    # Analyst-consensus block: only when we found at least one numeric
    # field from a reputable source. Computes beat/miss in code (deterministic
    # given actuals + consensus) rather than trusting the extraction LLM's
    # self-reported `beat_or_miss` — that field is only reliable when the
    # filing itself mentions consensus, which most quarterly reports don't.
    consensus = f.get("consensus") if isinstance(f.get("consensus"), dict) else None
    verdict = _consensus_verdict(f, consensus)
    if consensus and any(
        consensus.get(k) is not None for k in ("revenue", "eps", "net_profit")
    ):
        cons_lines = ["<i>Analyst consensus:</i>"]
        for label, actual_key, cons_key in (
            ("Revenue", "revenue", "revenue"),
            ("EPS", "eps", "eps"),
            ("Net profit", "net_profit", "net_profit"),
        ):
            cv = consensus.get(cons_key)
            av = f.get(actual_key)
            if cv is None:
                continue
            cv_disp = _num_compact(cv) if label != "EPS" else _esc(cv)
            tag = _beat_miss_tag(av, cv)
            cons_lines.append(
                f"• {label}: est <b>{cv_disp}</b>"
                + (f" → {tag}" if tag else "")
            )
        lines.append("\n".join(cons_lines))
        sources = consensus.get("sources") or []
        if isinstance(sources, list) and sources:
            cited = [
                f'<a href="{_esc(s.get("url"))}">{_esc(s.get("name"))}</a>'
                for s in sources[:3]
                if isinstance(s, dict) and s.get("url") and s.get("name")
            ]
            if cited:
                lines.append("Sources: " + " · ".join(cited))
        if verdict:
            lines.append(f"<b>Verdict: {verdict}</b>")
    else:
        # No consensus found — fall back to the LLM's self-declared label
        # if it was confident enough to assert beat/miss/inline.
        bom = str(f.get("beat_or_miss") or "").strip().lower()
        if bom in {"beat", "miss", "inline"}:
            lines.append(f"Consensus: <b>{bom}</b>")

    guidance = f.get("guidance_change")
    if guidance:
        lines.append(f"Guidance: {_esc(guidance)}")

    drivers = f.get("key_drivers") or []
    if isinstance(drivers, list) and drivers:
        items = "\n".join(f"• {_esc(str(d))}" for d in drivers[:5])
        lines.append(items)

    if isinstance(p, dict):
        lines.extend(_format_affected_section(p))

    if event.source_url:
        lines.append(f'<a href="{_esc(event.source_url)}">Source filing</a>')
    return _clip("\n".join(lines))


def _beat_miss_tag(actual: Any, consensus: Any) -> str | None:
    """One-glance comparison label: '<b>beat</b> (+5.2%)' / '<b>miss</b> (-1.3%)' /
    '<b>in-line</b>'. Returns None when either value is missing or non-numeric,
    or when consensus is zero (division undefined)."""
    try:
        a = float(actual)
        c = float(consensus)
    except (TypeError, ValueError):
        return None
    if c == 0:
        return None
    diff_pct = (a - c) / abs(c) * 100
    if diff_pct > 0.5:
        return f"<b>beat</b> ({diff_pct:+.1f}%)"
    if diff_pct < -0.5:
        return f"<b>miss</b> ({diff_pct:+.1f}%)"
    return "<b>in-line</b>"


def _consensus_verdict(financials: dict[str, Any], consensus: dict[str, Any] | None) -> str | None:
    """Roll the per-metric beat/miss into a single overall verdict —
    'beat' / 'miss' / 'mixed' / 'in-line' — so the alert has one
    bottom-line takeaway. Returns None when no comparable metric exists."""
    if not consensus:
        return None
    verdicts: list[str] = []
    for actual_key, cons_key in (
        ("revenue", "revenue"),
        ("eps", "eps"),
        ("net_profit", "net_profit"),
    ):
        cv = consensus.get(cons_key)
        av = financials.get(actual_key)
        try:
            a = float(av)
            c = float(cv)
        except (TypeError, ValueError):
            continue
        if c == 0:
            continue
        diff_pct = (a - c) / abs(c) * 100
        if diff_pct > 0.5:
            verdicts.append("beat")
        elif diff_pct < -0.5:
            verdicts.append("miss")
        else:
            verdicts.append("in-line")
    if not verdicts:
        return None
    if all(v == "beat" for v in verdicts):
        return "beat"
    if all(v == "miss" for v in verdicts):
        return "miss"
    if all(v == "in-line" for v in verdicts):
        return "in-line"
    return "mixed"


def format_unlock(event: Event) -> str:
    """Share-unlock catalyst card. The trader cares about: which company,
    how big (% of float), when, who's the holder (concentration risk).

    Payload shape comes from ``cn_unlocks_sync._build_payload``:
      {unlock: {float_date, total_share, total_ratio, share_type,
                holders: [{name, share, ratio}]}, classification: {...}}
    """
    p = event.payload or {}
    u = p.get("unlock", {}) if isinstance(p, dict) else {}
    c = p.get("classification", {}) if isinstance(p, dict) else {}
    sym = _ticker(event.symbol)
    name = _esc(event.company_name or event.symbol)
    ratio = u.get("total_ratio")
    ratio_str = f"{float(ratio):.2f}%" if isinstance(ratio, int | float) else "—"
    share_compact = _num_compact(u.get("total_share")) or "—"
    when_rel = _rel(event.event_date)
    lines = [
        f"<b>{name}</b>  <code>{sym}</code>",
        f"<i>lockup_unlock · importance {_esc(c.get('importance'))}</i>",
        f"<blockquote>Share unlock: {share_compact} shares "
        f"({ratio_str} of float) on {_esc(u.get('float_date'))}"
        f"{f' ({_esc(when_rel)})' if when_rel else ''}.</blockquote>",
    ]
    share_type = u.get("share_type")
    if share_type:
        lines.append(f"Type: {_esc(share_type)}")
    holders = u.get("holders") or []
    if holders:
        # Cap at 3 — concentration is what matters; tail-holders are noise.
        top = sorted(holders, key=lambda h: -(h.get("ratio") or 0))[:3]
        lines.append("Top holders:")
        for h in top:
            h_ratio = h.get("ratio")
            r_str = f"{float(h_ratio):.2f}%" if isinstance(h_ratio, int | float) else "—"
            lines.append(f"  · {_esc(h.get('name') or '—')} — {r_str}")
        if len(holders) > 3:
            lines.append(f"  · +{len(holders) - 3} more")
    return _clip("\n".join(lines))
