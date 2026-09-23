"""Source sweeps for the catalyst news pipeline (split).

Each ``_sync_*_catalysts`` function fetches one source family for the
tracked universe and feeds normalized items into the shared
prefilter → dedupe → classify pipeline (``_process_news_item``).
``catalyst_sync.sync_catalysts`` orchestrates them in coverage order
(EODHD → Eastmoney → MOPS → web_search) so dated primary sources land
first and the cross-source dedup suppresses web_search rediscoveries.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy.ext.asyncio import AsyncSession

from catalyst_radar.adapters.eastmoney_news import EastmoneyNewsAdapter
from catalyst_radar.adapters.eastmoney_news import supports as eastmoney_news_supports
from catalyst_radar.adapters.eodhd import EodhdConfigError
from catalyst_radar.adapters.eodhd_news import EodhdNewsAdapter, to_news_code
from catalyst_radar.adapters.mops_announcements import MopsAnnouncementsAdapter
from catalyst_radar.adapters.mops_announcements import supports as mops_supports
from catalyst_radar.adapters.websearch_news import WebSearchNewsAdapter
from catalyst_radar.logging import get_logger
from catalyst_radar.repositories.event_repository import EventRepository
from catalyst_radar.repositories.notification_repository import (
    NotificationRepository,
)
from catalyst_radar.repositories.source_repository import (
    RawItemRepository,
    SourceRunRepository,
)
from catalyst_radar.services.catalyst_classify import _process_news_item
from catalyst_radar.services.lifecycle import is_pre_ipo_cn_symbol
from catalyst_radar.services.openai_classifier import (
    OpenAIClassifier,
    OpenAIConfigError,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from catalyst_radar.services.catalyst_classify import (
        CatalystSyncSummary,
        _Dedup,
        _Gate,
    )
    from catalyst_radar.services.earnings_enrich import EarningsEnricher
    from catalyst_radar.services.url_liveness import ProbeOutcome

log = get_logger(__name__)


def _url_probe_stats(outcomes: Sequence[ProbeOutcome]) -> dict[str, Any] | None:
    """Aggregate per-URL liveness-probe outcomes  into the
    queryable shape persisted on the source run: kept/dropped totals plus
    status histograms for kept-non-2xx (the WAF-suspect keeps) and drops.
    None when nothing was probed — absence means no probes ran."""
    if not outcomes:
        return None
    kept = 0
    dropped = 0
    kept_non_2xx: dict[str, int] = {}
    dropped_status: dict[str, int] = {}
    for o in outcomes:
        key = "error" if o.status is None else str(o.status)
        if o.kept:
            kept += 1
            if o.status is None or not 200 <= o.status < 300:
                kept_non_2xx[key] = kept_non_2xx.get(key, 0) + 1
        else:
            dropped += 1
            dropped_status[key] = dropped_status.get(key, 0) + 1
    return {
        "kept": kept,
        "dropped": dropped,
        "kept_non_2xx": kept_non_2xx,
        "dropped_status": dropped_status,
    }


async def _sync_eodhd_catalysts(
    session: AsyncSession,
    *,
    tracked: list,
    cfg,
    gate: _Gate,
    dedup: _Dedup,
    budget: int,
    adapter: EodhdNewsAdapter,
    classifier: OpenAIClassifier,
    events_repo: EventRepository,
    notif_repo: NotificationRepository,
    raw_repo: RawItemRepository,
    runs: SourceRunRepository,
    s: CatalystSyncSummary,
    enricher: EarningsEnricher | None = None,
) -> tuple[int, bool]:
    """Per-ticker news sweep via EODHD's /news endpoint — the primary
    source for US / HK / KR listings. Returns ``(remaining budget,
    aborted)``; ``aborted`` is True when an ``OpenAIConfigError`` stopped
    the run, in which case the orchestrator returns the summary
    immediately without running the later sweeps."""
    # Exchanges EODHD's /news returns nothing for (Taiwan, mainland CN). Each
    # call still costs 5 EODHD units, so skip them — Taiwan is covered by the
    # web_search gap sweep and CN-listed by the Eastmoney sweep.
    eodhd_news_skip = {
        x.strip().upper()
        for x in str(getattr(cfg, "catalyst_eodhd_news_skip_exchanges", "")).split(",")
        if x.strip()
    }

    for tc in tracked:
        if budget <= 0:
            break
        # Pre-IPO names (CSRC reservation codes) have no listing ticker,
        # so EODHD's news endpoint 404s on them. Skip — web_search news
        # picks them up via company name, which is the only news
        # path that works pre-listing.
        if is_pre_ipo_cn_symbol(tc.symbol, tc.exchange):
            continue
        # EODHD has no news for these exchanges; the call would cost 5 units
        # to return []. Coverage comes from the Eastmoney / web_search sweeps.
        if (tc.exchange or "").upper() in eodhd_news_skip:
            s.eodhd_news_skipped += 1
            continue
        code = to_news_code(tc.symbol, tc.exchange)
        run = await runs.start(f"eodhd_news.{code}")
        try:
            result = await adapter.fetch(code)
        except EodhdConfigError as exc:
            s.errors += 1
            await runs.finish(run, status="skipped", last_error=str(exc))
            log.warning("catalyst_sync_skipped", code=code, error=str(exc))
            continue
        except Exception as exc:  # noqa: BLE001 - one company must not abort all
            s.errors += 1
            await runs.finish(run, status="failed", last_error=repr(exc))
            continue

        await raw_repo.store(
            source_name=result.source_name,
            schema_name=result.schema_name,
            raw_payload=result.payload,
            source_url=result.source_url,
            http_status=result.http_status,
            source_run_id=run.id,
        )
        if result.http_status != 200:
            await runs.finish(
                run,
                status="failed" if result.http_status else "empty",
                last_error=f"http {result.http_status}",
            )
            await session.commit()
            continue

        for raw in result.items:
            if budget <= 0:
                break
            news = adapter.normalize(raw)
            s.fetched += 1
            sid = adapter.source_event_id(raw)
            try:
                budget = await _process_news_item(
                    session,
                    tc=tc,
                    news=news,
                    sid=sid,
                    source_name="eodhd_news",
                    events_repo=events_repo,
                    notif_repo=notif_repo,
                    classifier=classifier,
                    gate=gate,
                    dedup=dedup,
                    s=s,
                    budget=budget,
                    cfg_obj=cfg,
                    enricher=enricher,
                )
            except OpenAIConfigError as exc:
                s.errors += 1
                await runs.finish(run, status="failed", last_error=str(exc))
                await session.commit()
                return budget, True

        await runs.finish(run, status="success", item_count=len(result.items))
        await session.commit()

    return budget, False


async def _sync_eastmoney_catalysts(
    session: AsyncSession,
    *,
    tracked: list,
    cfg,
    gate: _Gate,
    dedup: _Dedup,
    budget: int,
    adapter: EastmoneyNewsAdapter | None,
    classifier: OpenAIClassifier,
    events_repo: EventRepository,
    notif_repo: NotificationRepository,
    raw_repo: RawItemRepository,
    runs: SourceRunRepository,
    s: CatalystSyncSummary,
    enricher: EarningsEnricher | None = None,
) -> int:
    """Per-ticker CN news sweep via akshare's Eastmoney wrapper. Runs for
    tracked SSE/SZSE/BSE *listings* (pre-IPO reservation codes have no
    listing ticker, so stock_news_em 404s on them — those stay on the
    web_search path). Items carry a real publish date and flow through the
    same gate + dedup as EODHD."""
    if not bool(getattr(cfg, "catalyst_eastmoney_news_enabled", True)):
        return budget

    adapter = adapter or EastmoneyNewsAdapter()
    for tc in tracked:
        if budget <= 0:
            break
        if not eastmoney_news_supports(tc.exchange):
            continue
        if is_pre_ipo_cn_symbol(tc.symbol, tc.exchange):
            continue

        run = await runs.start(f"eastmoney_news.{tc.symbol}")
        try:
            result = await adapter.fetch(tc.symbol)
        except Exception as exc:  # noqa: BLE001 - one company must not abort all
            s.errors += 1
            await runs.finish(run, status="failed", last_error=repr(exc))
            continue

        await raw_repo.store(
            source_name=result.source_name,
            schema_name=result.schema_name,
            raw_payload=result.payload,
            source_url=result.source_url,
            http_status=result.http_status,
            source_run_id=run.id,
        )
        if result.http_status != 200:
            await runs.finish(
                run,
                status="failed" if result.http_status else "empty",
                last_error=f"http {result.http_status}",
            )
            await session.commit()
            continue

        for raw in result.items:
            if budget <= 0:
                break
            news = adapter.normalize(raw)
            s.fetched += 1
            sid = adapter.source_event_id(raw)
            try:
                budget = await _process_news_item(
                    session,
                    tc=tc,
                    news=news,
                    sid=sid,
                    source_name="eastmoney_news",
                    events_repo=events_repo,
                    notif_repo=notif_repo,
                    classifier=classifier,
                    gate=gate,
                    dedup=dedup,
                    s=s,
                    budget=budget,
                    cfg_obj=cfg,
                    enricher=enricher,
                )
            except OpenAIConfigError as exc:
                s.errors += 1
                await runs.finish(run, status="failed", last_error=str(exc))
                await session.commit()
                return budget

        await runs.finish(run, status="success", item_count=len(result.items))
        await session.commit()

    return budget


async def _sync_mops_catalysts(
    session: AsyncSession,
    *,
    tracked: list,
    cfg,
    gate: _Gate,
    dedup: _Dedup,
    budget: int,
    adapter: MopsAnnouncementsAdapter | None,
    classifier: OpenAIClassifier,
    events_repo: EventRepository,
    notif_repo: NotificationRepository,
    raw_repo: RawItemRepository,
    runs: SourceRunRepository,
    s: CatalystSyncSummary,
    enricher: EarningsEnricher | None = None,
) -> int:
    """Taiwan material-information sweep via MOPS . Runs for
    tracked TWSE / TPEx listings — the authoritative primary disclosure
    source for a market where EODHD's /news returns nothing. Items carry a
    real publish datetime (Taipei) and flow through the same gate + dedup
    as every other news source."""
    if not bool(getattr(cfg, "catalyst_mops_news_enabled", True)):
        return budget

    adapter = adapter or MopsAnnouncementsAdapter()
    for tc in tracked:
        if budget <= 0:
            break
        if not mops_supports(tc.exchange):
            continue

        run = await runs.start(f"mops_announcements.{tc.symbol}")
        try:
            result = await adapter.fetch(tc.symbol)
        except Exception as exc:  # noqa: BLE001 - one company must not abort all
            s.errors += 1
            await runs.finish(run, status="failed", last_error=repr(exc))
            continue

        await raw_repo.store(
            source_name=result.source_name,
            schema_name=result.schema_name,
            raw_payload=result.payload,
            source_url=result.source_url,
            http_status=result.http_status,
            source_run_id=run.id,
        )
        if result.http_status != 200:
            await runs.finish(
                run,
                status="failed" if result.http_status else "empty",
                last_error=f"http {result.http_status}",
            )
            await session.commit()
            continue

        for raw in result.items:
            if budget <= 0:
                break
            news = adapter.normalize(raw)
            s.fetched += 1
            s.mops_items += 1
            sid = adapter.source_event_id(raw)
            try:
                budget = await _process_news_item(
                    session,
                    tc=tc,
                    news=news,
                    sid=sid,
                    source_name="mops_announcements",
                    events_repo=events_repo,
                    notif_repo=notif_repo,
                    classifier=classifier,
                    gate=gate,
                    dedup=dedup,
                    s=s,
                    budget=budget,
                    cfg_obj=cfg,
                    enricher=enricher,
                )
            except OpenAIConfigError as exc:
                s.errors += 1
                await runs.finish(run, status="failed", last_error=str(exc))
                await session.commit()
                return budget

        await runs.finish(run, status="success", item_count=len(result.items))
        await session.commit()

    return budget


async def _sync_websearch_catalysts(
    session: AsyncSession,
    *,
    tracked: list,
    cfg,
    gate: _Gate,
    dedup: _Dedup,
    budget: int,
    adapter: WebSearchNewsAdapter | None,
    classifier: OpenAIClassifier,
    events_repo: EventRepository,
    notif_repo: NotificationRepository,
    raw_repo: RawItemRepository,
    runs: SourceRunRepository,
    s: CatalystSyncSummary,
    enricher: EarningsEnricher | None = None,
) -> int:
    """Web_search catalyst sweep. Runs for every tracked company when
    ``catalyst_websearch_all_markets`` is set (relying on cross-source dedup to
    avoid double-alerting EODHD stories), otherwise only the gap exchanges.
    Items flow through the same gate + dedup as EODHD.

    for the gap exchanges (Taiwan/CN-pre-IPO), web_search is the *only*
    news source — EODHD's /news returns nothing for them. So when the broad
    master switch is off we still run the sweep restricted to those exchanges
    (``catalyst_websearch_gap_always_on``, on by default); the expensive
    all-markets sweep stays opt-in behind ``catalyst_websearch_enabled``."""
    gap_only = False
    if not cfg.catalyst_websearch_enabled:
        if not bool(getattr(cfg, "catalyst_websearch_gap_always_on", True)):
            return budget
        gap_only = True  # gap exchanges only — their sole news source

    adapter = adapter or WebSearchNewsAdapter()
    if not adapter.configured:
        log.warning("websearch_catalysts_skipped", reason="openai not configured")
        return budget

    # Lookback is runtime-editable; the adapter snapshots the static
    # settings value at construction, so re-apply the effective() value
    # here for edits to land without a restart.
    adapter.lookback_days = int(
        getattr(cfg, "catalyst_websearch_lookback_days", adapter.lookback_days)
        or adapter.lookback_days
    )

    # In gap-only mode the broad all-markets sweep is forced off — the
    # per-company filter below restricts to the gap exchanges.
    all_markets = bool(cfg.catalyst_websearch_all_markets) and not gap_only
    eastmoney_on = bool(getattr(cfg, "catalyst_eastmoney_news_enabled", True))
    gap = {
        x.strip().upper()
        for x in str(cfg.catalyst_websearch_gap_exchanges).split(",")
        if x.strip()
    }

    for tc in tracked:
        if budget <= 0:
            break
        # CN *listed* names are covered by the dated Eastmoney sweep above;
        # web_search here is reserved for CN pre-IPO names (no listing code,
        # so Eastmoney/EODHD can't reach them) and non-CN gap markets. This
        # is the actual reduction in web_search reliance — it no longer
        # rediscovers CN listed-company pages, the source of the
        # undated-product-page false positives (Unitree-class). Only ceded
        # when the Eastmoney sweep is enabled — otherwise web_search stays
        # the fallback so CN listed names aren't left with no news at all.
        if (
            eastmoney_on
            and eastmoney_news_supports(tc.exchange)
            and not is_pre_ipo_cn_symbol(tc.symbol, tc.exchange)
        ):
            continue
        if not all_markets and tc.exchange.upper() not in gap:
            continue

        run = await runs.start(f"websearch_news.{tc.symbol}.{tc.exchange}")
        try:
            result = await adapter.fetch(
                company_name=tc.company_name, symbol=tc.symbol, exchange=tc.exchange
            )
        except Exception as exc:  # noqa: BLE001 - one company must not abort all
            s.errors += 1
            await runs.finish(run, status="failed", last_error=repr(exc))
            continue

        await raw_repo.store(
            source_name=adapter.source_name,
            schema_name=adapter.schema_name,
            raw_payload=result.raw,
            source_url=result.source_url,
            http_status=200 if result.status == "completed" else 0,
            source_run_id=run.id,
        )
        if result.status != "completed":
            s.errors += 1
            await runs.finish(run, status="failed", last_error=result.error)
            await session.commit()
            continue

        for news in result.items:
            if budget <= 0:
                break
            s.fetched += 1
            sid = adapter.source_event_id(news)
            try:
                budget = await _process_news_item(
                    session,
                    tc=tc,
                    news=news,
                    sid=sid,
                    source_name="websearch_news",
                    events_repo=events_repo,
                    notif_repo=notif_repo,
                    classifier=classifier,
                    gate=gate,
                    dedup=dedup,
                    s=s,
                    budget=budget,
                    cfg_obj=cfg,
                    enricher=enricher,
                )
            except OpenAIConfigError as exc:
                s.errors += 1
                await runs.finish(run, status="failed", last_error=str(exc))
                await session.commit()
                return budget

        probe_stats = _url_probe_stats(result.url_probes)
        await runs.finish(
            run,
            status="success",
            item_count=len(result.items),
            summary={"url_probes": probe_stats} if probe_stats else None,
        )
        await session.commit()

    return budget
