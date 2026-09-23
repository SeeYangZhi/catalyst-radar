"""Wire-format regression guards for every Structured Outputs call site.

We mock httpx at the transport layer inside ``openai.AsyncOpenAI`` so we
can assert the exact JSON the SDK serializes for /v1/responses. Each
test captures the posted body and verifies:

- ``text.format.type == "json_schema"`` and ``strict == True`` (the
  whole point of strict outputs — the bug class this prevents was free-
  form ``manufacturing_expansion`` vs ``manufacturing expansion`` drift).
- ``reasoning.effort`` is at the per-model floor (we pay nothing for
  chain-of-thought tokens; we already had a prod cost spike from this).
- ``tools = [{"type": "web_search"}]`` on every Tier-2 site (web_search
  + Structured Outputs is the combo we live-probed; a refactor that
  drops the tool would silently revert search-based call sites).

Without these tests, a future refactor that drops the schema, the
reasoning param, or the tool would silently revert the call site."""

from __future__ import annotations

import json
from typing import Any

import httpx
import openai
import pytest

# Real class reference captured before any monkeypatching.
_RealAsyncOpenAI = openai.AsyncOpenAI

# Modules that bind their own `AsyncOpenAI` symbol via `from openai
# import AsyncOpenAI`. Patching `openai.AsyncOpenAI` alone is not enough
# — Python copies the reference at import time, so we have to patch
# every module that already imported it.
_BIND_TARGETS = [
    "openai.AsyncOpenAI",
    "catalyst_radar.services.openai_classifier.AsyncOpenAI",
    "catalyst_radar.services.ipo_summarizer.AsyncOpenAI",
    "catalyst_radar.services.earnings_enrich.AsyncOpenAI",
    "catalyst_radar.services.source_discovery.AsyncOpenAI",
    "catalyst_radar.adapters.websearch_news.AsyncOpenAI",
]


def _canned_reply(parsed_obj: dict) -> dict:
    """A minimal /v1/responses success body. The SDK reads `output[].
    content[].text` to recover the JSON it will then validate against
    the supplied Pydantic schema."""
    return {
        "id": "resp_test",
        "object": "response",
        "created_at": 0,
        "model": "test-model",
        "status": "completed",
        "output": [
            {
                "id": "msg_test",
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [
                    {
                        "type": "output_text",
                        "text": json.dumps(parsed_obj),
                        "annotations": [],
                    }
                ],
            }
        ],
        "usage": {
            "input_tokens": 10,
            "output_tokens": 5,
            "total_tokens": 15,
        },
    }


@pytest.fixture
def _patch_openai(monkeypatch):
    """Patches every module's AsyncOpenAI binding so a test can capture
    the request body sent to /v1/responses without hitting the network.
    Returns a factory; calling it sets the reply and yields the capture
    dict."""
    capture: dict[str, Any] = {}

    def make(reply: dict):
        async def handler(request: httpx.Request) -> httpx.Response:
            capture["url"] = str(request.url)
            capture["body"] = json.loads(request.content.decode())
            return httpx.Response(200, json=reply)

        def patched(*args, **kwargs):
            kwargs["http_client"] = httpx.AsyncClient(
                transport=httpx.MockTransport(handler)
            )
            return _RealAsyncOpenAI(*args, **kwargs)

        for target in _BIND_TARGETS:
            monkeypatch.setattr(target, patched)
        return capture

    return make


# ── Pydantic-schema sanity (catches drift if someone edits llm.py) ───


def test_event_subtype_enum_is_canonical() -> None:
    """The classifier's downstream subtype guard hardcodes a few specific
    strings — most importantly catalyst_enrich._ENRICH_SUBTYPES expects
    'earnings_report' and 'guidance' verbatim. If someone renames them
    in llm.py, the enrichment trigger silently stops firing."""
    from catalyst_radar.schemas.llm import EventSubtype

    enum_values = set(EventSubtype.__args__)
    assert "earnings_report" in enum_values
    assert "guidance" in enum_values


def test_reasoning_off_for_picks_per_model_floor() -> None:
    """gpt-5.4-* accepts 'none' (rejects 'minimal'); gpt-5-* accepts
    'minimal' (rejects 'none'). The helper must pick the right floor per
    model or the Responses API returns 400 on every call."""
    from catalyst_radar.services.openai_classifier import reasoning_off_for

    assert reasoning_off_for("gpt-5.4-mini")["effort"] == "none"
    assert reasoning_off_for("gpt-5.4-mini-2026-03-17")["effort"] == "none"
    assert reasoning_off_for("gpt-5-mini")["effort"] == "minimal"
    assert reasoning_off_for("gpt-5-mini-2025-08-07")["effort"] == "minimal"
    assert reasoning_off_for("future-unknown-model")["effort"] == "minimal"


# ── Tier 1: text-only structured-output sites ────────────────────────


