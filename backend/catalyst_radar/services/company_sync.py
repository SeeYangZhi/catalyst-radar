from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from catalyst_radar.adapters.eodhd import (
    EodhdCompanyReferenceAdapter,
    EodhdConfigError,
)
from catalyst_radar.logging import get_logger
from catalyst_radar.repositories.company_repository import (
    CompanyReferenceRepository,
)
from catalyst_radar.repositories.source_repository import (
    RawItemRepository,
    SourceRunRepository,
)

log = get_logger(__name__)


@dataclass(slots=True)
class SyncSummary:
    exchanges: list[str]
    total_upserted: int
    errors: int


def _split_exchanges(raw: str) -> list[str]:
    return [e.strip().upper() for e in raw.split(",") if e.strip()]


async def sync_company_reference(
    session: AsyncSession,
    adapter: EodhdCompanyReferenceAdapter | None = None,
) -> SyncSummary:
    """Sync EODHD exchange symbol lists into company_reference.

    One source_run per exchange. Raw payload is persisted before
    normalization. A single failing exchange does not abort the rest.
    Returns early with no runs recorded when the runtime gate
    `eodhd_company_reference_enabled` is False — useful when the EODHD
    plan doesn't include `exchange-symbol-list` (HTTP 402 every call).
    """
    from catalyst_radar.runtime_config import effective

    eff = await effective(session)
    if not bool(eff.eodhd_company_reference_enabled):
        log.info("company_sync_disabled")
        return SyncSummary(exchanges=[], total_upserted=0, errors=0)

    adapter = adapter or EodhdCompanyReferenceAdapter()
    exchanges = _split_exchanges(str(eff.eodhd_company_reference_exchanges))
    runs = SourceRunRepository(session)
    raw_repo = RawItemRepository(session)
    ref_repo = CompanyReferenceRepository(session)

    total_upserted = 0
    errors = 0

    for exchange in exchanges:
        run = await runs.start(f"eodhd.company_reference.{exchange}")
        try:
            result = await adapter.fetch(exchange)
        except EodhdConfigError as exc:
            errors += 1
            await runs.finish(run, status="skipped", last_error=str(exc))
            log.warning("company_sync_skipped", exchange=exchange, error=str(exc))
            continue
        except Exception as exc:  # noqa: BLE001 - one exchange must not abort all
            errors += 1
            await runs.finish(run, status="failed", last_error=repr(exc))
            log.warning("company_sync_failed", exchange=exchange, error=repr(exc))
            continue

        await raw_repo.store(
            source_name=result.source_name,
            schema_name=result.schema_name,
            raw_payload=result.payload,
            source_url=result.source_url,
            http_status=result.http_status,
            source_run_id=run.id,
        )

        if result.http_status != 200 or not result.items:
            # EODHD returns 402 (Payment Required) for *daily quota exceeded*
            # on All-in-One and lower plans, not just "your plan can't access
            # this endpoint". Surface it as rate-limited so it's clear the
            # next run will recover automatically when the daily quota resets.
            rate_limited = result.http_status == 402
            if result.http_status == 200:
                status = "empty"
            elif rate_limited:
                status = "rate_limited"
            else:
                status = "failed"
            last_error: str | None = None
            if rate_limited:
                last_error = (
                    "EODHD daily API quota exhausted (HTTP 402). The next "
                    "scheduled run will retry after the quota resets at "
                    "00:00 UTC. Check https://eodhd.com/cp/dashboard for "
                    "remaining requests."
                )
            elif status == "failed":
                last_error = f"http {result.http_status}"
            await runs.finish(
                run,
                status=status,
                item_count=0,
                last_error=last_error,
            )
            await session.commit()
            log.info(
                "company_sync_no_data",
                exchange=exchange,
                http_status=result.http_status,
            )
            # Short-circuit on 402 — every subsequent exchange will hit the
            # same daily-quota error and just produce identical rows.
            if rate_limited:
                log.warning("company_sync_rate_limited_short_circuit")
                break
            continue

        upserted = 0
        for raw in result.items:
            if not adapter.is_equity_like(raw):
                continue
            norm = adapter.normalize(raw)
            if not norm["symbol"] or not norm["exchange"]:
                continue
            await ref_repo.upsert(
                symbol=norm["symbol"],
                exchange=norm["exchange"],
                company_name=norm["company_name"],
                country=norm["country"],
                currency=norm["currency"],
                isin=norm["isin"],
                source=norm["source"],
                source_payload=norm["source_payload"],
            )
            upserted += 1

        await runs.finish(run, status="success", item_count=upserted)
        await session.commit()
        total_upserted += upserted
        log.info("company_sync_ok", exchange=exchange, upserted=upserted)

    return SyncSummary(exchanges=exchanges, total_upserted=total_upserted, errors=errors)
