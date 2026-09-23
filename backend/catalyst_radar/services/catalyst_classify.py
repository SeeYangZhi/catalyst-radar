"""Per-item catalyst pipeline: prefilter → dedupe gates → classifier (split).

``_process_news_item`` is the shared driver every source sweep
(``catalyst_sources``) feeds normalized items into: freshness/undated
gates, deterministic prefilter, cross-source URL dedup, the OpenAI
classifier, semantic same-story merge, the repeat-alert window, spillover
assessment, and event + notification creation.

Clock: tests freeze the catalyst pipeline's notion of "now" by
monkeypatching ``catalyst_sync.utcnow`` (autouse conftest fixture), so all
call sites here resolve the clock through that module attribute at call
time (``_utcnow``) instead of binding ``models.base.utcnow`` at import
time.
"""

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from rapidfuzz import fuzz
from sqlalchemy.ext.asyncio import AsyncSession

from catalyst_radar.config import settings
from catalyst_radar.dedup import content_dedup_key, content_text_key
from catalyst_radar.dedup import dedup_key as make_dedup_key
from catalyst_radar.models.base import as_utc as _as_utc
from catalyst_radar.models.classifier import ClassifierRun
from catalyst_radar.models.event import Event, EventRelevance
from catalyst_radar.models.notification import Notification
from catalyst_radar.repositories.event_repository import EventRepository
from catalyst_radar.repositories.notification_repository import (
    NotificationRepository,
)
from catalyst_radar.services.affected_companies import assess_affected_companies
from catalyst_radar.services.alerts import format_catalyst
from catalyst_radar.services.catalyst_enrich import maybe_enrich_earnings
from catalyst_radar.services.catalyst_prefilter import prefilter_reason
from catalyst_radar.services.earnings_enrich import EarningsEnricher
from catalyst_radar.services.openai_classifier import OpenAIClassifier

_IMPORTANCE_RANK = {"low": 0, "medium": 1, "high": 2}

# URL-substring patterns for aggregator / opinion / listicle domains.
# These sources mostly recycle real news as "X stocks to buy"-style
# commentary; the classifier sometimes reads the embedded fact as
# fresh, producing a HIGH/0.96 false-positive catalyst. We don't drop
# their articles — they're sent to the review queue instead of
# auto-firing to Telegram, so the user can promote the rare gem.
_AUTOSEND_DEMOTE_DOMAINS: tuple[str, ...] = (
    "finance.yahoo.com/markets/stocks/articles",
    "finance.yahoo.com/news",
    "insidermonkey.com",
    "fool.com",
    "seekingalpha.com",
    "247wallst.com",
    "thestreet.com",
    "benzinga.com",
    "zacks.com/stock/news",
    "nasdaq.com/articles",
    "investorplace.com",
    "marketbeat.com",
    "barchart.com",
)


def _utcnow() -> datetime:
    """The catalyst pipeline clock, resolved through ``catalyst_sync.utcnow``
    at call time so the conftest clock-freeze monkeypatch keeps working."""
    from catalyst_radar.services import catalyst_sync

    return catalyst_sync.utcnow()


def _autosend_blocked_by_domain(url: str | None) -> str | None:
    """Return the matched aggregator domain substring if the article
    URL is from an opinion / listicle source we don't trust to autosend
    on; otherwise None. Used to demote autosend → review for these
    sources even when the classifier is confident."""
    if not url:
        return None
    u = url.lower()
    for needle in _AUTOSEND_DEMOTE_DOMAINS:
        if needle in u:
            return needle
    return None


@dataclass(slots=True)
class CatalystSyncSummary:
    fetched: int
    classified: int
    prefiltered: int
    catalysts: int
    autosent: int
    review: int
    skipped: int
    errors: int
    deduped: int = 0  # cross-source URL duplicates suppressed (no second alert)
    enriched: int = 0  # earnings/guidance items that got structured financials
    propagated: int = 0  # events that surfaced 1+ affected non-primary listing
    propagation_dropped: int = 0  # parent_only events with no affected listings
    eodhd_news_skipped: int = 0  # tracked cos skipped (no EODHD news coverage)
    mops_items: int = 0  # TW material-information items fetched from MOPS


