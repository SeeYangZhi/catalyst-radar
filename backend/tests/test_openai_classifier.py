"""Tests for services/openai_classifier.py — parse / error / usage / aclose.

No live OpenAI calls: ``openai_classifier.AsyncOpenAI`` is monkeypatched with
a stub whose response object mimics exactly the Responses-API attribute paths
the module reads:

- ``response.usage`` → ``input_tokens`` / ``output_tokens`` (via getattr)
- ``response.id``, ``response.model``, ``response.status``
- ``response.incomplete_details`` (stringified when status != "completed")
- ``response.output[*]`` items (``.type == "message"``) → ``.content[*].refusal``
- ``response.output_parsed`` (typed pydantic instance, ``.model_dump()``-ed)

The stub also records ``close()`` invocations — regression guard for
b03d144 (deterministic client close so the SDK's GC-time ``aclose()`` never
lands on a later Celery task's event loop).
"""

import httpx
import pytest
from openai import APIError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from catalyst_radar.adapters.base import FetchResult
from catalyst_radar.adapters.eodhd_news import EodhdNewsAdapter
from catalyst_radar.config import settings
from catalyst_radar.models.company import TrackedCompany
from catalyst_radar.models.event import Event
from catalyst_radar.models.notification import Notification
from catalyst_radar.repositories.classifier_repository import ClassifierRunRepository
from catalyst_radar.schemas.llm import CatalystClassification, SameEventJudgement
from catalyst_radar.services import openai_classifier as oc
from catalyst_radar.services.catalyst_sync import sync_catalysts
from catalyst_radar.services.openai_classifier import (
    OpenAIClassifier,
    OpenAIConfigError,
    reasoning_off_for,
)

# ── Responses-API stub ───────────────────────────────────────────────────


class _Usage:
    def __init__(self, input_tokens: int = 321, output_tokens: int = 87) -> None:
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


class _RefusalPart:
    type = "refusal"

    def __init__(self, refusal: str | None) -> None:
        self.refusal = refusal


class _MessageItem:
    type = "message"

    def __init__(self, content: list) -> None:
        self.content = content


class _ReasoningItem:
    """Non-message output item — _extract_refusal must skip it."""

    type = "reasoning"


class _IncompleteDetails:
    reason = "max_output_tokens"

    def __str__(self) -> str:
        return "max_output_tokens"


class _Response:
    """Duck-typed openai Responses-API response."""

    def __init__(
        self,
        *,
        status: str = "completed",
        output_parsed=None,
        response_id: str = "resp_fake_1",
        model: str = "gpt-fake-1",
        usage: _Usage | None = None,
        incomplete_details=None,
        output: list | None = None,
    ) -> None:
        self.status = status
        self.output_parsed = output_parsed
        self.id = response_id
        self.model = model
        self.usage = usage if usage is not None else _Usage()
        self.incomplete_details = incomplete_details
        self.output = output or []


class _StubResponses:
    def __init__(self, outcome) -> None:
        self._outcome = outcome
        self.calls: list[dict] = []

    async def parse(self, **kwargs):
        self.calls.append(kwargs)
        if isinstance(self._outcome, Exception):
            raise self._outcome
        return self._outcome


class _StubAsyncOpenAI:
    """Mimics the AsyncOpenAI surface openai_classifier touches:
    ``.responses.parse(...)`` and ``.close()`` (the SDK's async close)."""

    def __init__(self, outcome, *, api_key=None, timeout=None) -> None:
        self.api_key = api_key
        self.timeout = timeout
        self.responses = _StubResponses(outcome)
        self.close_calls = 0

    async def close(self) -> None:
        self.close_calls += 1


def _install_stub(monkeypatch: pytest.MonkeyPatch, outcome) -> list[_StubAsyncOpenAI]:
    """Replace the module's AsyncOpenAI with a stub factory; returns the
    list of constructed stub clients (appended on construction)."""
    created: list[_StubAsyncOpenAI] = []

    class _Factory(_StubAsyncOpenAI):
        def __init__(self, *, api_key=None, timeout=None) -> None:
            super().__init__(outcome, api_key=api_key, timeout=timeout)
            created.append(self)

    monkeypatch.setattr(oc, "AsyncOpenAI", _Factory)
    return created


