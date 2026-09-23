"""Catalyst news pipeline orchestrator.

Split per into three focused modules; this module wires them
together and stays the public entry point:

- ``catalyst_sources``  — per-source sweeps (EODHD / Eastmoney / MOPS /
  web_search) feeding items into the shared pipeline.
- ``catalyst_classify`` — per-item prefilter → dedupe gates →
  classifier driver (``_process_news_item``) + the gate/dedup configs
  and ``CatalystSyncSummary``.
- ``catalyst_enrich``   — earnings-report enrichment wiring.

Seams: tests monkeypatch ``catalyst_sync.utcnow`` to freeze the
pipeline clock (moved code resolves the clock through this module at
call time), and historically imported the moved helpers from here — all
moved names are re-exported below. Keep the re-exports in sync with the
child modules.
"""

from datetime import timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from catalyst_radar.adapters.eastmoney_news import EastmoneyNewsAdapter
from catalyst_radar.adapters.eodhd_news import EodhdNewsAdapter
from catalyst_radar.adapters.mops_announcements import MopsAnnouncementsAdapter
from catalyst_radar.adapters.websearch_news import WebSearchNewsAdapter
from catalyst_radar.config import settings
from catalyst_radar.logging import get_logger
from catalyst_radar.models.base import utcnow  # seam: conftest freezes the catalyst clock here
from catalyst_radar.repositories.company_repository import (
    TrackedCompanyRepository,
)
from catalyst_radar.repositories.event_repository import EventRepository
from catalyst_radar.repositories.notification_repository import (
    NotificationRepository,
)
from catalyst_radar.repositories.source_repository import (
    RawItemRepository,
    SourceRunRepository,
)
from catalyst_radar.services.catalyst_classify import (  # noqa: F401 — re-exported seams
    _AUTOSEND_DEMOTE_DOMAINS,
    _IMPORTANCE_RANK,
    _STORY_KEY_DATE_SUFFIX,
    CatalystSyncSummary,
    _autosend_blocked_by_domain,
    _canonical_story_key,
    _Dedup,
    _event_subtype,
    _find_semantic_twin,
    _Gate,
    _ignored_event,
    _news_dt,
    _process_news_item,
    _rank,
    _recent_notified_same_subtype,
    _similarity,
    _story_key,
    assess_primary_count,
)
from catalyst_radar.services.catalyst_enrich import (  # noqa: F401 — re-exported seam
    _ENRICH_SUBTYPES,
    build_earnings_enricher,
)
from catalyst_radar.services.catalyst_sources import (  # noqa: F401 — re-exported seams
    _sync_eastmoney_catalysts,
    _sync_eodhd_catalysts,
    _sync_mops_catalysts,
    _sync_websearch_catalysts,
    _url_probe_stats,
)
from catalyst_radar.services.earnings_enrich import (  # noqa: F401 — re-exported seam
    EarningsEnricher,
)
from catalyst_radar.services.openai_classifier import (
    OpenAIClassifier,
    OpenAIConfigError,  # noqa: F401 — re-exported seam (raised from the sweeps)
)

log = get_logger(__name__)