def _rank(value: str | None) -> int:
    return _IMPORTANCE_RANK.get(str(value or "").lower(), -1)


def _news_dt(news: dict) -> datetime | None:
    """Parse the EODHD news ISO timestamp into a tz-aware datetime."""
    raw = news.get("date")
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


async def _ignored_event(
    repo: EventRepository,
    *,
    sid: str,
    tc,
    news: dict,
    reason: str,
    source_name: str = "eodhd_news",
) -> None:
    event = Event(
        event_type="catalyst",
        source_name=source_name,
        source_event_id=sid,
        dedup_key=make_dedup_key(sid),
        symbol=tc.symbol,
        exchange=tc.exchange,
        company_name=tc.company_name,
        title=news.get("title"),
        event_date=_news_dt(news),
        source_url=news.get("link"),
        status="ignored",
        payload={"news": news, "ignore_reason": reason},
    )
    await repo.upsert(event)


@dataclass(slots=True)
class _Gate:
    min_rank: int
    auto_rank: int
    min_conf: float
    auto_conf: float
    max_age_days: int
    # Web_search occasionally surfaces long-lived static product pages
    # (e.g. unitree.com/cn/H2/, listed since WAIC 2025) with no
    # ``published_date``. The stale-news age gate is silently skipped for
    # undated items, so they slip through and get alerted as "fresh
    # launches" months after the actual launch. When this is true we drop
    # undated items outright — the dominant pattern for these false
    # positives is self-published product pages with no in-page date,
    # which a real news article would have.
    drop_undated_news: bool
    # step 3: repeat-alert window (hours). A second autosend-grade
    # catalyst for the same (symbol, subtype) within this window of an
    # already-notified event is demoted to review. 0 disables.
    repeat_window_hours: int = 0


@dataclass(slots=True)
class _Dedup:
    window_hours: int
    title_threshold: int
    grey_threshold: int
    judge_enabled: bool
    story_key_max_date_gap_days: int


def _event_subtype(payload: dict | None) -> str | None:
    sub = ((payload or {}).get("classification") or {}).get("event_subtype")
    return (str(sub).strip().lower() or None) if sub else None


# Strip trailing `_YYYY-MM-DD` / `_YYYY-MM` / `_YYYY` from a story_key.
# The v3 prompt forbids dates in story_key (re-reporting on day N+1 was
# minting a new key for the same event); this normalizer makes legacy v2
# dated keys still dedup against fresh v3 date-less keys during rollout.
_STORY_KEY_DATE_SUFFIX = re.compile(r"_(\d{4}(?:-\d{2}(?:-\d{2})?)?)$")


def _canonical_story_key(raw: str | None) -> str | None:
    s = str(raw or "").strip().lower()
    if not s:
        return None
    return _STORY_KEY_DATE_SUFFIX.sub("", s)


def _story_key(payload: dict | None) -> str | None:
    key = ((payload or {}).get("classification") or {}).get("story_key")
    return _canonical_story_key(key)


def _similarity(title: str, summary: str, candidate: Event) -> int:
    """Best fuzzy match of the new item's title/summary against a candidate.

    The classifier's normalized summary often matches across two articles about
    the same event better than the raw headlines do, so we score both."""
    cand_summary = ((candidate.payload or {}).get("classification") or {}).get("summary") or ""
    scores = [fuzz.token_set_ratio(title, candidate.title or "")]
    if summary and cand_summary:
        scores.append(fuzz.token_set_ratio(summary, cand_summary))
    return int(max(scores))


