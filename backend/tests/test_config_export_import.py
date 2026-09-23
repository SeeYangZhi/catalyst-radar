"""Tests for runtime-config export/import (GET /settings/export, POST /settings/import).

Overrides in `app_config` were lost twice during DB migrations; the export
document is the durable backup format. Contract: export contains ONLY keys in
runtime_config.EDITABLE (internal `_`-prefixed rows excluded); import is
all-or-nothing — every key is validated before anything is written.
"""

from __future__ import annotations

from httpx import AsyncClient
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from catalyst_radar.models.config import AppConfig
from catalyst_radar.repositories.config_repository import ConfigRepository
from catalyst_radar.runtime_config import effective


async def _auth_headers(
    client: AsyncClient, admin_credentials: dict[str, str]
) -> dict[str, str]:
    token = (await client.post("/api/v1/auth/token", data=admin_credentials)).json()[
        "access_token"
    ]
    return {"Authorization": f"Bearer {token}"}


async def test_export_empty_envelope(
    client: AsyncClient, admin_credentials: dict[str, str]
) -> None:
    """No overrides set → versioned envelope with an empty overrides map."""
    h = await _auth_headers(client, admin_credentials)
    resp = await client.get("/api/v1/settings/export", headers=h)
    assert resp.status_code == 200
    assert resp.json() == {"schema_version": 1, "overrides": {}}


async def test_export_contains_exactly_the_put_overrides(
    client: AsyncClient, admin_credentials: dict[str, str]
) -> None:
    """Two overrides set via PUT /settings → export holds exactly those keys."""
    h = await _auth_headers(client, admin_credentials)
    put = await client.put(
        "/api/v1/settings",
        headers=h,
        json={"telegram_polling_enabled": False, "eodhd_sync_interval_minutes": 120},
    )
    assert put.status_code == 200

    doc = (await client.get("/api/v1/settings/export", headers=h)).json()
    assert doc["schema_version"] == 1
    assert doc["overrides"] == {
        "telegram_polling_enabled": False,
        "eodhd_sync_interval_minutes": 120,
    }


async def test_export_excludes_internal_underscore_keys(
    client: AsyncClient,
    admin_credentials: dict[str, str],
    db_session: AsyncSession,
) -> None:
    """Internal `_`-prefixed app_config rows never leak into the export."""
    await ConfigRepository(db_session).set("_internal_sweep_state", {"cursor": 42})
    h = await _auth_headers(client, admin_credentials)
    doc = (await client.get("/api/v1/settings/export", headers=h)).json()
    assert "_internal_sweep_state" not in doc["overrides"]
    assert doc["overrides"] == {}


async def test_export_skips_non_coercible_stored_value(
    client: AsyncClient,
    admin_credentials: dict[str, str],
    db_session: AsyncSession,
) -> None:
    """A legacy garbage row (written by the old lenient PUT) is omitted from
    the export; valid keys are still present — exports stay importable."""
    repo = ConfigRepository(db_session)
    await repo.set("daily_digest_hour", "abc")  # legacy non-coercible value
    await repo.set("telegram_alerts_enabled", False)
    h = await _auth_headers(client, admin_credentials)
    doc = (await client.get("/api/v1/settings/export", headers=h)).json()
    assert "daily_digest_hour" not in doc["overrides"]
    assert doc["overrides"] == {"telegram_alerts_enabled": False}


async def test_export_with_garbage_row_round_trips(
    client: AsyncClient,
    admin_credentials: dict[str, str],
    db_session: AsyncSession,
) -> None:
    """Mix of valid + garbage stored rows: the export must import cleanly
    (200) with the valid keys applied — never a 422 dead-end on restore."""
    repo = ConfigRepository(db_session)
    await repo.set("daily_digest_hour", "abc")  # legacy non-coercible value
    await repo.set("telegram_alerts_enabled", False)
    await repo.set("catalyst_min_confidence", 0.9)
    h = await _auth_headers(client, admin_credentials)
    doc = (await client.get("/api/v1/settings/export", headers=h)).json()

    resp = await client.post("/api/v1/settings/import", headers=h, json=doc)
    assert resp.status_code == 200
    assert resp.json() == {"applied": 2}
    eff = await effective(db_session)
    assert eff.telegram_alerts_enabled is False
    assert eff.catalyst_min_confidence == 0.9


async def test_export_normalizes_legacy_string_int(
    client: AsyncClient,
    admin_credentials: dict[str, str],
    db_session: AsyncSession,
) -> None:
    """A stored string "7" for an int key (old lenient PUT) exports as 7."""
    await ConfigRepository(db_session).set("daily_digest_hour", "7")
    h = await _auth_headers(client, admin_credentials)
    doc = (await client.get("/api/v1/settings/export", headers=h)).json()
    assert doc["overrides"]["daily_digest_hour"] == 7