def _parsed(**over) -> CatalystClassification:
    base = dict(
        is_company_critical=True,
        event_subtype="product_launch",
        importance="high",
        confidence=0.9,
        expected_impact="New revenue line.",
        summary="$AAPL launches a new AI inference chip.",
        why_it_matters="Expands TAM.",
        suggested_action="research",
        ignore_reason="",
        story_key="aapl_launch_ai_chip",
    )
    base.update(over)
    return CatalystClassification(**base)


def _api_error(message: str = "boom") -> APIError:
    return APIError(
        message,
        httpx.Request("POST", "https://api.openai.test/v1/responses"),
        body=None,
    )


# ── classify: well-formed structured output ──────────────────────────────


async def test_classify_well_formed_output_parses_fields(monkeypatch) -> None:
    parsed = _parsed()
    created = _install_stub(monkeypatch, _Response(output_parsed=parsed))
    clf = OpenAIClassifier(api_key="test-key")

    result = await clf.classify(
        company_name="Apple Inc",
        symbol="AAPL",
        title="Apple launches AI inference chip",
        content="A substantial product announcement with real business impact.",
    )

    assert result.status == "completed"
    assert result.output == parsed.model_dump()
    # Every contract field the pipeline reads downstream is present.
    for key in (
        "is_company_critical",
        "event_subtype",
        "importance",
        "confidence",
        "summary",
        "ignore_reason",
        "story_key",
    ):
        assert key in result.output
    assert result.response_id == "resp_fake_1"
    assert result.model == "gpt-fake-1"
    assert result.input_tokens == 321
    assert result.output_tokens == 87
    assert result.error is None

    # Constructor wiring: api_key + timeout flow into the SDK client.
    assert len(created) == 1
    assert created[0].api_key == "test-key"
    assert created[0].timeout == settings.openai_timeout_seconds


async def test_classify_request_shape(monkeypatch) -> None:
    """The parse() call carries the strict-mode schema, the configured
    model/temperature/token budget, a $-prefixed ticker, and the 4000-char
    body clip."""
    created = _install_stub(monkeypatch, _Response(output_parsed=_parsed()))
    clf = OpenAIClassifier(api_key="test-key")

    await clf.classify(
        company_name="Apple Inc",
        symbol="AAPL",
        title="Apple launches AI inference chip",
        content="x" * 5000,
    )

    (call,) = created[0].responses.calls
    assert call["model"] == settings.catalyst_classifier_model
    assert call["text_format"] is CatalystClassification
    assert call["temperature"] == settings.catalyst_classifier_temperature
    assert call["max_output_tokens"] == settings.catalyst_classifier_max_output_tokens
    assert call["reasoning"] == reasoning_off_for(clf.model)
    system, user = call["input"]
    assert system["role"] == "system"
    assert "financial catalyst classifier" in system["content"]
    assert user["role"] == "user"
    assert "Company: Apple Inc ($AAPL)" in user["content"]
    assert "Headline: Apple launches AI inference chip" in user["content"]
    # Body clipped to 4000 chars.
    body = user["content"].split("Body: ", 1)[1]
    assert len(body) == 4000


def test_reasoning_off_per_model_family() -> None:
    assert reasoning_off_for("gpt-5.4-mini") == {"effort": "none"}
    assert reasoning_off_for("gpt-5-mini") == {"effort": "minimal"}
    assert reasoning_off_for("some-future-model") == {"effort": "minimal"}


# ── classify: malformed / truncated / refused output ────────────────────


async def test_classify_truncated_output_returns_incomplete_status(monkeypatch) -> None:
    """Truncation (max_output_tokens hit) comes back as status='incomplete'
    with output_parsed unusable — classify must hand back a failed-shaped
    result, not raise."""
    _install_stub(
        monkeypatch,
        _Response(
            status="incomplete",
            output_parsed=None,
            incomplete_details=_IncompleteDetails(),
        ),
    )
    clf = OpenAIClassifier(api_key="test-key")

    result = await clf.classify(
        company_name="Apple Inc", symbol="AAPL", title="t", content="c"
    )
    assert result.status == "incomplete"
    assert result.output is None
    assert "max_output_tokens" in (result.error or "")
    # Usage metadata is still captured for the (billed) truncated call.
    assert result.input_tokens == 321
    assert result.output_tokens == 87


