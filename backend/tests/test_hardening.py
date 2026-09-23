"""Security-hardening tests (deep-analysis task 5).

Covers:
- Redis-backed login limiter sliding-window math (fake client) + the
  in-memory fallback path at the endpoint.
- Telegram webhook secret rejection (constant-time compare).
- Production insecure-default startup assertion.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from catalyst_radar.api.v1 import auth as auth_module
from catalyst_radar.config import Settings


# ── Redis sliding-window limiter ─────────────────────────────────────────
class _FakePipe:
    def __init__(self, store: dict[str, dict[str, float]]) -> None:
        self.store = store
        self.ops: list[tuple] = []

    async def __aenter__(self) -> _FakePipe:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    def zremrangebyscore(self, key: str, lo: float, hi: float) -> _FakePipe:
        self.ops.append(("rem", key, hi))
        return self

    def zadd(self, key: str, mapping: dict[str, float]) -> _FakePipe:
        self.ops.append(("add", key, mapping))
        return self

    def zcard(self, key: str) -> _FakePipe:
        self.ops.append(("card", key))
        return self

    def expire(self, key: str, ttl: int) -> _FakePipe:
        self.ops.append(("exp", key))
        return self

    async def execute(self) -> list:
        out: list = []
        for op in self.ops:
            if op[0] == "rem":
                _, key, hi = op
                z = self.store.setdefault(key, {})
                for member, score in list(z.items()):
                    if score <= hi:
                        del z[member]
                out.append(0)
            elif op[0] == "add":
                _, key, mapping = op
                self.store.setdefault(key, {}).update(mapping)
                out.append(len(mapping))
            elif op[0] == "card":
                out.append(len(self.store.get(op[1], {})))
            else:  # exp
                out.append(True)
        return out


class _FakeRedis:
    def __init__(self) -> None:
        self.store: dict[str, dict[str, float]] = {}

    def pipeline(self, transaction: bool = True) -> _FakePipe:
        return _FakePipe(self.store)


@pytest.mark.asyncio
async def test_redis_sliding_window_blocks_after_max() -> None:
    """The 6th attempt in the window (max = 5) trips the limit."""
    fake = _FakeRedis()
    verdicts = [await auth_module._redis_sliding_window(fake, ["ip:1.2.3.4"]) for _ in range(6)]
    assert verdicts[:5] == [True, True, True, True, True]
    assert verdicts[5] is False


@pytest.mark.asyncio
async def test_redis_sliding_window_independent_keys() -> None:
    """Distinct keys (different IPs) each get their own budget."""
    fake = _FakeRedis()
    for _ in range(5):
        assert await auth_module._redis_sliding_window(fake, ["ip:a"]) is True
    # A fresh IP is unaffected by the exhausted one.
    assert await auth_module._redis_sliding_window(fake, ["ip:b"]) is True


@pytest.mark.asyncio
async def test_login_limiter_inmemory_fallback(
    client: AsyncClient, admin_credentials: dict[str, str]
) -> None:
    """With Redis returning None (forced in conftest), the endpoint falls
    back to the in-memory window: 5 attempts allowed, the 6th is 429."""
    wrong = {"username": admin_credentials["username"], "password": "nope"}
    codes = [
        (await client.post("/api/v1/auth/token", data=wrong)).status_code for _ in range(6)
    ]
    assert codes[:5] == [401, 401, 401, 401, 401]
    assert codes[5] == 429


@pytest.mark.asyncio
async def test_login_limiter_redis_verdict_blocks(
    client: AsyncClient, admin_credentials: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the Redis backend itself reports 'exceeded', the very first
    request is rejected with 429 (overrides the conftest in-memory force)."""

    async def _exceeded(keys: list[str]) -> bool:
        return False

    monkeypatch.setattr(auth_module, "_redis_within_limit", _exceeded)
    resp = await client.post("/api/v1/auth/token", data=admin_credentials)
    assert resp.status_code == 429


# ── Telegram webhook secret ──────────────────────────────────────────────
@pytest.mark.asyncio
async def test_webhook_rejects_wrong_secret(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from catalyst_radar.config import settings

    monkeypatch.setattr(settings, "telegram_webhook_secret", "topsecret")
    resp = await client.post(
        "/api/v1/telegram/webhook",
        json={},
        headers={"X-Telegram-Bot-Api-Secret-Token": "wrong"},
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_webhook_accepts_correct_secret(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from catalyst_radar.config import settings

    monkeypatch.setattr(settings, "telegram_webhook_secret", "topsecret")
    resp = await client.post(
        "/api/v1/telegram/webhook",
        json={},  # no update_id → ignored, but only after the secret passes
        headers={"X-Telegram-Bot-Api-Secret-Token": "topsecret"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "ignored"


@pytest.mark.asyncio
async def test_webhook_missing_header_rejected(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from catalyst_radar.config import settings

    monkeypatch.setattr(settings, "telegram_webhook_secret", "topsecret")
    resp = await client.post("/api/v1/telegram/webhook", json={})
    assert resp.status_code == 403


# ── Production secret assertion ──────────────────────────────────────────
def test_dev_environment_has_no_secret_problems() -> None:
    s = Settings(environment="development")
    assert s.production_secret_problems() == []


def test_production_flags_insecure_defaults() -> None:
    s = Settings(environment="production")  # defaults still in place
    problems = s.production_secret_problems()
    assert any("JWT_SECRET_KEY" in p for p in problems)
    assert any("DEFAULT_ADMIN_PASSWORD" in p for p in problems)


def test_production_with_overrides_is_clean() -> None:
    s = Settings(
        environment="prod",
        jwt_secret_key="a-real-long-random-secret",
        default_admin_password="an0ther-strong-pw!",
        database_url="postgresql+asyncpg://radar_user:s3cret@db:5432/radar_db",
    )
    assert s.production_secret_problems() == []


def test_production_flags_default_postgres_password() -> None:
    s = Settings(
        environment="prod",
        jwt_secret_key="a-real-long-random-secret",
        default_admin_password="an0ther-strong-pw!",
    )
    assert any("Postgres" in p for p in s.production_secret_problems())


def test_production_flags_bot_without_admin_chat() -> None:
    s = Settings(
        environment="prod",
        jwt_secret_key="a-real-long-random-secret",
        default_admin_password="an0ther-strong-pw!",
        database_url="postgresql+asyncpg://radar_user:s3cret@db:5432/radar_db",
        telegram_bot_token="123:abc",
        telegram_admin_chat_id="",
    )
    assert any("TELEGRAM_ADMIN_CHAT_ID" in p for p in s.production_secret_problems())
