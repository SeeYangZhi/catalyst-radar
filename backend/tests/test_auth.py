from httpx import AsyncClient


async def test_login_and_me(client: AsyncClient, admin_credentials: dict[str, str]) -> None:
    resp = await client.post("/api/v1/auth/token", data=admin_credentials)
    assert resp.status_code == 200
    token = resp.json()["access_token"]
    assert token

    me = await client.get("/api/v1/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200
    body = me.json()
    assert body["email"] == admin_credentials["username"]
    assert body["role"] == "admin"


async def test_me_requires_auth(client: AsyncClient) -> None:
    resp = await client.get("/api/v1/me")
    assert resp.status_code == 401


async def test_login_wrong_password(client: AsyncClient) -> None:
    resp = await client.post(
        "/api/v1/auth/token",
        data={"username": "admin@radar.local", "password": "wrong"},
    )
    assert resp.status_code == 401


async def test_register_disabled_by_default(client: AsyncClient) -> None:
    resp = await client.post(
        "/api/v1/auth/register",
        json={"email": "new@radar.local", "password": "secret123"},
    )
    assert resp.status_code == 403


async def test_settings_protected(client: AsyncClient, admin_credentials: dict[str, str]) -> None:
    assert (await client.get("/api/v1/settings")).status_code == 401

    token = (await client.post("/api/v1/auth/token", data=admin_credentials)).json()["access_token"]
    resp = await client.get("/api/v1/settings", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    assert resp.json()["registration_enabled"] is False
