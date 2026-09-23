# api/v1/ — FastAPI routers

## Purpose

HTTP surface for the dashboard. One router per resource (companies, events, notifications, settings, …). Stateless: routers call repositories or services; never inline business logic, never run long jobs synchronously.

## Ownership

All API endpoints under `/api/v1/*`. Shared deps (`SessionDep`, `CurrentUser`, oauth2 scheme) live in `../deps.py` and are owned at the api/ level.

## Local Contracts

### Router shape

```python
from catalyst_radar.api.deps import CurrentUser, SessionDep

router = APIRouter(prefix="/<resource>", tags=["<resource>"])

@router.post("/foo", response_model=FooOut, status_code=status.HTTP_201_CREATED)
async def create_foo(
    payload: FooRequest,
    current_user: CurrentUser,
    session: SessionDep,
) -> Foo:
    ...
```

- Always declare `response_model` (or `response_model=None` with an explicit return type if you need raw passthrough).
- Always include `current_user: CurrentUser` — auth is required on every endpoint except `auth/*`, `health`, and the Telegram webhook (`POST /telegram/webhook`, guarded by the echoed secret token instead). `tests/test_auth_sweep.py` enforces this by sweeping the live route table; intentionally public routes go in its `PUBLIC_PATHS` allowlist with a justification comment.
- Always include `session: SessionDep` for DB access. Don't construct `AsyncSession` manually.

### Use repositories, not raw SQL

`from catalyst_radar.repositories.<name>_repository import <Name>Repository` then `repo = NameRepository(session)`. Routes that grew a one-off `select()` are tech debt — extract a repo method.

### Pydantic schemas

Request/response models in the same router file when small, or in `../schemas/<name>.py` when shared. Strict mode is the default for LLM schemas (see `schemas/llm.py`); HTTP request/response schemas are forgiving by default — only use `extra="forbid"` when you want to reject unknown keys.

### Status codes

- `201 Created` for POST that creates (set `status_code=status.HTTP_201_CREATED`).
- `204 No Content` for DELETE (no body).
- `409 Conflict` for "already exists" / idempotency violations.
- `422 Unprocessable Content` for validation failures (use `HTTPException`; FastAPI's automatic 422 covers schema errors).
- `404 Not Found` for missing resources.

### Side-effects from a POST

Long-running work (preflight, source discovery, LLM enrichment) is enqueued via Celery `.delay()`, not awaited. Pattern:

```python
def _enqueue_preflight(tracked_company_id: int) -> None:
    from catalyst_radar.tasks import task_run_preflight
    task_run_preflight.delay(tracked_company_id)
```

The route returns immediately; the worker runs the task. Idle nginx / load-balancer timeouts will trip if you await a 30-90s LLM call inline.

### Auth + admin

`CurrentUser` resolves the bearer token. There's currently one admin user (`admin@radar.local`, seeded by `seed.py`); no fine-grained roles. If you need a feature gated to a future role, leave a TODO and don't bolt on a half-roles system.

### Error responses

`raise HTTPException(status_code=..., detail="...")`. `detail` is shown to the user; keep it short and actionable. Stack traces stay in the server logs.

## Work Guidance

- New endpoint: pick or create the matching router file → add the route → ensure response_model + auth + session deps → call into a repository/service.
- Wire the router into `__init__.py` (or wherever routers are aggregated for the FastAPI app — currently `main.py` does the include).
- Add a test in `tests/test_<resource>_api.py` covering: auth required (401 unauth'd), happy path (201/200), idempotency (409), validation (422).

## Verification

- `cd backend && uv run pytest tests/test_<resource>_api.py`.
- Curl smoke: `curl -X POST http://localhost:8000/api/v1/companies/tracked -H 'Authorization: Bearer ...' -d '...'`.

## Child DOX Index

(none)