async def test_classify_sends_strict_schema_with_subtype_enum(_patch_openai) -> None:
    from catalyst_radar.schemas.llm import EventSubtype
    from catalyst_radar.services.openai_classifier import OpenAIClassifier

    capture = _patch_openai(
        _canned_reply(
            {
                "is_company_critical": True,
                "event_subtype": "product_launch",
                "importance": "high",
                "confidence": 0.9,
                "expected_impact": "stock up",
                "summary": "summary",
                "why_it_matters": "matters",
                "suggested_action": "act",
                "ignore_reason": "",
                "story_key": "tst_launch_widget",
            }
        )
    )
    clf = OpenAIClassifier(api_key="sk-test")
    result = await clf.classify(
        company_name="Test Co", symbol="TST", title="t", content="c"
    )

    assert result.status == "completed"
    assert capture["url"].endswith("/v1/responses")
    assert capture["body"]["reasoning"]["effort"] in {"minimal", "none"}
    fmt = capture["body"]["text"]["format"]
    assert fmt["type"] == "json_schema"
    assert fmt["strict"] is True
    schema = fmt["schema"]
    # event_subtype must be a closed enum — that is the whole point of
    # this migration (no more "manufacturing_expansion" vs
    # "manufacturing expansion").
    assert set(schema["properties"]["event_subtype"]["enum"]) == set(
        EventSubtype.__args__
    )
    assert schema["properties"]["importance"]["enum"] == ["low", "medium", "high"]
    # strict=true requires every key listed in `required`.
    assert set(schema["required"]) == set(schema["properties"].keys())
    # additionalProperties:false is mandatory for strict mode.
    assert schema["additionalProperties"] is False


async def test_classify_surfaces_refusal_as_failed(_patch_openai) -> None:
    """When the API returns a refusal content item (Structured Outputs
    safety path), the classifier must mark it as failed and bubble the
    refusal text — strict mode promises validate-or-refuse, both paths
    need to be handled."""
    from catalyst_radar.services.openai_classifier import OpenAIClassifier

    _patch_openai(
        {
            "id": "resp_test",
            "object": "response",
            "created_at": 0,
            "model": "test-model",
            "status": "completed",
            "output": [
                {
                    "id": "msg_test",
                    "type": "message",
                    "role": "assistant",
                    "status": "completed",
                    "content": [
                        {"type": "refusal", "refusal": "cannot comply"}
                    ],
                }
            ],
            "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
        }
    )
    clf = OpenAIClassifier(api_key="sk-test")
    result = await clf.classify(
        company_name="X", symbol="X", title="t", content="c"
    )
    assert result.status == "failed"
    assert result.error is not None and "cannot comply" in result.error


async def test_same_event_sends_strict_boolean_schema(_patch_openai) -> None:
    from catalyst_radar.services.openai_classifier import OpenAIClassifier

    capture = _patch_openai(_canned_reply({"same": True}))
    clf = OpenAIClassifier(api_key="sk-test")
    out = await clf.same_event(
        a_title="a", a_summary="as", b_title="b", b_summary="bs"
    )
    assert out is True
    assert capture["body"]["reasoning"]["effort"] in {"minimal", "none"}
    fmt = capture["body"]["text"]["format"]
    assert fmt["strict"] is True
    assert fmt["schema"]["properties"]["same"]["type"] == "boolean"
    assert fmt["schema"]["required"] == ["same"]


async def test_earnings_extract_sends_strict_schema_with_beat_or_miss_enum(
    monkeypatch, _patch_openai
) -> None:
    """extract_from_url fetches the URL via its own httpx call before the
    LLM call. Stub that fetcher so the LLM transport is the only thing
    the test sees."""
    from catalyst_radar.services import earnings_enrich
    from catalyst_radar.services.earnings_enrich import extract_from_url

    async def fake_fetch(url, *, timeout_seconds):
        return (b"<html>some real earnings text " * 50 + b"</html>", "text/html")

    monkeypatch.setattr(earnings_enrich, "_fetch", fake_fetch)

    capture = _patch_openai(
        _canned_reply(
            {
                "period_label": "Q1 FY26",
                "currency": "USD",
                "revenue": 100.0,
                "revenue_yoy_pct": 12.5,
                "gross_profit": None,
                "gross_margin_pct": None,
                "operating_profit": None,
                "net_profit": 20.0,
                "net_profit_yoy_pct": 15.0,
                "eps": 0.5,
                "eps_yoy_pct": 10.0,
                "beat_or_miss": "beat",
                "guidance_change": "",
                "key_drivers": ["a", "b"],
                "headline": "Q1 revenue +12.5%",
            }
        )
    )
    r = await extract_from_url(
        "https://example.com/r.html",
        company_name="X",
        symbol="X",
        api_key="sk-test",
    )
    assert r.status == "completed"
    assert capture["body"]["reasoning"]["effort"] in {"minimal", "none"}
    fmt = capture["body"]["text"]["format"]
    assert fmt["strict"] is True
    props = fmt["schema"]["properties"]
    assert props["beat_or_miss"]["enum"] == ["beat", "miss", "inline", "unknown"]
    # The SDK serializes nullable Pydantic fields via `anyOf` — both
    # branches are acceptable per OpenAI's strict schema rules.
    assert props["revenue"]["anyOf"] == [{"type": "number"}, {"type": "null"}]
    assert props["key_drivers"]["type"] == "array"


