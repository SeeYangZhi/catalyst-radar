from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from catalyst_radar.repositories.config_repository import ConfigRepository
from catalyst_radar.runtime_config import effective
from catalyst_radar.services.dispatch import deliver_pending_notifications


async def test_effective_falls_back_to_env_then_override(
    db_session: AsyncSession,
) -> None:
    eff = await effective(db_session)
    assert eff.telegram_alerts_enabled is True  # env default
    assert isinstance(eff.catalyst_min_confidence, float)

    await ConfigRepository(db_session).set("telegram_alerts_enabled", False)
    await ConfigRepository(db_session).set("catalyst_min_confidence", "0.9")
    eff2 = await effective(db_session)
    assert eff2.telegram_alerts_enabled is False
    assert eff2.catalyst_min_confidence == 0.9  # coerced to float


async def test_dispatch_respects_alerts_disabled(
    db_session: AsyncSession,
) -> None:
    from catalyst_radar.models.notification import Notification, TelegramChat

    db_session.add(TelegramChat(chat_id="1", is_active=True))
    db_session.add(
        Notification(
            channel="telegram",
            dedup_key="d:1",
            status="pending",
            payload={"text": "x"},
        )
    )
    await ConfigRepository(db_session).set("telegram_alerts_enabled", False)
    await db_session.commit()

    out = await deliver_pending_notifications(db_session)
    assert (out.sent, out.failed, out.skipped) == (0, 0, 0)


async def test_settings_api_roundtrip_and_validation(
    client: AsyncClient, admin_credentials: dict[str, str]
) -> None:
    token = (await client.post("/api/v1/auth/token", data=admin_credentials)).json()["access_token"]
    h = {"Authorization": f"Bearer {token}"}

    got = (await client.get("/api/v1/settings", headers=h)).json()
    assert "restart_required_keys" in got
    assert "eodhd_sync_interval_minutes" in got["restart_required_keys"]
    assert got["telegram_polling_enabled"] is True

    bad = await client.put("/api/v1/settings", headers=h, json={"not_a_key": 1})
    assert bad.status_code == 422

    put = await client.put(
        "/api/v1/settings",
        headers=h,
        json={"eodhd_sync_interval_minutes": 120, "telegram_polling_enabled": False},
    )
    assert put.status_code == 200
    assert put.json()["eodhd_sync_interval_minutes"] == 120
    assert put.json()["telegram_polling_enabled"] is False


def test_ipo_two_tier_knobs_defaults_and_allowlisted() -> None:
    from catalyst_radar.config import settings
    from catalyst_radar.runtime_config import EDITABLE

    assert settings.ipo_dispatch_grace_minutes == 5
    assert settings.ipo_websearch_provisional_enabled is True
    assert "ipo_dispatch_grace_minutes" in EDITABLE
    assert "ipo_websearch_provisional_enabled" in EDITABLE