async def _find_semantic_twin(
    events_repo: EventRepository,
    classifier: OpenAIClassifier,
    dedup: _Dedup,
    *,
    symbol: str,
    exchange: str,
    subtype: str | None,
    title: str,
    summary: str,
    story_key: str | None,
    event_date: datetime | None,
    now: datetime,
) -> Event | None:
    """Find a recent catalyst event that is the SAME story as this candidate.

    Scoped to one company (symbol AND exchange). Three layered checks, in
    order of cost / precision:

    1. story_key exact match (with event-date proximity guard) — the
       classifier emits a canonical event identifier (e.g.
       ``a25310_ipo_review_pass``) that collapses paraphrase and language.
       Fast, deterministic, no LLM call. To prevent over-collapse when
       the same counterparty announces multiple distinct deals within
       the 72h window, we additionally require the new item's
       ``event_date`` to be within ``story_key_max_date_gap_days`` of
       the candidate's. Missing date on either side falls through to
       merge — trust the canonical key when we have no date signal.
    2. fuzzy title/summary similarity — strong match auto-merges; grey
       zone forwards to the LLM judge (1 call). Best-scoring candidate
       wins; subtype-match is a tiebreaker.
    3. subtype fallback — when nothing clears the grey bar but there IS
       a same-(symbol, subtype) candidate in window, still judge it.
       Salvages the remaining cross-language cases when v1-era events
       (no story_key) are still in window."""
    since = now - timedelta(hours=dedup.window_hours)
    candidates = await events_repo.list_recent_catalysts_for_company(symbol, exchange)
    # `list_recent_catalysts_for_company` returns created_at-DESC, so the
    # first survivor of the window filter is also the most recent for any
    # subtype/story_key match we accumulate below.
    in_window: list[Event] = [
        c for c in candidates
        if _as_utc(c.created_at) is None or _as_utc(c.created_at) >= since
    ]

    if story_key:
        max_gap = timedelta(days=dedup.story_key_max_date_gap_days)
        new_evt = _as_utc(event_date)
        # The deterministic story_key fast-path is gated by event_date
        # proximity (max_gap below), so its candidate scan can safely reach
        # further back than the fuzzy/judge window_hours: a slow source
        # (web_search) routinely rediscovers a days-old story after the
        # original has aged past window_hours, and an identical-story_key
        # straggler would otherwise mint a duplicate (two identical-headline
        # MU board-appointment events, ingested ~74h apart, both stuck in
        # review). Scan at least the proximity horizon; the fuzzy/judge
        # layers below stay bounded to window_hours (in_window).
        story_since = now - timedelta(
            hours=max(dedup.window_hours, dedup.story_key_max_date_gap_days * 24)
        )
        for cand in candidates:
            created = _as_utc(cand.created_at)
            if created is not None and created < story_since:
                break  # created_at-DESC: everything past here is older still
            if _story_key(cand.payload) != story_key:
                continue
            cand_evt = _as_utc(cand.event_date)
            # Fall through to merge when either side lacks a date —
            # canonical key is the only signal we have. When BOTH dates
            # exist, gate on proximity so two distinct events with the
            # same canonical anchor (a hypothetical second NVIDIA × A26029
            # partnership 14 days after the first) still alert separately.
            if new_evt is None or cand_evt is None or abs(new_evt - cand_evt) <= max_gap:
                return cand

    best_grey: Event | None = None
    best_grey_score = -1
    best_grey_subtype_match = False
    subtype_fallback: Event | None = None
    for cand in in_window:
        cand_sub = _event_subtype(cand.payload)
        subtype_match = bool(subtype and cand_sub and subtype == cand_sub)

        # Most-recent same-subtype in window — used as a fallback for the
        # judge when both story_key and fuzz fail. CN news headlines and
        # LLM-normalized summaries about the same event routinely score
        # <55 (token-set on unsegmented CJK is character-level, paraphrased
        # English summaries still drift), so fuzz alone misses real
        # duplicates. `in_window` is created_at-DESC so the first
        # same-subtype hit IS the most recent.
        if subtype_match and subtype_fallback is None:
            subtype_fallback = cand

        score = _similarity(title, summary, cand)
        if score >= dedup.title_threshold:
            return cand
        if score >= dedup.grey_threshold:
            # Highest-scoring grey candidate wins; same-subtype breaks ties.
            # The old "first match wins" was sending the judge against an
            # arbitrary candidate instead of the most-similar one, which
            # tanked merge recall once grey_threshold dropped low enough to
            # admit multiple candidates per item.
            better = (
                score > best_grey_score
                or (score == best_grey_score and subtype_match and not best_grey_subtype_match)
            )
            if better:
                best_grey = cand
                best_grey_score = score
                best_grey_subtype_match = subtype_match

    judge_target = best_grey if best_grey is not None else subtype_fallback
    if judge_target is not None and dedup.judge_enabled and hasattr(classifier, "same_event"):
        cand_summary = (
            ((judge_target.payload or {}).get("classification") or {}).get("summary") or ""
        )
        if await classifier.same_event(
            a_title=title,
            a_summary=summary,
            b_title=judge_target.title or "",
            b_summary=cand_summary,
        ):
            return judge_target
    return None