async def test_classify_empty_output_parsed_fails_soft(monkeypatch) -> None:
    """status='completed' but no parsed object (malformed JSON the SDK
    couldn't coerce) → failed result, no exception to the caller."""
    _install_stub(monkeypatch, _Response(output_parsed=None))
    clf = OpenAIClassifier(api_key="test-key")

    result = await clf.classify(
        company_name="Apple Inc", symbol="AAPL", title="t", content="c"
    )
    assert result.status == "failed"
    assert result.output is None
    assert result.error == "empty output_parsed"


async def test_classify_api_error_fails_soft(monkeypatch) -> None:
    _install_stub(monkeypatch, _api_error("rate limited"))
    clf = OpenAIClassifier(api_key="test-key")

    result = await clf.classify(
        company_name="Apple Inc", symbol="AAPL", title="t", content="c"
    )
    assert result.status == "failed"
    assert result.output is None
    assert "rate limited" in (result.error or "")


async def test_classify_refusal_surfaces_as_failed(monkeypatch) -> None:
    """Structured-Outputs safety refusals sit in response.output as a
    refusal content part; the SDK does not raise. classify must convert
    them to a failed result."""
    _install_stub(
        monkeypatch,
        _Response(
            output_parsed=_parsed(),  # would parse — refusal must win
            output=[
                _ReasoningItem(),
                _MessageItem([_RefusalPart(None), _RefusalPart("I cannot help with that")]),
            ],
        ),
    )
    clf = OpenAIClassifier(api_key="test-key")

    result = await clf.classify(
        company_name="Apple Inc", symbol="AAPL", title="t", content="c"
    )
    assert result.status == "failed"
    assert result.output is None
    assert result.error == "refusal: I cannot help with that"


async def test_classify_without_api_key_raises_config_error(monkeypatch) -> None:
    _install_stub(monkeypatch, _Response(output_parsed=_parsed()))
    clf = OpenAIClassifier(api_key="")
    assert clf.configured is False
    with pytest.raises(OpenAIConfigError):
        await clf.classify(company_name="X", symbol="X", title="t", content="c")


# ── same_event judge ─────────────────────────────────────────────────────


async def test_same_event_returns_judgement_bool(monkeypatch) -> None:
    _install_stub(
        monkeypatch, _Response(output_parsed=SameEventJudgement(same=True))
    )
    clf = OpenAIClassifier(api_key="test-key")
    assert (
        await clf.same_event(a_title="a", a_summary="s", b_title="b", b_summary="s")
        is True
    )


async def test_same_event_returns_none_on_error_paths(monkeypatch) -> None:
    """API error, non-completed status, and unconfigured client all map to
    None — the dedup caller treats None as 'do not merge'."""
    _install_stub(monkeypatch, _api_error())
    clf_err = OpenAIClassifier(api_key="test-key")
    assert (
        await clf_err.same_event(a_title="a", a_summary="s", b_title="b", b_summary="s")
        is None
    )

    _install_stub(monkeypatch, _Response(status="incomplete", output_parsed=None))
    clf_inc = OpenAIClassifier(api_key="test-key")
    assert (
        await clf_inc.same_event(a_title="a", a_summary="s", b_title="b", b_summary="s")
        is None
    )

    clf_off = OpenAIClassifier(api_key="")
    assert (
        await clf_off.same_event(a_title="a", a_summary="s", b_title="b", b_summary="s")
        is None
    )


# ── classifier run persisted with prompt version + token usage ───────────

_FRESH_ITEM = {
    "title": "Apple launches major new AI inference chip product line",
    "content": "A substantial product announcement with real business impact and detail.",
    "link": "https://news.example/apple-ai-chip-launch",
    "date": "2026-05-28",  # fresh relative to the frozen 2026-06-04 clock
}


class _OneItemNewsStub(EodhdNewsAdapter):
    def __init__(self) -> None:
        super().__init__(api_key="stub")

    async def fetch(self, target: str) -> FetchResult:
        return FetchResult(
            source_name="eodhd_news",
            schema_name="eodhd.news.v1",
            source_url=f"https://eodhd.test/news?s={target}",
            http_status=200,
            payload=[_FRESH_ITEM],
            items=[_FRESH_ITEM],
        )


