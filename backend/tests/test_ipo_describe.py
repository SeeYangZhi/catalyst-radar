import pytest

from catalyst_radar.models.event import Event
from catalyst_radar.services.ipo_describe import describe_ipo_event
from catalyst_radar.services.ipo_summarizer import IpoSummary, IpoWebDescription


class FakeSummarizer:
    configured = True

    def __init__(self, *, web: str | None = None, prospectus: str | None = None) -> None:
        self._web = web
        self._prospectus = prospectus
        self.web_calls = 0
        self.summ_calls = 0

    async def describe_via_web(self, **_kw) -> IpoWebDescription:
        self.web_calls += 1
        return IpoWebDescription(
            "completed", description=self._web, sources=[{"name": "Reuters", "url": "u"}]
        )

    async def summarize(self, **_kw) -> IpoSummary:
        self.summ_calls += 1
        return IpoSummary(description=self._prospectus, market_cap_usd=None)


def _ipo(profile: dict) -> Event:
    return Event(
        event_type="ipo", source_name="eodhd.ipos", source_event_id="s",
        dedup_key="d", symbol="ACME", company_name="Acme Inc.", payload={"profile": profile},
    )


async def _never_fetch(_e):
    return None


async def _fetch_ok(_e):
    return {"summary": "Acme builds warehouse robots.", "filing_url": "f", "doc_type": "424B"}


@pytest.mark.asyncio
async def test_imminent_uses_websearch_first() -> None:
    s = FakeSummarizer(web="Acme is a robotics company.")
    e = _ipo({"checked": True})
    out = await describe_ipo_event(
        e, summarizer=s, fetch_prospectus=_never_fetch, exchange="NASDAQ",
        imminent=True, websearch_enabled=True, prospectus_source="edgar",
    )
    assert out.described is True
    assert s.web_calls == 1 and s.summ_calls == 0
    prof = e.payload["profile"]
    assert prof["description"] == "Acme is a robotics company."
    assert prof["description_source"] == "websearch"


@pytest.mark.asyncio
async def test_provisional_upgrades_to_prospectus() -> None:
    s = FakeSummarizer(prospectus="Acme designs autonomous warehouse robots.")
    e = _ipo({"description": "old web blurb", "description_source": "websearch"})
    out = await describe_ipo_event(
        e, summarizer=s, fetch_prospectus=_fetch_ok, exchange="NASDAQ",
        imminent=True, websearch_enabled=True, prospectus_source="edgar",
    )
    assert out.described is True
    prof = e.payload["profile"]
    assert prof["description_source"] == "edgar"
    assert prof["prospectus_attempted"] is True
    assert s.web_calls == 0


@pytest.mark.asyncio
async def test_upgrade_guard_keeps_web_blurb_when_prospectus_is_boilerplate() -> None:
    s = FakeSummarizer(prospectus="Describes the public offering process and application channels.")
    e = _ipo({"description": "good web blurb", "description_source": "websearch"})
    out = await describe_ipo_event(
        e, summarizer=s, fetch_prospectus=_fetch_ok, exchange="HKSE",
        imminent=False, websearch_enabled=True, prospectus_source="prospectus",
    )
    assert out.described is False
    prof = e.payload["profile"]
    assert prof["description"] == "good web blurb"
    assert prof["description_source"] == "websearch"
    assert prof["prospectus_attempted"] is True


@pytest.mark.asyncio
async def test_prospectus_not_refetched_once_attempted() -> None:
    s = FakeSummarizer(web="web blurb")
    calls = {"n": 0}

    async def _counting_fetch(_e):
        calls["n"] += 1
        return {"summary": "x"}

    # description-less backlog event that already had its prospectus attempt
    e = _ipo({"checked": True, "prospectus_attempted": True})
    out = await describe_ipo_event(
        e, summarizer=s, fetch_prospectus=_counting_fetch, exchange="HKSE",
        imminent=False, websearch_enabled=True, prospectus_source="prospectus",
    )
    assert calls["n"] == 0          # prospectus NOT re-fetched
    assert out.described is True    # web fallback still fills it
    assert e.payload["profile"]["description_source"] == "websearch"


@pytest.mark.asyncio
async def test_backlog_is_prospectus_first() -> None:
    s = FakeSummarizer(web="web", prospectus="Acme makes robots.")
    e = _ipo({"checked": True})
    out = await describe_ipo_event(
        e, summarizer=s, fetch_prospectus=_fetch_ok, exchange="NASDAQ",
        imminent=False, websearch_enabled=True, prospectus_source="edgar",
    )
    assert out.described is True
    assert s.summ_calls == 1 and s.web_calls == 0
    assert e.payload["profile"]["description_source"] == "edgar"
