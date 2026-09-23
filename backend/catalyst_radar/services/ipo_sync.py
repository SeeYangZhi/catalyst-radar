import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from catalyst_radar.adapters.eodhd import EodhdConfigError
from catalyst_radar.adapters.eodhd_calendar import EodhdIpoAdapter, default_window
from catalyst_radar.config import settings
from catalyst_radar.dedup import dedup_key as make_dedup_key
from catalyst_radar.logging import get_logger
from catalyst_radar.models.base import as_utc, today_local, utcnow
from catalyst_radar.models.event import Event, EventRelevance
from catalyst_radar.models.notification import Notification
from catalyst_radar.repositories.config_repository import ConfigRepository
from catalyst_radar.repositories.event_repository import EventRepository
from catalyst_radar.repositories.notification_repository import (
    NotificationRepository,
)
from catalyst_radar.repositories.source_repository import (
    RawItemRepository,
    SourceRunRepository,
)
from catalyst_radar.services.alerts import format_ipo

log = get_logger(__name__)

# EODHD IPO `exchange` is free text (e.g. "HKSE", "Shanghai"); map to an
# ISO country. Keys are matched case-insensitively (see country_for_exchange).
# Unmapped exchanges resolve to None and are never alerted.
_EXCHANGE_COUNTRY = {
    # United States
    "NASDAQ": "US",
    "NYSE": "US",
    "NYSE AMERICAN": "US",
    "NYSEARCA": "US",
    "AMEX": "US",
    "OTC": "US",
    "US": "US",
    # Hong Kong — EODHD emits "HKSE" (not "HKEX"); the others are defensive.
    "HKSE": "HK",
    "HKEX": "HK",
    "SEHK": "HK",
    "HK": "HK",
    # Mainland China
    "SHANGHAI": "CN",
    "SHENZHEN": "CN",
    "BEIJING": "CN",
    "SSE": "CN",
    "SZSE": "CN",
    "BSE": "CN",
    "CN": "CN",
    # South Korea
    "KRX": "KR",
    "KOSDAQ": "KR",
    "KOSPI": "KR",
    "KO": "KR",
    "KQ": "KR",
}


@dataclass(slots=True)
class IpoSyncSummary:
    fetched: int
    events_created: int
    matched: int
    notifications_created: int


def country_for_exchange(exchange: str | None) -> str | None:
    if not exchange:
        return None
    return _EXCHANGE_COUNTRY.get(exchange.strip().upper())


def _enabled_countries(spec: str) -> set[str]:
    return {c.strip().upper() for c in spec.split(",") if c.strip()}


# Token-level exclusions: matched as whole space/punct-delimited words so
# real operating companies ("United Airlines", "FundVantage Aerospace")
# aren't false-positived by substring overlap. Covers SPACs (acquisition
# corps), investment vehicles, ETF/ETN/trust/fund/series, and the
# derivative classes (warrants/rights/units) that frequently sneak onto
# EODHD's IPO calendar but aren't real operating-company listings.
_EXCLUDED_TOKENS = frozenset(
    {
        "acquisition",
        "etf",
        "etn",
        "fund",
        "funds",
        "investors",
        "investments",
        "rights",
        "series",
        "trust",
        "unit",
        "units",
        "warrant",
        "warrants",
    }
)

# Multi-word phrases (no clean token boundary). Kept tiny on purpose.
_EXCLUDED_PHRASES = ("when issued",)

_TOKEN_SPLIT_RE = re.compile(r"[^a-z0-9]+")


def _etf_trust_indicators(name: str | None) -> bool:
    """Return True if the company name is an instrument we don't want to
    alert on: ETF/ETN/trust/fund, SPAC acquisition corp, investment
    vehicle, or a warrant/right/unit class.

    The events are still ingested and stored — this only gates alert
    creation (mirrors the existing :func:`_industry_matches` flow)."""
    if not name:
        return False
    low = name.lower()
    if any(p in low for p in _EXCLUDED_PHRASES):
        return True
    tokens = _TOKEN_SPLIT_RE.split(low)
    return any(t in _EXCLUDED_TOKENS for t in tokens)