async def _seed_company(session: AsyncSession) -> None:
    session.add(
        TrackedCompany(symbol="AAPL", exchange="US", company_name="Apple Inc", source="manual")
    )
    await session.commit()


async def test_classifier_run_recorded_with_prompt_version_and_usage(
    db_session: AsyncSession, monkeypatch
) -> None:
    """Driving the catalyst sync with a real OpenAIClassifier (stubbed SDK
    client) must persist one ClassifierRun row carrying the prompt version
    and the per-call token usage — the durable cost/audit trail."""
    await _seed_company(db_session)
    _install_stub(
        monkeypatch,
        _Response(output_parsed=_parsed(), usage=_Usage(input_tokens=555, output_tokens=66)),
    )
    clf = OpenAIClassifier(api_key="test-key")

    s = await sync_catalysts(db_session, news_adapter=_OneItemNewsStub(), classifier=clf)
    assert s.classified == 1

    runs = await ClassifierRunRepository(db_session).list_recent()
    assert len(runs) == 1
    run = runs[0]
    assert run.prompt_version == settings.catalyst_prompt_version
    assert run.model == "gpt-fake-1"  # response.model, not the configured alias
    assert run.status == "completed"
    assert run.response_id == "resp_fake_1"
    assert run.input_tokens == 555
    assert run.output_tokens == 66
    assert run.is_company_critical is True
    assert run.output["event_subtype"] == "product_launch"
    assert run.error is None


async def test_failed_classification_recorded_and_event_ignored(
    db_session: AsyncSession, monkeypatch
) -> None:
    """A truncated (incomplete) classification still writes a ClassifierRun
    row — with the error — and the item becomes an ignored event, never an
    alert."""
    await _seed_company(db_session)
    _install_stub(
        monkeypatch,
        _Response(
            status="incomplete",
            output_parsed=None,
            incomplete_details=_IncompleteDetails(),
        ),
    )
    clf = OpenAIClassifier(api_key="test-key")

    s = await sync_catalysts(db_session, news_adapter=_OneItemNewsStub(), classifier=clf)
    assert s.classified == 1
    assert s.errors == 1

    runs = await ClassifierRunRepository(db_session).list_recent()
    assert len(runs) == 1
    run = runs[0]
    assert run.status == "incomplete"
    assert run.prompt_version == settings.catalyst_prompt_version
    assert "max_output_tokens" in (run.error or "")
    assert run.is_company_critical is None
    assert run.output is None

    events = (await db_session.execute(select(Event))).scalars().all()
    assert len(events) == 1
    assert events[0].status == "ignored"
    assert (events[0].payload or {}).get("ignore_reason") == "classifier_incomplete"
    notifs = (await db_session.execute(select(Notification))).scalars().all()
    assert notifs == []


# ── aclose: deterministic client close (regression for b03d144) ──────────


async def test_aclose_closes_underlying_client(monkeypatch) -> None:
    created = _install_stub(monkeypatch, _Response(output_parsed=_parsed()))
    clf = OpenAIClassifier(api_key="test-key")
    assert created[0].close_calls == 0

    await clf.aclose()
    assert created[0].close_calls == 1
    assert clf._client is None  # client released — GC can't re-schedule a close


async def test_aclose_is_idempotent(monkeypatch) -> None:
    created = _install_stub(monkeypatch, _Response(output_parsed=_parsed()))
    clf = OpenAIClassifier(api_key="test-key")

    await clf.aclose()
    await clf.aclose()
    assert created[0].close_calls == 1  # second call is a no-op


async def test_aclose_noop_when_unconfigured() -> None:
    clf = OpenAIClassifier(api_key="")
    await clf.aclose()  # must not raise


async def test_classify_after_aclose_raises_config_error(monkeypatch) -> None:
    """After a deterministic close the classifier must refuse further calls
    instead of resurrecting a client on a possibly-dead loop."""
    _install_stub(monkeypatch, _Response(output_parsed=_parsed()))
    clf = OpenAIClassifier(api_key="test-key")
    await clf.aclose()
    with pytest.raises(OpenAIConfigError):
        await clf.classify(company_name="X", symbol="X", title="t", content="c")
