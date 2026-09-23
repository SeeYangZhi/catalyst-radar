"""Effective runtime configuration.

Values come from env-based Settings, optionally overridden at runtime by
rows in `app_config` (edited from the Alert Settings UI). Behavioural
code should read effective config via `effective()` so UI changes take
effect without a redeploy. Sync-interval keys are baked into the Celery Beat
schedule at startup, so changing them needs a scheduler restart (flagged in the
UI via RESTART_REQUIRED); digest hours are read live by the hourly digest tick.
"""

from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from catalyst_radar.config import settings
from catalyst_radar.repositories.config_repository import ConfigRepository

# key -> (Settings attribute, python type) for everything editable at runtime.
EDITABLE: dict[str, tuple[str, type]] = {
    "registration_enabled": ("registration_enabled", bool),
    "telegram_alerts_enabled": ("telegram_alerts_enabled", bool),
    "telegram_polling_enabled": ("telegram_polling_enabled", bool),
    "eodhd_ipo_enabled_countries": ("eodhd_ipo_enabled_countries", str),
    "hk_ipo_source_aastocks_enabled": ("hk_ipo_source_aastocks_enabled", bool),
    "sec_edgar_enrich_enabled": ("sec_edgar_enrich_enabled", bool),
    "hkex_prospectus_enrich_enabled": ("hkex_prospectus_enrich_enabled", bool),
    "hkex_listed_flip_enabled": ("hkex_listed_flip_enabled", bool),
    "cn_ipo_source_akshare_enabled": ("cn_ipo_source_akshare_enabled", bool),
    "cn_ipo_prospectus_enrich_enabled": ("cn_ipo_prospectus_enrich_enabled", bool),
    "cn_ipo_review_enabled": ("cn_ipo_review_enabled", bool),
    "ipo_dispatch_grace_minutes": ("ipo_dispatch_grace_minutes", int),
    "ipo_websearch_provisional_enabled": ("ipo_websearch_provisional_enabled", bool),
    "cn_ipo_review_sync_interval_minutes": ("cn_ipo_review_sync_interval_minutes", int),
    "cn_unlocks_enabled": ("cn_unlocks_enabled", bool),
    "unlock_alert_windows_days": ("unlock_alert_windows_days", str),
    "eodhd_company_reference_exchanges": ("eodhd_company_reference_exchanges", str),
    "eodhd_company_reference_enabled": ("eodhd_company_reference_enabled", bool),
    "alert_timezone": ("alert_timezone", str),
    "earnings_alert_windows_days": ("earnings_alert_windows_days", str),
    "ipo_alert_windows_days": ("ipo_alert_windows_days", str),
    "ipo_exclude_etfs_trusts": ("ipo_exclude_etfs_trusts", bool),
    "ipo_industry_keywords": ("ipo_industry_keywords", str),
    "ipo_min_deal_size_usd": ("ipo_min_deal_size_usd", int),
    "ipo_exchange_filter": ("ipo_exchange_filter", str),
    "eodhd_sync_interval_minutes": ("eodhd_sync_interval_minutes", int),
    "catalyst_sync_interval_minutes": ("catalyst_sync_interval_minutes", int),
    "daily_digest_hour": ("daily_digest_hour", int),
    "weekly_digest_day": ("weekly_digest_day", str),
    "weekly_digest_hour": ("weekly_digest_hour", int),
    "catalyst_min_importance": ("catalyst_min_importance", str),
    "catalyst_min_confidence": ("catalyst_min_confidence", float),
    "catalyst_autosend_min_importance": ("catalyst_autosend_min_importance", str),
    "catalyst_autosend_min_confidence": ("catalyst_autosend_min_confidence", float),
    "catalyst_max_items_per_run": ("catalyst_max_items_per_run", int),
    "catalyst_news_max_age_days": ("catalyst_news_max_age_days", int),
    "catalyst_drop_undated_news": ("catalyst_drop_undated_news", bool),
    "catalyst_eastmoney_news_enabled": ("catalyst_eastmoney_news_enabled", bool),
    "catalyst_mops_news_enabled": ("catalyst_mops_news_enabled", bool),
    "catalyst_eodhd_news_skip_exchanges": ("catalyst_eodhd_news_skip_exchanges", str),
    "catalyst_enrich_earnings_enabled": ("catalyst_enrich_earnings_enabled", bool),
    "catalyst_enrich_max_per_run": ("catalyst_enrich_max_per_run", int),
    "catalyst_enrich_consensus_enabled": ("catalyst_enrich_consensus_enabled", bool),
    "catalyst_blog_sources_enabled": ("catalyst_blog_sources_enabled", bool),
    "catalyst_discovery_enabled": ("catalyst_discovery_enabled", bool),
    "catalyst_websearch_enabled": ("catalyst_websearch_enabled", bool),
    "catalyst_websearch_all_markets": ("catalyst_websearch_all_markets", bool),
    "catalyst_websearch_gap_always_on": ("catalyst_websearch_gap_always_on", bool),
    "catalyst_websearch_gap_exchanges": ("catalyst_websearch_gap_exchanges", str),
    "catalyst_websearch_lookback_days": ("catalyst_websearch_lookback_days", int),
    "catalyst_dedup_llm_judge_enabled": ("catalyst_dedup_llm_judge_enabled", bool),
    "catalyst_repeat_alert_window_hours": ("catalyst_repeat_alert_window_hours", int),
    "monitor_self_enabled": ("monitor_self_enabled", bool),
    "monitor_max_ingestion_gap_hours": ("monitor_max_ingestion_gap_hours", int),
    "monitor_source_stale_hours": ("monitor_source_stale_hours", int),
    "monitor_alert_cooldown_hours": ("monitor_alert_cooldown_hours", int),
    "monitor_failure_streak_window_hours": ("monitor_failure_streak_window_hours", int),
    "monitor_failure_streak_threshold": ("monitor_failure_streak_threshold", int),
    "relationship_propagation_enabled": ("relationship_propagation_enabled", bool),
    "relationship_preflight_enabled": ("relationship_preflight_enabled", bool),
    "relationship_max_hops": ("relationship_max_hops", int),
}

