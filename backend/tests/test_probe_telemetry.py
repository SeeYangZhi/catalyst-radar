"""persist URL-probe outcomes in source-run summaries.

Container logs are wiped on every deploy, so the ``url_probe_kept`` /
``url_probe_dropped`` structlog events alone cannot support the WAF
false-keep monitoring . The probe layer returns per-URL outcomes and
``catalyst_sync`` persists an aggregated histogram on the websearch
source run — kept/dropped totals plus per-status breakdowns for
kept-non-2xx (the WAF-suspect keeps) and drops.
"""

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from catalyst_radar.adapters import websearch_news
from catalyst_radar.adapters.websearch_news import WebSearchNewsAdapter, WebSearchResult
from catalyst_radar.config import settings
from catalyst_radar.models.company import TrackedCompany
from catalyst_radar.models.source import SourceRun
from catalyst_radar.services.catalyst_sync import sync_catalysts
from catalyst_radar.services.url_liveness import ProbeOutcome
from tests.test_catalyst_sync import _EmptyNewsStub, _FakeClassifier

_ITEMS = [
    {
        "title": "Shunsin wins major AI inference chip packaging order",
        "content": "A substantial new customer win with real revenue impact and detail.",
        "link": "https://live-a.example/ai-order",
        "date": "2026-05-20",
    },
    {
        "title": "Shunsin announces major capacity expansion plan",
        "content": "A substantial operational announcement with real business impact.",
        "link": "https://live-b.example/expansion",
        "date": "2026-05-21",
    },
    {
        "title": "Shunsin signs major optical packaging contract",
        "content": "A substantial contract announcement with real revenue detail.",
        "link": "https://paywalled.example/report",
        "date": "2026-05-22",
    },
    {
        "title": "Shunsin quarterly financial report",
        "content": "A substantial earnings summary with detailed financial figures.",
        "link": "https://dead.example/2026Q1EN.pdf",
        "date": "2026-05-23",
    },
]

# url -> (final status, kept?): 2 kept-2xx, 1 kept-403 (WAF-suspect), 1 dropped-404.
_PLAN = {
    "https://live-a.example/ai-order": (200, True),
    "https://live-b.example/expansion": (200, True),
    "https://paywalled.example/report": (403, True),
    "https://dead.example/2026Q1EN.pdf": (404, False),
}


async def _fake_probe_urls(urls: list[str]) -> list[ProbeOutcome]:
    return [ProbeOutcome(url=u, status=_PLAN[u][0], kept=_PLAN[u][1]) for u in urls]


class _ProbingWebSearchStub(WebSearchNewsAdapter):
    """Skips the OpenAI call but runs the REAL liveness step, so probe
    outcomes flow back exactly as the production ``fetch()`` produces them."""

    def __init__(self, items: list[dict]) -> None:
        super().__init__(api_key="stub")
        self._items = items

    async def fetch(self, *, company_name: str, symbol: str, exchange: str) -> WebSearchResult:
        items, probes = await self._apply_liveness(list(self._items))
        return WebSearchResult("completed", items, url_probes=probes)


async def _seed_tw_company(session: AsyncSession) -> None:
    session.add(
        TrackedCompany(
            symbol="6451", exchange="TW", company_name="Shunsin Technology", source="manual"
        )
    )
    await session.commit()


async def test_probe_outcomes_persisted_on_websearch_run(
    db_session: AsyncSession, monkeypatch
) -> None:
    monkeypatch.setattr(settings, "catalyst_websearch_enabled", True)
    monkeypatch.setattr(websearch_news, "probe_urls", _fake_probe_urls)
    await _seed_tw_company(db_session)

    await sync_catalysts(
        db_session,
        news_adapter=_EmptyNewsStub(),
        classifier=_FakeClassifier(),
        websearch_adapter=_ProbingWebSearchStub(_ITEMS),
    )

    runs = (
        (
            await db_session.execute(
                select(SourceRun).where(SourceRun.source_name == "websearch_news.6451.TW")
            )
        )
        .scalars()
        .all()
    )
    assert len(runs) == 1
    run = runs[0]
    assert run.status == "success"
    assert run.item_count == 3  # the dropped-404 item never enters the pipeline
    assert run.summary == {
        "url_probes": {
            "kept": 3,
            "dropped": 1,
            "kept_non_2xx": {"403": 1},
            "dropped_status": {"404": 1},
        }
    }


async def test_no_probes_means_no_url_probes_key(
    db_session: AsyncSession, monkeypatch
) -> None:
    """A run that probed nothing (no URLs surfaced) must NOT grow an empty
    url_probes block — absence is the signal that no probes ran."""
    monkeypatch.setattr(settings, "catalyst_websearch_enabled", True)
    monkeypatch.setattr(websearch_news, "probe_urls", _fake_probe_urls)
    await _seed_tw_company(db_session)

    await sync_catalysts(
        db_session,
        news_adapter=_EmptyNewsStub(),
        classifier=_FakeClassifier(),
        websearch_adapter=_ProbingWebSearchStub([]),
    )

    runs = (await db_session.execute(select(SourceRun))).scalars().all()
    assert runs  # eodhd + websearch runs both finished
    for run in runs:
        assert run.summary is None


async def test_source_runs_api_returns_summary(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    admin_credentials: dict[str, str],
    monkeypatch,
) -> None:
    """The runs API must expose the summary payload so the dashboard can
    render probe counts."""
    async with session_factory() as s:
        monkeypatch.setattr(settings, "catalyst_websearch_enabled", True)
        monkeypatch.setattr(websearch_news, "probe_urls", _fake_probe_urls)
        await _seed_tw_company(s)
        await sync_catalysts(
            s,
            news_adapter=_EmptyNewsStub(),
            classifier=_FakeClassifier(),
            websearch_adapter=_ProbingWebSearchStub(_ITEMS),
        )

    token = (await client.post("/api/v1/auth/token", data=admin_credentials)).json()[
        "access_token"
    ]
    resp = await client.get(
        "/api/v1/source-runs", headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 200
    rows = resp.json()
    websearch = [r for r in rows if r["source_name"] == "websearch_news.6451.TW"]
    assert len(websearch) == 1
    assert websearch[0]["summary"]["url_probes"]["kept"] == 3
    assert websearch[0]["summary"]["url_probes"]["dropped"] == 1
