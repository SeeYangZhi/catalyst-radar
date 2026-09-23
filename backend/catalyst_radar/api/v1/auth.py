import secrets
import time
from collections import defaultdict
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordRequestForm

from catalyst_radar.api.deps import SessionDep
from catalyst_radar.config import settings
from catalyst_radar.logging import get_logger
from catalyst_radar.models.user import User
from catalyst_radar.repositories.user_repository import UserRepository
from catalyst_radar.runtime_config import effective
from catalyst_radar.schemas.auth import RegisterRequest, Token, UserOut
from catalyst_radar.security import create_access_token, hash_password, verify_password

router = APIRouter()
log = get_logger(__name__)


# Sliding-window login limiter, keyed by BOTH client IP and submitted email
# — an attacker rotating one is still bounded on the other. Redis-backed so
# the limit is shared across the api + worker processes (and survives an
# api restart); falls back to a per-process in-memory window whenever Redis
# is unreachable, so login never hard-fails on a Redis blip.
_LOGIN_WINDOW_SECONDS = 60.0
_LOGIN_MAX_ATTEMPTS = 5
_login_attempts: dict[str, list[float]] = defaultdict(list)


def _client_ip(request: Request) -> str:
    """First hop in X-Forwarded-For (set by nginx from Cloudflare's
    CF-Connecting-IP), falling back to the direct peer."""
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        return xff.split(",", 1)[0].strip()
    return request.client.host if request.client else "unknown"


async def _redis_sliding_window(client: object, keys: list[str]) -> bool:
    """Sliding-window count for each key against ``client`` (a redis.asyncio
    client). Returns False as soon as any key exceeds the limit, True if all
    keys are within it. Each attempt is a unique ZSET member scored by wall
    time; stale members are trimmed and the key TTL'd so the window self-
    expires. Split out from client construction so it's unit-testable with a
    fake client."""
    now = time.time()
    cutoff = now - _LOGIN_WINDOW_SECONDS
    for key in keys:
        rkey = f"login_rl:{key}"
        member = f"{now:.6f}:{secrets.token_hex(4)}"  # unique per attempt
        async with client.pipeline(transaction=True) as pipe:
            pipe.zremrangebyscore(rkey, 0, cutoff)
            pipe.zadd(rkey, {member: now})
            pipe.zcard(rkey)
            pipe.expire(rkey, int(_LOGIN_WINDOW_SECONDS) + 1)
            _, _, count, _ = await pipe.execute()
        if count > _LOGIN_MAX_ATTEMPTS:
            return False
    return True


async def _redis_within_limit(keys: list[str]) -> bool | None:
    """Redis sliding-window check across all keys. Returns True (within
    limit), False (exceeded), or None when Redis is unavailable so the
    caller can fall back to the in-memory window. A fresh client per call
    (like the healthcheck) sidesteps event-loop-binding issues and is cheap
    for an endpoint hit this rarely."""
    try:
        from redis import asyncio as aioredis

        client = aioredis.from_url(
            settings.redis_url, socket_connect_timeout=2.0, socket_timeout=2.0
        )
    except Exception:  # noqa: BLE001 - redis missing/misconfigured → fall back
        return None
    try:
        return await _redis_sliding_window(client, keys)
    except Exception:  # noqa: BLE001 - any Redis error → in-memory fallback
        return None
    finally:
        try:
            await client.aclose()
        except Exception:  # noqa: BLE001 - best-effort close
            pass


def _check_login_rate_limit_inmemory(ip: str, email: str) -> None:
    now = time.monotonic()
    cutoff = now - _LOGIN_WINDOW_SECONDS
    for key in (f"ip:{ip}", f"email:{email.strip().lower()}"):
        bucket = _login_attempts[key]
        bucket[:] = [t for t in bucket if t > cutoff]
        if len(bucket) >= _LOGIN_MAX_ATTEMPTS:
            log.warning("login_rate_limited", key=key, attempts=len(bucket), backend="memory")
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Too many login attempts. Wait a minute and try again.",
            )
        bucket.append(now)


async def _check_login_rate_limit(ip: str, email: str) -> None:
    keys = [f"ip:{ip}", f"email:{email.strip().lower()}"]
    verdict = await _redis_within_limit(keys)
    if verdict is None:
        _check_login_rate_limit_inmemory(ip, email)
        return
    if not verdict:
        log.warning("login_rate_limited", ip=ip, backend="redis")
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many login attempts. Wait a minute and try again.",
        )


@router.post("/token", response_model=Token)
async def login(
    form_data: Annotated[OAuth2PasswordRequestForm, Depends()],
    session: SessionDep,
    request: Request,
) -> Token:
    await _check_login_rate_limit(_client_ip(request), form_data.username)
    repo = UserRepository(session)
    user = await repo.get_by_email(form_data.username)
    if user is None or not verify_password(form_data.password, user.hashed_password):
        log.info("login_failed", email=form_data.username)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if not user.is_active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Inactive user")

    log.info("login_succeeded", user_id=user.id, email=user.email)
    return Token(access_token=create_access_token(str(user.id), {"role": user.role}))


@router.post("/register", response_model=UserOut, status_code=status.HTTP_201_CREATED)
async def register(payload: RegisterRequest, session: SessionDep) -> User:
    cfg = await effective(session)
    if not bool(cfg.registration_enabled):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Registration is disabled",
        )
    repo = UserRepository(session)
    if await repo.get_by_email(payload.email) is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Email already registered")

    user = User(
        email=payload.email.strip().lower(),
        hashed_password=hash_password(payload.password),
        full_name=payload.full_name,
        role="admin",
    )
    return await repo.create(user)