# Keys baked into the Celery Beat schedule at startup (UI marks these
# restart-only). Digest hours are NOT here: the hourly digest tick reads them
# live via effective(), so edits take effect on the next tick without a restart.
RESTART_REQUIRED = {
    "eodhd_sync_interval_minutes",
    "catalyst_sync_interval_minutes",
}


_BOOL_TRUE = {"1", "true", "yes", "on"}
_BOOL_FALSE = {"0", "false", "no", "off"}


def _coerce(value: Any, typ: type, *, strict: bool = False) -> Any:
    """Coerce a value to its EDITABLE type.

    Lenient by default (READ path): a non-coercible value is returned as-is so
    `effective()` never crashes on a garbage app_config row. With strict=True
    (WRITE paths) a non-coercible value raises ValueError instead, so nothing
    invalid gets persisted.
    """
    if value is None or isinstance(value, typ):
        return value
    if typ is bool:
        text = str(value).strip().lower()
        if strict and text not in _BOOL_TRUE | _BOOL_FALSE:
            raise ValueError(f"{value!r} is not a valid bool")
        return text in _BOOL_TRUE
    try:
        return typ(value)
    except (TypeError, ValueError) as exc:
        if strict:
            raise ValueError(f"{value!r} is not a valid {typ.__name__}") from exc
        return value


def coerce_editable(key: str, value: Any) -> Any:
    """Strict coercion for write paths (PUT /settings, POST /settings/import).

    `key` must be in EDITABLE. Raises ValueError naming the key when the value
    cannot be coerced to the key's type — callers must write nothing in that
    case.
    """
    _, typ = EDITABLE[key]
    try:
        return _coerce(value, typ, strict=True)
    except ValueError as exc:
        raise ValueError(f"invalid value for '{key}': {exc}") from exc


@dataclass(slots=True)
class EffectiveConfig:
    values: dict[str, Any]

    def __getattr__(self, name: str) -> Any:
        try:
            return self.values[name]
        except KeyError as exc:  # pragma: no cover - programmer error
            raise AttributeError(name) from exc


async def effective(session: AsyncSession) -> EffectiveConfig:
    """Env defaults merged with persisted app_config overrides."""
    overrides = await ConfigRepository(session).all()
    merged: dict[str, Any] = {}
    for key, (attr, typ) in EDITABLE.items():
        if key in overrides and overrides[key] is not None:
            merged[key] = _coerce(overrides[key], typ)
        else:
            merged[key] = getattr(settings, attr)
    return EffectiveConfig(merged)