def _industry_matches(description: str | None, keywords: list[str]) -> bool:
    """Return True if description matches any industry keyword."""
    if not keywords:
        return True
    if not description:
        return False
    low = description.lower()
    return any(k in low for k in keywords)


def _exchange_allowed(exchange: str | None, allowed: set[str]) -> bool:
    """Return True if exchange is in the allowed set (or no filter set)."""
    if not allowed:
        return True
    if not exchange:
        return False
    return exchange.strip().upper() in allowed


def _deal_size_usd(payload: dict[str, Any]) -> float:
    """Extract deal size in USD from payload, or 0 if unknown."""
    # EODHD provides offer_price and shares, or price_from/price_to range
    shares = payload.get("shares") or 0
    price = payload.get("offer_price") or 0
    if not price and shares:
        # Use midpoint of price range if available
        price_from = payload.get("price_from") or 0
        price_to = payload.get("price_to") or 0
        if price_from and price_to:
            price = (float(price_from) + float(price_to)) / 2
        elif price_from:
            price = float(price_from)
        elif price_to:
            price = float(price_to)
    if price and shares:
        return float(price) * float(shares)
    # Fallback: profile.market_cap or profile.deal_size
    profile = payload.get("profile") or {}
    return float(profile.get("market_cap") or profile.get("deal_size") or 0)


def _ipo_windows(spec: str) -> list[int]:
    return sorted(
        {int(w) for w in spec.split(",") if w.strip().lstrip("-").isdigit()},
        reverse=True,
    )


def _pick_window(days_until: int, windows: list[int]) -> int | None:
    candidates = [w for w in windows if 0 <= days_until <= w]
    return min(candidates) if candidates else None


def _needs_profile(event: Event) -> bool:
    """True if the event still lacks a company description. Re-attempt even
    when a prior run set ``profile.checked=True`` — the prospectus/EDGAR path
    can mark 'checked' but leave the description empty (ToC/image-only PDF,
    parse failure), and a later web_search fallback may succeed where it
    didn't. Shared by the US/HK/CN enrichers."""
    prof = (event.payload or {}).get("profile") or {}
    return not prof.get("description")


def _needs_upgrade(event: Event) -> bool:
    """True for a provisional web_search blurb that hasn't yet had the
    authoritative prospectus/EDGAR pass. Tracked by ``prospectus_attempted``
    so the canonical fetch runs at most once even when it stays unavailable."""
    prof = (event.payload or {}).get("profile") or {}
    return prof.get("description_source") == "websearch" and not prof.get("prospectus_attempted")


async def select_enrich_candidates(
    session: AsyncSession,
    rows: Sequence[Event],
    *,
    budget: int,
    cap: int = 40,
    now: datetime | None = None,
    window_end: datetime | None = None,
) -> list[Event]:
    """Order description-enrichment candidates so events about to be alerted
    can't lose the per-run budget race (a source recovering after an outage
    mints several events at once; under a plain ``[:budget]`` clip some of
    their alerts ship without a description).

    "Imminent" means the event has a *pending* notification — the precise
    definition of about-to-be-dispatched — or, when ``window_end`` is given
    (CN keeps its listing-window heuristic for rows whose notifications
    aren't minted yet), an ``event_date`` between ``now`` and ``window_end``.
    All imminent rows are covered first (bounded by ``cap``); any remaining
    budget drains the backlog in the caller's row order. Shared by the
    US/HK/CN enrichers."""
    pending_ids = set(
        (
            await session.execute(
                select(Notification.event_id).where(
                    Notification.status == "pending",
                    Notification.event_id.is_not(None),
                )
            )
        )
        .scalars()
        .all()
    )
    now = now or utcnow()
    need_desc = [e for e in rows if _needs_profile(e)]
    need_upg = [e for e in rows if _needs_upgrade(e)]

    def _imminent(e: Event) -> bool:
        return e.id in pending_ids or (
            window_end is not None
            and (d := as_utc(e.event_date)) is not None
            and now <= d <= window_end
        )

    # Priority tiers: imminent-no-desc → imminent-upgrade → backlog-no-desc.
    # (Backlog upgrades are left to a future run — never urgent.)
    imm_desc = [e for e in need_desc if _imminent(e)]
    imm_upg = [e for e in need_upg if _imminent(e)]
    backlog = [e for e in need_desc if not _imminent(e)]
    ordered = imm_desc + imm_upg + backlog
    take = min(cap, max(budget, len(imm_desc) + len(imm_upg)))
    return ordered[:take]


