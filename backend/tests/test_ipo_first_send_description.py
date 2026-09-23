import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from catalyst_radar.models.base import utcnow
from catalyst_radar.models.event import Event
from catalyst_radar.models.notification import Notification, TelegramChat
from catalyst_radar.services.alert_backfill import backfill_alert_edits
from catalyst_radar.services.dispatch import deliver_pending_notifications
from catalyst_radar.services.ipo_describe import describe_ipo_event
from catalyst_radar.services.ipo_summarizer import IpoSummary, IpoWebDescription
from tests.test_dispatch import RecorderClient


class _Summ:
    configured = True

    async def describe_via_web(self, **_kw):
        return IpoWebDescription("completed", description="Acme is a robotics firm.")

    async def summarize(self, **_kw):
        return IpoSummary(
            description="Acme designs autonomous warehouse robots.",
            market_cap_usd=None,
        )


@pytest.mark.asyncio
async def test_first_send_has_web_blurb_then_upgrades(db_session: AsyncSession) -> None:
    db_session.add(TelegramChat(chat_id="111", is_active=True))
    event = Event(
        event_type="ipo", source_name="eodhd.ipos", source_event_id="e2e",
        dedup_key="dk-e2e", symbol="ACME", country="US", company_name="Acme Inc.",
        event_date=utcnow(), source_url="https://sec.gov/acme.htm",
        payload={"profile": {"checked": True}},
    )
    db_session.add(event)
    await db_session.flush()
    db_session.add(
        Notification(event_id=event.id, channel="telegram", dedup_key="n-e2e",
                     status="pending", payload={"text": "stale"})
    )
    await db_session.commit()

    s = _Summ()

    async def _no_prospectus(_e):
        return None

    # Tier 1: imminent web provisional.
    await describe_ipo_event(event, summarizer=s, fetch_prospectus=_no_prospectus,
                             exchange="NASDAQ", imminent=True, websearch_enabled=True,
                             prospectus_source="edgar")
    db_session.add(event)
    await db_session.commit()

    client = RecorderClient(message_id=900)
    await deliver_pending_notifications(db_session, client=client)
    assert "Acme is a robotics firm." in client.calls[0][1]  # web blurb shipped

    # Tier 2: prospectus upgrade lands; backfill edits the sent message.
    async def _prospectus(_e):
        return {"summary": "x", "filing_url": "f", "form": "S-1"}

    await describe_ipo_event(event, summarizer=s, fetch_prospectus=_prospectus,
                             exchange="NASDAQ", imminent=True, websearch_enabled=True,
                             prospectus_source="edgar")
    db_session.add(event)
    await db_session.commit()
    assert event.payload["profile"]["description_source"] == "edgar"

    edited = await backfill_alert_edits(db_session, [event], client=client)
    assert edited == 1
    assert "Acme designs autonomous warehouse robots." in client.edits[0]["text"]