async def test_ipo_summarize_sends_strict_schema(_patch_openai) -> None:
    from catalyst_radar.services.ipo_summarizer import IpoSummarizer

    capture = _patch_openai(
        _canned_reply({"description": "a real desc", "market_cap_usd": 1234567.0})
    )
    s = IpoSummarizer(api_key="sk-test")
    r = await s.summarize(
        company_name="Acme", symbol="ACM", summary_text="business overview..."
    )
    assert r.description == "a real desc"
    assert r.market_cap_usd == 1234567.0
    assert capture["body"]["reasoning"]["effort"] in {"minimal", "none"}
    fmt = capture["body"]["text"]["format"]
    assert fmt["strict"] is True
    props = fmt["schema"]["properties"]
    assert props["description"]["type"] == "string"
    assert props["market_cap_usd"]["anyOf"] == [{"type": "number"}, {"type": "null"}]


# ── Tier 2: web_search call sites ───────────────────────────────────


async def test_websearch_news_fetch_sends_strict_items_schema(_patch_openai) -> None:
    """websearch_news.fetch feeds the entire catalyst pipeline. The
    schema must guarantee {items: [{title, url, published_date, summary}]}
    so the classifier never gets a malformed candidate."""
    from catalyst_radar.adapters.websearch_news import WebSearchNewsAdapter

    capture = _patch_openai(_canned_reply({"items": []}))
    adapter = WebSearchNewsAdapter(api_key="sk-test")
    result = await adapter.fetch(company_name="Acme", symbol="ACM", exchange="US")
    assert result.status == "completed"
    assert capture["body"]["reasoning"]["effort"] in {"minimal", "none"}
    assert capture["body"]["tools"] == [{"type": "web_search"}]
    fmt = capture["body"]["text"]["format"]
    assert fmt["strict"] is True
    # The SDK resolves $ref-style nested item schemas — find the items
    # definition wherever it ended up.
    schema = fmt["schema"]
    items_schema = schema["properties"]["items"]
    assert items_schema["type"] == "array"


async def test_source_discovery_sends_strict_kind_enum(_patch_openai) -> None:
    """The kind enum is the closed-set guard that stops the model from
    inventing source kinds the coercion layer would drop."""
    from catalyst_radar.services.source_discovery import (
        ALLOWED_KINDS,
        OpenAIWebSearchProvider,
    )

    capture = _patch_openai(_canned_reply({"sources": []}))
    provider = OpenAIWebSearchProvider(api_key="sk-test")
    result = await provider.discover(company_name="Acme", symbol="ACM", exchange="US")
    assert result.status == "completed"
    assert capture["body"]["reasoning"]["effort"] in {"minimal", "none"}
    assert capture["body"]["tools"] == [{"type": "web_search"}]
    fmt = capture["body"]["text"]["format"]
    assert fmt["strict"] is True
    # Walk the schema tree to find the kind enum — Pydantic emits a
    # $defs ref for the nested item model.
    schema = fmt["schema"]
    item_def = (
        next(iter(schema.get("$defs", {}).values()))
        if "$defs" in schema
        else schema["properties"]["sources"]["items"]
    )
    assert set(item_def["properties"]["kind"]["enum"]) == ALLOWED_KINDS


async def test_find_consensus_sends_strict_schema_with_sources_array(
    _patch_openai,
) -> None:
    from catalyst_radar.services.earnings_enrich import find_consensus

    capture = _patch_openai(
        _canned_reply(
            {
                "revenue": None,
                "eps": None,
                "net_profit": None,
                "currency": "",
                "period_label": "",
                "sources": [],
                "notes": "",
            }
        )
    )
    result = await find_consensus(
        company_name="Acme",
        symbol="ACM",
        period_label="Q1 FY26",
        api_key="sk-test",
    )
    assert result.status == "completed"
    assert capture["body"]["reasoning"]["effort"] in {"minimal", "none"}
    assert capture["body"]["tools"] == [{"type": "web_search"}]
    fmt = capture["body"]["text"]["format"]
    assert fmt["strict"] is True
    props = fmt["schema"]["properties"]
    assert props["revenue"]["anyOf"] == [{"type": "number"}, {"type": "null"}]


async def test_ipo_describe_via_web_sends_strict_schema(_patch_openai) -> None:
    from catalyst_radar.services.ipo_summarizer import IpoSummarizer

    capture = _patch_openai(_canned_reply({"description": "", "sources": []}))
    s = IpoSummarizer(api_key="sk-test")
    result = await s.describe_via_web(company_name="Acme", symbol="ACM")
    assert result.status == "completed"
    assert capture["body"]["reasoning"]["effort"] in {"minimal", "none"}
    assert capture["body"]["tools"] == [{"type": "web_search"}]
    fmt = capture["body"]["text"]["format"]
    assert fmt["strict"] is True