async def _recent_notified_same_subtype(
    events_repo: EventRepository,
    *,
    symbol: str,
    exchange: str,
    subtype: str,
    event_date: datetime | None,
    now: datetime,
    window_hours: int,
    max_date_gap_days: int,
) -> Event | None:
    """step 3: the most recent already-NOTIFIED catalyst for the same
    (symbol, subtype) within the repeat window — i.e. the alert this new item
    would be noise on top of. Same event-date proximity rule as the story_key
    fast-path: when both sides carry dates and they're further apart than
    ``max_date_gap_days``, the two are clearly distinct events (a second deal
    weeks later) and the repeat gate must NOT fire."""
    since = now - timedelta(hours=window_hours)
    max_gap = timedelta(days=max_date_gap_days)
    new_evt = _as_utc(event_date)
    for cand in await events_repo.list_recent_catalysts_for_company(symbol, exchange):
        created = _as_utc(cand.created_at)
        if created is not None and created < since:
            break  # created_at-DESC: everything past here is older still
        if cand.status != "notified":
            continue
        if _event_subtype(cand.payload) != subtype:
            continue
        cand_evt = _as_utc(cand.event_date)
        if new_evt is None or cand_evt is None or abs(new_evt - cand_evt) <= max_gap:
            return cand
    return None