async def _enrich_description(
    adapter: EodhdIpoAdapter, code: str | None
) -> str | None:
    """Fetch a one-line company description from EODHD fundamentals API."""
    if not code or not adapter.api_key:
        return None
    try:
        url = f"{adapter.base_url}/fundamentals/{code}"
        params = {"api_token": adapter.api_key, "fmt": "json"}
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(url, params=params)
        if resp.status_code != 200:
            return None
        data = resp.json()
        raw = (data.get("General") or {}).get("Description")
        desc = str(raw).strip() if isinstance(raw, str) else ""
        # Store the full company description. The Telegram message as a
        # whole is still bounded by alerts._clip; an individual blurb is
        # not pre-truncated so detail cards show the complete summary.
        return desc or None
    except Exception:  # noqa: BLE001
        return None


def _set_profile(event: Event, key: str, value: str | bool | None) -> None:
    """Set a field inside event.payload['profile'] without clobbering."""
    if value is None:
        return
    p = dict(event.payload or {})
    prof = dict(p.get("profile") or {})
    prof[key] = value
    p["profile"] = prof
    event.payload = p


async def sync_ipos(
    session: AsyncSession,
    adapter: EodhdIpoAdapter | None = None,
) -> IpoSyncSummary:
    adapter = adapter or EodhdIpoAdapter()
    runs = SourceRunRepository(session)
    run = await runs.start("eodhd.ipos")

    try:
        result = await adapter.fetch(default_window(settings.eodhd_ipo_lookahead_days))
    except EodhdConfigError as exc:
        await runs.finish(run, status="skipped", last_error=str(exc))
        log.warning("ipo_sync_skipped", error=str(exc))
        return IpoSyncSummary(0, 0, 0, 0)
    except Exception as exc:  # noqa: BLE001
        await runs.finish(run, status="failed", last_error=repr(exc))
        log.warning("ipo_sync_failed", error=repr(exc))
        return IpoSyncSummary(0, 0, 0, 0)

    await RawItemRepository(session).store(
        source_name=result.source_name,
        schema_name=result.schema_name,
        raw_payload=result.payload,
        source_url=result.source_url,
        http_status=result.http_status,
        source_run_id=run.id,
    )

    if result.http_status != 200:
        await runs.finish(run, status="failed", last_error=f"http {result.http_status}")
        await session.commit()
        return IpoSyncSummary(0, 0, 0, 0)

    from catalyst_radar.runtime_config import effective

    eff = await effective(session)
    enabled = _enabled_countries(eff.eodhd_ipo_enabled_countries)
    sector_cfg = await ConfigRepository(session).get("ipo_sector_keywords")
    keywords = [k.lower() for k in sector_cfg] if isinstance(sector_cfg, list) else []

    # New filter configs
    exclude_etfs = eff.ipo_exclude_etfs_trusts
    industry_kw = [k.strip().lower() for k in eff.ipo_industry_keywords.split(",") if k.strip()]
    min_deal_size = eff.ipo_min_deal_size_usd
    exchange_filter = _enabled_countries(eff.ipo_exchange_filter)

    events_repo = EventRepository(session)
    notif_repo = NotificationRepository(session)
    windows = _ipo_windows(eff.ipo_alert_windows_days)
    today = today_local(eff.alert_timezone)

    events_created = matched = notifications_created = 0
    filtered_out = {"etf": 0, "industry": 0, "deal_size": 0, "exchange": 0}

    for raw in result.items:
        norm = adapter.normalize(raw)
        country = country_for_exchange(norm["exchange"])
        # EODHD is the US source. HK comes from AAStocks (richer schedule
        # data), and EODHD's HK rows have known data-quality issues (e.g.
        # filing_date == start_date). Skipping non-US rows here keeps the
        # IPO surface clean and avoids duplicate events per listing.
        if country != "US":
            continue
        sid = adapter.source_event_id(raw)
        event = Event(
            event_type="ipo",
            source_name=adapter.source_name,
            source_event_id=sid,
            dedup_key=adapter.dedup_key(raw),
            symbol=norm["symbol"],
            exchange=norm["exchange"],
            country=country,
            company_name=norm["company_name"],
            title=norm["title"],
            event_date=norm["event_date"],
            payload=norm["payload"],
        )
        event, created = await events_repo.upsert(event)
        if created:
            events_created += 1
            # Enrich new IPOs with company description from EODHD fundamentals
            code = norm["payload"].get("code")
            desc = await _enrich_description(adapter, code)
            if desc:
                _set_profile(event, "description", desc)
                session.add(event)

        # Country gating is driven solely by the enabled set (config /
        # IPO Filters UI). Unmapped exchanges (country is None) never match.
        if country is None or country not in enabled:
            continue
        if keywords:
            haystack = f"{event.company_name or ''}".lower()
            if not any(k in haystack for k in keywords):
                continue

        # --- New filters ---
        # 1. ETF/Trust exclusion
        if exclude_etfs and _etf_trust_indicators(event.company_name):
            filtered_out["etf"] += 1
            continue

        # 2. Exchange filter
        if not _exchange_allowed(event.exchange, exchange_filter):
            filtered_out["exchange"] += 1
            continue

        # 3. Industry keyword filter (matches company name or description)
        if industry_kw:
            desc = ((event.payload or {}).get("profile") or {}).get("description") or ""
            haystack = f"{event.company_name or ''} {desc}".lower()
            if not _industry_matches(haystack, industry_kw):
                filtered_out["industry"] += 1
                continue

        # 4. Minimum deal size
        if min_deal_size > 0:
            deal_size = _deal_size_usd(event.payload or {})
            if deal_size > 0 and deal_size < min_deal_size:
                filtered_out["deal_size"] += 1
                continue

        matched += 1
        session.add(
            EventRelevance(
                event_id=event.id,
                matched=True,
                reason=f"IPO in enabled country {country}",
            )
        )

        if event.event_date is None:
            continue
        days_until = (event.event_date.date() - today).days
        window = _pick_window(days_until, windows)
        if window is None:
            continue

        dkey = make_dedup_key(sid, "ipo", str(window))
        if await notif_repo.get_by_dedup_key(dkey) is not None:
            continue
        session.add(
            Notification(
                event_id=event.id,
                channel="telegram",
                dedup_key=dkey,
                reminder_window=str(window),
                status="pending",
                payload={"text": format_ipo(event)},
                dispatch_after=utcnow() + timedelta(minutes=int(eff.ipo_dispatch_grace_minutes)),
            )
        )
        notifications_created += 1

    await session.commit()
    await runs.finish(run, status="success", item_count=len(result.items))
    await session.commit()
    log.info(
        "ipo_sync_ok",
        fetched=len(result.items),
        events_created=events_created,
        matched=matched,
        notifications=notifications_created,
        filtered=filtered_out,
    )
    return IpoSyncSummary(
        fetched=len(result.items),
        events_created=events_created,
        matched=matched,
        notifications_created=notifications_created,
    )