async def test_import_restores_effective_after_wipe(
    client: AsyncClient,
    admin_credentials: dict[str, str],
    db_session: AsyncSession,
) -> None:
    """Export → wipe app_config (the migration-loss scenario) → import → the
    effective() config matches its pre-wipe state."""
    h = await _auth_headers(client, admin_credentials)
    put = await client.put(
        "/api/v1/settings",
        headers=h,
        json={"telegram_alerts_enabled": False, "catalyst_min_confidence": 0.9},
    )
    assert put.status_code == 200
    doc = (await client.get("/api/v1/settings/export", headers=h)).json()

    pre = await effective(db_session)
    assert pre.telegram_alerts_enabled is False
    assert pre.catalyst_min_confidence == 0.9

    # Simulate the migration data loss.
    await db_session.execute(delete(AppConfig))
    await db_session.commit()
    wiped = await effective(db_session)
    assert wiped.telegram_alerts_enabled is True  # back to env default

    resp = await client.post("/api/v1/settings/import", headers=h, json=doc)
    assert resp.status_code == 200
    assert resp.json() == {"applied": 2}

    post = await effective(db_session)
    assert post.values == pre.values


async def test_import_rejects_non_editable_keys_all_or_nothing(
    client: AsyncClient,
    admin_credentials: dict[str, str],
    db_session: AsyncSession,
) -> None:
    """A single bad key fails the whole import with 422 naming the offenders;
    no key (including the valid ones) is applied."""
    h = await _auth_headers(client, admin_credentials)
    resp = await client.post(
        "/api/v1/settings/import",
        headers=h,
        json={
            "schema_version": 1,
            "overrides": {
                "telegram_alerts_enabled": False,
                "not_a_real_key": 1,
                "_internal_sweep_state": {"cursor": 1},
            },
        },
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert "not_a_real_key" in detail
    assert "_internal_sweep_state" in detail
    # All-or-nothing: the valid key was NOT applied either.
    assert await ConfigRepository(db_session).all() == {}


async def test_import_rejects_non_coercible_value_all_or_nothing(
    client: AsyncClient,
    admin_credentials: dict[str, str],
    db_session: AsyncSession,
) -> None:
    """A value that can't be coerced to the key's type fails the whole import
    with a 422 naming the key; nothing (including the valid keys) is written."""
    h = await _auth_headers(client, admin_credentials)
    resp = await client.post(
        "/api/v1/settings/import",
        headers=h,
        json={
            "schema_version": 1,
            "overrides": {
                "telegram_alerts_enabled": False,
                "daily_digest_hour": "abc",
            },
        },
    )
    assert resp.status_code == 422
    assert "daily_digest_hour" in resp.json()["detail"]
    assert await ConfigRepository(db_session).all() == {}


async def test_import_rejects_dict_where_int_expected(
    client: AsyncClient,
    admin_credentials: dict[str, str],
    db_session: AsyncSession,
) -> None:
    h = await _auth_headers(client, admin_credentials)
    resp = await client.post(
        "/api/v1/settings/import",
        headers=h,
        json={
            "schema_version": 1,
            "overrides": {"daily_digest_hour": {"nested": 1}},
        },
    )
    assert resp.status_code == 422
    assert "daily_digest_hour" in resp.json()["detail"]
    assert await ConfigRepository(db_session).all() == {}


async def test_put_rejects_non_coercible_value(
    client: AsyncClient,
    admin_credentials: dict[str, str],
    db_session: AsyncSession,
) -> None:
    """PUT /settings is strict too: invalid value → 422, nothing written."""
    h = await _auth_headers(client, admin_credentials)
    resp = await client.put(
        "/api/v1/settings",
        headers=h,
        json={"telegram_alerts_enabled": False, "daily_digest_hour": "abc"},
    )
    assert resp.status_code == 422
    assert "daily_digest_hour" in resp.json()["detail"]
    assert await ConfigRepository(db_session).all() == {}


async def test_put_still_coerces_numeric_strings(
    client: AsyncClient,
    admin_credentials: dict[str, str],
    db_session: AsyncSession,
) -> None:
    """The UI sends text-field values as strings — strict coercion must keep
    accepting '7' for an int key and store the coerced int."""
    h = await _auth_headers(client, admin_credentials)
    resp = await client.put(
        "/api/v1/settings",
        headers=h,
        json={"daily_digest_hour": "7"},
    )
    assert resp.status_code == 200
    assert (await ConfigRepository(db_session).all())["daily_digest_hour"] == 7


async def test_export_import_require_auth(client: AsyncClient) -> None:
    assert (await client.get("/api/v1/settings/export")).status_code == 401
    resp = await client.post(
        "/api/v1/settings/import",
        json={"schema_version": 1, "overrides": {}},
    )
    assert resp.status_code == 401