async def _process_news_item(
    session: AsyncSession,
    *,
    tc,
    news: dict,
    sid: str,
    source_name: str,
    events_repo: EventRepository,
    notif_repo: NotificationRepository,
    classifier: OpenAIClassifier,
    gate: _Gate,
    dedup: _Dedup,
    s: CatalystSyncSummary,
    budget: int,
    cfg_obj=None,
    enricher: EarningsEnricher | None = None,
) -> int:
    """Prefilter → classify → event/notification for a single normalized news
    item. Shared by the EODHD and web_search source paths. Returns the budget
    remaining after this item (a classified item costs one unit)."""
    if await events_repo.get_by_source_event_id(sid) is not None:
        return budget  # idempotent: already processed this article

    # Freshness gate. web_search occasionally returns months-old articles
    # for thinly-covered names (the LLM is told "recent" but doesn't
    # strictly comply); we drop anything dated outside the configured
    # window so the user doesn't get an alert about year-old news. The
    # EODHD path also benefits — its news API has been observed to
    # backfill older items into the recent feed.
    item_dt = _news_dt(news)
    if item_dt is not None:
        age_days = (_utcnow() - item_dt).days
        if age_days > gate.max_age_days:
            s.prefiltered += 1
            await _ignored_event(
                events_repo,
                sid=sid,
                tc=tc,
                news=news,
                reason=f"stale_news_{age_days}d",
                source_name=source_name,
            )
            return budget
    elif gate.drop_undated_news:
        # The stale-news gate above is silently bypassed when ``date`` is
        # null; web_search's static-product-page rediscoveries (Unitree H2,
        # R1 pre-sale, DigitalServo) all returned date=null and were
        # alerted as fresh launches months after the actual launch. Drop
        # outright — a real news article carries a date.
        s.prefiltered += 1
        await _ignored_event(
            events_repo,
            sid=sid,
            tc=tc,
            news=news,
            reason="undated_news",
            source_name=source_name,
        )
        return budget

    reason = prefilter_reason(news["title"], news["content"])
    if reason is not None:
        s.prefiltered += 1
        await _ignored_event(
            events_repo, sid=sid, tc=tc, news=news, reason=reason, source_name=source_name
        )
        return budget

    # Per-company cross-source key: URL-exact when we have a link, else title.
    link = news.get("link")
    content_key = (
        content_dedup_key(tc.symbol, tc.exchange, link)
        if link
        else content_text_key(tc.symbol, tc.exchange, news["title"])
    )
    # URL-exact dedup BEFORE classifying — a duplicate another source already
    # alerted costs no LLM call. (Semantic same-story dedup needs the
    # classification, so it happens after.) ``content_dedup_key`` hashes
    # ``normalize_url(link)`` so mobile/desktop variants (/cn/mobile/H2plus/
    # vs /cn/H2plus/) collapse here without a second gate.
    prior = await events_repo.get_by_dedup_key(content_key)
    if prior is not None:
        await events_repo.append_also_seen_in(
            prior,
            {"source": source_name, "url": link, "source_event_id": sid, "via": "url"},
        )
        s.deduped += 1
        return budget

    budget -= 1
    cls = await classifier.classify(
        company_name=tc.company_name,
        symbol=tc.symbol,
        title=news["title"],
        content=news["content"],
    )

    s.classified += 1
    session.add(
        ClassifierRun(
            model=cls.model or classifier.model,
            prompt_version=settings.catalyst_prompt_version,
            status=cls.status,
            response_id=cls.response_id,
            input_tokens=cls.input_tokens,
            output_tokens=cls.output_tokens,
            is_company_critical=(
                bool(cls.output.get("is_company_critical")) if cls.output else None
            ),
            output=cls.output,
            error=cls.error,
        )
    )

    if cls.status != "completed" or cls.output is None:
        s.errors += 1
        await _ignored_event(
            events_repo,
            sid=sid,
            tc=tc,
            news=news,
            reason=f"classifier_{cls.status}",
            source_name=source_name,
        )
        return budget

    out = cls.output
    critical = bool(out.get("is_company_critical"))
    importance = str(out.get("importance", "")).lower()
    try:
        confidence = float(out.get("confidence") or 0)
    except (TypeError, ValueError):
        confidence = 0.0

    passes = critical and _rank(importance) >= gate.min_rank and confidence >= gate.min_conf
    if not passes:
        s.skipped += 1
        await _ignored_event(
            events_repo,
            sid=sid,
            tc=tc,
            news=news,
            reason=out.get("ignore_reason") or "below_threshold",
            source_name=source_name,
        )
        return budget

    autosend = _rank(importance) >= gate.auto_rank and confidence >= gate.auto_conf
    # Even a HIGH-confidence classification on an aggregator / opinion
    # piece (Yahoo Finance editorial, Insider Monkey, etc.) is suspect
    # — those sources recycle real corporate facts as commentary and
    # the classifier reads the embedded fact as fresh. Demote autosend
    # to review so the user sees it but Telegram doesn't fire on it
    # by default.
    demoted_by = _autosend_blocked_by_domain(news.get("link"))
    if autosend and demoted_by is not None:
        autosend = False
        out["autosend_demoted_by_domain"] = demoted_by

    subtype_lower = str(out.get("event_subtype") or "").strip().lower()

    # Semantic same-story merge (catches the same event at a different URL).
    twin = await _find_semantic_twin(
        events_repo,
        classifier,
        dedup,
        symbol=tc.symbol,
        exchange=tc.exchange,
        subtype=subtype_lower or None,
        title=news["title"],
        summary=str(out.get("summary") or ""),
        story_key=_canonical_story_key(out.get("story_key")),
        event_date=_news_dt(news),
        now=_utcnow(),
    )
    if twin is not None:
        await events_repo.append_also_seen_in(
            twin,
            {"source": source_name, "url": link, "source_event_id": sid, "via": "semantic"},
        )
        s.deduped += 1
        return budget

    # step 3: repeat-alert window. The twin check above merges the
    # SAME story; this gate additionally quiets a *different* story with
    # the same (symbol, subtype) when one already alerted within the
    # window — the leak mode where a bundled re-report mints a unique
    # story_key and the judge says "different". Demote (not drop): the
    # event + notification still land in the review queue, Telegram just
    # doesn't fire twice in a day for the same kind of news. The
    # event-date proximity rule exempts clearly-distinct events.
    if autosend and gate.repeat_window_hours > 0 and subtype_lower:
        repeat_of = await _recent_notified_same_subtype(
            events_repo,
            symbol=tc.symbol,
            exchange=tc.exchange,
            subtype=subtype_lower,
            event_date=_news_dt(news),
            now=_utcnow(),
            window_hours=gate.repeat_window_hours,
            max_date_gap_days=dedup.story_key_max_date_gap_days,
        )
        if repeat_of is not None:
            autosend = False
            out["autosend_demoted_by_repeat_window"] = repeat_of.id

    payload: dict[str, Any] = {"news": news, "classification": out}

    # Earnings-report enrichment (catalyst_enrich): best-effort 2nd LLM
    # call — a failure must NEVER block the notification.
    financials = await maybe_enrich_earnings(
        enricher,
        subtype=subtype_lower,
        url=link,
        company_name=tc.company_name,
        symbol=tc.symbol,
    )
    if financials is not None:
        payload["financials"] = financials
        s.enriched += 1

    event = Event(
        event_type="catalyst",
        source_name=source_name,
        source_event_id=sid,
        dedup_key=content_key,
        symbol=tc.symbol,
        exchange=tc.exchange,
        company_name=tc.company_name,
        title=news["title"],
        event_date=_news_dt(news),
        source_url=link,
        status="notified" if autosend else "review",
        payload=payload,
    )
    event, created = await events_repo.upsert(event)
    if not created:
        # Same URL already alerted by another source — provenance recorded in
        # the original event's also_seen_in; do not relevance-link or re-alert.
        s.deduped += 1
        return budget

    # Spillover propagation runs BEFORE EventRelevance + Notification so
    # we don't write an EventRelevance row for an event that gets dropped
    # silently — EventRelevance.matched=True is the canonical signal that
    # the UI's "relevant events" filter joins on (event_repository._list
    # uses `EventRelevance.matched.is_(True)` as the subquery). An
    # orphaned matched-row on an ignored event would surface dropped
    # spillover candidates back into the dashboard.
    propagation_enabled = bool(getattr(cfg_obj, "relationship_propagation_enabled", True))
    if propagation_enabled:
        assess = await assess_affected_companies(
            session, event, primary_tracked=tc
        )
        # Bake the affected list + assessment status into the event
        # payload so the formatter (and the dispatch re-render path)
        # can render the "Affects your watchlist" section without an
        # extra query.
        event.payload = {
            **(event.payload or {}),
            "affected": assess.payload_rows,
            "affected_assessment": {
                "status": assess.llm_status,
                "error": assess.llm_error,
                "candidates_evaluated": assess.candidates_evaluated,
            },
        }
        session.add(event)
        if assess.affected_rows_inserted > assess_primary_count(assess):
            s.propagated += 1
        if not assess.has_alertable:
            s.propagation_dropped += 1
            # No primary-alertable + no affected → drop silently. The
            # event still exists (audit trail), but no Notification or
            # EventRelevance row is created so the UI never surfaces
            # this dropped event. Flush so the ignored state + dropped
            # marker land before sync_catalysts batches the commit.
            event.status = "ignored"
            event.payload = {
                **(event.payload or {}),
                "spillover_dropped": True,
            }
            session.add(event)
            await session.flush()
            return budget

    session.add(
        EventRelevance(
            event_id=event.id,
            tracked_company_id=tc.id,
            matched=True,
            reason="company-critical catalyst",
            score=confidence,
        )
    )

    dkey = make_dedup_key(content_key, "catalyst")
    if await notif_repo.get_by_dedup_key(dkey) is None:
        session.add(
            Notification(
                event_id=event.id,
                channel="telegram",
                dedup_key=dkey,
                status="pending" if autosend else "review",
                payload={"text": format_catalyst(event)},
            )
        )
    s.catalysts += 1
    if autosend:
        s.autosent += 1
    else:
        s.review += 1
    return budget


def assess_primary_count(assess) -> int:
    """Convenience: how many of the rows we just inserted were primary
    rows (vs affected). Used to decide whether to bump the propagated
    counter."""
    return len(assess.primary_listings)
