import json
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from catalyst_radar.adapters.base import FetchResult
from catalyst_radar.adapters.eodhd import EodhdCompanyReferenceAdapter
from catalyst_radar.repositories.company_repository import (
    CompanyReferenceRepository,
)
from catalyst_radar.services.company_sync import sync_company_reference

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "eodhd_exchange_symbol_list.json").read_text()
)


def test_eodhd_normalize_and_dedup() -> None:
    adapter = EodhdCompanyReferenceAdapter(api_key="test")
    norm = adapter.normalize(FIXTURE[1])  # Tencent HK
    assert norm["symbol"] == "0700"
    assert norm["exchange"] == "HK"
    assert norm["country"] == "HK"  # derived from exchange, not free-text
    assert norm["isin"] == "KYG00000Z002"

    assert adapter.is_equity_like(FIXTURE[0]) is True  # Common Stock
    assert adapter.is_equity_like(FIXTURE[2]) is False  # ETF

    k1 = adapter.dedup_key(FIXTURE[1])
    k2 = adapter.dedup_key(adapter.normalize(FIXTURE[1]))
    assert k1 == k2  # stable across raw and normalized forms


class _StubAdapter(EodhdCompanyReferenceAdapter):
    def __init__(self) -> None:
        super().__init__(api_key="stub")

    async def fetch(self, target: str) -> FetchResult:
        return FetchResult(
            source_name="eodhd",
            schema_name="eodhd.exchange_symbol_list.v1",
            source_url=f"https://eodhd.test/{target}",
            http_status=200,
            payload=FIXTURE,
            items=FIXTURE,
        )


async def test_company_sync_upserts_equity_only(db_session: AsyncSession, monkeypatch) -> None:
    monkeypatch.setattr(
        "catalyst_radar.config.settings.eodhd_company_reference_exchanges",
        "US",
    )
    summary = await sync_company_reference(db_session, adapter=_StubAdapter())
    assert summary.total_upserted == 2  # AAPL + Tencent, ETF excluded
    assert summary.errors == 0

    repo = CompanyReferenceRepository(db_session)
    rows = await repo.search(q="apple")
    assert len(rows) == 1
    assert rows[0].symbol == "AAPL"
    assert (await repo.search(q="XETF")) == []  # ETF not in reference