async def sync_catalysts(
    session: AsyncSession,
    news_adapter: EodhdNewsAdapter | None = None,
    classifier: OpenAIClassifier | None = None,
    websearch_adapter: WebSearchNewsAdapter | None = None,
    eastmoney_adapter: EastmoneyNewsAdapter | None = None,
    mops_adapter: MopsAnnouncementsAdapter | None = None,
) -> CatalystSyncSummary:
    news_adapter = news_adapter or EodhdNewsAdapter()
    classifier = classifier or OpenAIClassifier()

    tracked = await TrackedCompanyRepository(session).list_active()
    events_repo = EventRepository(session)
    notif_repo = NotificationRepository(session)
    raw_repo = RawItemRepository(session)
    runs = SourceRunRepository(session)

    from catalyst_radar.runtime_config import effective

    cfg = await effective(session)
    gate = _Gate(
        min_rank=_rank(cfg.catalyst_min_importance),
        auto_rank=_rank(cfg.catalyst_autosend_min_importance),
        min_conf=cfg.catalyst_min_confidence,
        auto_conf=cfg.catalyst_autosend_min_confidence,
        max_age_days=int(cfg.catalyst_news_max_age_days),
        drop_undated_news=bool(cfg.catalyst_drop_undated_news),
        repeat_window_hours=int(
            getattr(cfg, "catalyst_repeat_alert_window_hours", 0) or 0
        ),
    )
    dedup = _Dedup(
        window_hours=settings.catalyst_dedup_window_hours,
        title_threshold=settings.catalyst_dedup_title_threshold,
        grey_threshold=settings.catalyst_dedup_grey_threshold,
        judge_enabled=bool(cfg.catalyst_dedup_llm_judge_enabled),
        story_key_max_date_gap_days=int(settings.catalyst_story_key_max_date_gap_days),
    )
    budget = int(cfg.catalyst_max_items_per_run)
    enricher = build_earnings_enricher(cfg)

    s = CatalystSyncSummary(0, 0, 0, 0, 0, 0, 0, 0)

    budget, aborted = await _sync_eodhd_catalysts(
        session,
        tracked=tracked,
        cfg=cfg,
        gate=gate,
        dedup=dedup,
        budget=budget,
        adapter=news_adapter,
        classifier=classifier,
        events_repo=events_repo,
        notif_repo=notif_repo,
        raw_repo=raw_repo,
        runs=runs,
        s=s,
        enricher=enricher,
    )
    if aborted:
        # OpenAIConfigError: classification can't run at all — abort the
        # whole sync (no point sweeping further sources into the same wall).
        return s

    # Dated CN news (Eastmoney per-ticker) runs BEFORE web_search so its
    # items land first and the per-company URL/story dedup suppresses any
    # web_search rediscovery of the same story.
    budget = await _sync_eastmoney_catalysts(
        session,
        tracked=tracked,
        cfg=cfg,
        gate=gate,
        dedup=dedup,
        budget=budget,
        adapter=eastmoney_adapter,
        classifier=classifier,
        events_repo=events_repo,
        notif_repo=notif_repo,
        raw_repo=raw_repo,
        runs=runs,
        s=s,
        enricher=enricher,
    )

    # Taiwan material-information announcements (MOPS —) also run
    # BEFORE web_search: primary-source disclosures land first and the
    # per-company title/story dedup suppresses any web_search rediscovery.
    budget = await _sync_mops_catalysts(
        session,
        tracked=tracked,
        cfg=cfg,
        gate=gate,
        dedup=dedup,
        budget=budget,
        adapter=mops_adapter,
        classifier=classifier,
        events_repo=events_repo,
        notif_repo=notif_repo,
        raw_repo=raw_repo,
        runs=runs,
        s=s,
        enricher=enricher,
    )

    budget = await _sync_websearch_catalysts(
        session,
        tracked=tracked,
        cfg=cfg,
        gate=gate,
        dedup=dedup,
        budget=budget,
        adapter=websearch_adapter,
        classifier=classifier,
        events_repo=events_repo,
        notif_repo=notif_repo,
        raw_repo=raw_repo,
        runs=runs,
        s=s,
        enricher=enricher,
    )

    # step 4 (amended): ignored rows are load-bearing while the
    # article is still in the source feed — `get_by_source_event_id` is
    # what stops the same below-threshold article from being re-classified
    # (an LLM call) every sync cycle. They're already invisible to the UI
    # (no EventRelevance row) and to the twin scan (status filter), so the
    # only real pollution is table growth. Purge them once the article has
    # aged past the freshness gate (+ margin) — at that point a re-encounter
    # dies pre-classify on the stale/undated gate anyway.
    purged = await events_repo.delete_stale_ignored_catalysts(
        utcnow() - timedelta(days=gate.max_age_days + 7)
    )
    if purged:
        log.info("ignored_catalysts_purged", count=purged)

    await session.commit()
    log.info(
        "catalyst_sync_ok",
        fetched=s.fetched,
        classified=s.classified,
        prefiltered=s.prefiltered,
        catalysts=s.catalysts,
        autosent=s.autosent,
        review=s.review,
        skipped=s.skipped,
        deduped=s.deduped,
        enriched=s.enriched,
        propagated=s.propagated,
        propagation_dropped=s.propagation_dropped,
        eodhd_news_skipped=s.eodhd_news_skipped,
        mops_items=s.mops_items,
        errors=s.errors,
    )
    return s
