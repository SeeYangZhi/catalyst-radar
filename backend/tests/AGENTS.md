# tests/ — backend test suite

## Purpose

Pytest-asyncio suite covering adapters, services, sync pipelines, API endpoints, and dedup logic. In-memory SQLite per test; no live network calls.

## Ownership

All `tests/test_*.py` under `backend/tests/`. Shared fixtures in `conftest.py`. No production code lives here.

## Local Contracts

### Fixtures (`conftest.py`)

- `session_factory` — fresh in-memory SQLite engine per test, seeded with an admin user. SQLModel metadata applied via `create_all`.
- `db_session` — single `AsyncSession` from the factory; the common starting point for repository/service tests.
- `client` — `httpx.AsyncClient` against the ASGI app, with `get_session` overridden to use the test factory.
- `admin_credentials` — `{username, password}` for the seeded admin (`TEST_ADMIN_EMAIL` / `TEST_ADMIN_PASSWORD`).
- `_reset_login_rate_limit` — autouse; clears the in-memory login limiter so consecutive auth'd tests don't trip the 5/min cap, and pins the limiter to its in-memory backend (deterministic with or without a local Redis).
- `_pin_gap_websearch_off` — autouse; pins `catalyst_websearch_gap_always_on` off, `catalyst_eodhd_news_skip_exchanges` empty, and `catalyst_mops_news_enabled` off so tests that track gap-exchange (TW/CN) companies stay deterministic without injecting stubs. Tests exercising those paths opt in explicitly.
- `_freeze_catalyst_clock` — autouse; pins `catalyst_sync.utcnow` to 2026-06-04 so the catalyst news fixtures (fixed mid-2026 dates) never age past the 21-day freshness gate.

### Async convention

`asyncio_mode = "auto"` in `pyproject.toml`. Just write `async def test_*` — no `@pytest.mark.asyncio` decorator needed.

### No live network

Every adapter has a stub subclass in its test file. Stub by either:
- Overriding `_fetch_blocking` (akshare / tushare / pandas DataFrames as fixtures).
- Overriding `fetch` to return a `FetchResult` constructed from a JSON fixture under `tests/fixtures/`.
- For LLM stubs: subclass the real classifier and override `classify` (and `same_event` if testing dedup), return canned dicts.
- When the classifier itself is under test, stub one level lower: monkeypatch `openai_classifier.AsyncOpenAI` with a fake Responses-API client whose response mimics the exact attribute paths the module reads, and record `close()` calls (aclose regression guard). See `test_openai_classifier.py`.

Add new fixtures under `tests/fixtures/<source>.json`. Synthetic data with the exact real-source shape (same keys / DOM / sheet layout); never commit verbatim third-party captures.

### No real DNS

Tests never resolve hostnames. The autouse `_stub_dns` fixture in `conftest.py` stubs `net_guard._resolve` (the SSRF guard in `catalyst_radar/net_guard.py`) to a public address; guard tests override it to exercise private / loopback / link-local rejection.

### Date drift

Fixtures with calendar dates (`tests/fixtures/eodhd_ipos.json`, etc.) anchor to `today_local(settings.alert_timezone)` via per-test rewrites in the test module (see `test_calendar_sync.py` top — it walks each fixture rec and replaces dates with offsets from today). NEVER hardcode an absolute future date in a fixture — it'll silently age out and the reminder-window logic will skip the test data.

### Test scope

- **adapter tests** — normalize / source_event_id / dedup_key. No DB.
- **service tests** — sync against `db_session` with stub adapter; assert event_repository + notification_repository state. Idempotency: run sync twice, assert second run is a no-op.
- **API tests** — use `client` + `_auth(client, admin_credentials)` to bear-token-auth, then exercise endpoints. Assert status code + response shape + DB state.

### `monkeypatch` over env

For tests that need a different `settings` value, use `monkeypatch.setattr(settings, "<key>", <value>)`. Don't set env vars; the `Settings` instance is already constructed at import.

### Real-shape stubs over minimal mocks

`_FakeClassifier` and friends return the exact shape `OpenAIClassifier.classify` returns (every field present, even `ignore_reason=""`). When the schema gains a new required field, the stubs break — that's the point. Update them in lockstep with the schema.

## Work Guidance

- New test file: name `test_<domain>.py` matching the module under test. Group related tests in one file.
- New stub class: place inline in the test file unless 3+ tests share it; only extract to a helper module when there's real reuse.
- When adding a regression test for a bug: write the test FIRST against the buggy code, watch it fail with a clear message, then fix the code.
- Tightening a test that "passed for the wrong reason": prefer changing the stub to break the unintended success path (e.g. paraphrased summaries that fuzz can't catch, forcing the test through the layer it claims to cover) over restructuring assertions.

## Verification

- `cd backend && uv run pytest -q` — full suite. Must be 100% green pre-commit.
- `cd backend && uv run pytest tests/test_<x>.py -x --tb=short` — single-file fast iteration with first-failure stop.
- Coverage is not yet wired; recall is enforced by the breadth of existing tests + a regression test for every production bug.

## Child DOX Index

(none)
