from dataclasses import dataclass, field
from typing import Any

from openai import APIError, AsyncOpenAI

from catalyst_radar.config import settings
from catalyst_radar.logging import get_logger
from catalyst_radar.prompt_context import current_date_line
from catalyst_radar.schemas.llm import (
    CatalystClassification,
    SameEventJudgement,
)
from catalyst_radar.schemas.llm import (
    EventSubtype as _EventSubtype,
)

log = get_logger(__name__)

PROMPT_VERSION = settings.catalyst_prompt_version

# Exported for tests + catalyst_sync — the canonical event_subtype enum
# lives in catalyst_radar.schemas.llm.EventSubtype; this is its values
# list for runtime checks.
EVENT_SUBTYPES: list[str] = list(_EventSubtype.__args__)

_SYSTEM = (
    "You are a financial catalyst classifier for a single trader's "
    "watchlist. Decide whether a news item is a company-critical catalyst "
    "for the given company. Company-critical means it BREAKS NEW "
    "INFORMATION that can move the stock or change the investment "
    "narrative: product launch, major business development, major "
    "partnership/customer, investor/analyst day, regulatory decision, "
    "lockup/share unlock, uplisting, guidance change, management change, "
    "major financing/issuance. "
    "\n\n"
    "Explicitly NOT critical:\n"
    "- hackathons, minor awards, routine marketing, generic ESG/community "
    "content, minor conference attendance, articles that only mention "
    "the company in passing;\n"
    "- **opinion / commentary / analysis pieces that re-cite already-"
    "known corporate facts as reasons to buy or sell the stock.** This "
    "is the most common failure mode. Articles like 'Here's Why X Is a "
    "Great Growth Stock', 'X Is Among the N Stocks to Buy', 'X vs Y: "
    "Which Is the Better Buy', 'Should You Buy X', 'Prediction: X Will "
    "Be Worth $N', 'Missed the AI Rally? Try These N Stocks' are "
    "analyst commentary, not news — even when their body summarises a "
    "real prior catalyst (a factory ramp, a contract win, an earnings "
    "beat), they are recycling, not announcing;\n"
    "- recap / sum-up pieces that say 'X recently announced...' or "
    "'X said in its last earnings call...' — the fact is real, but the "
    "ARTICLE is not the catalyst; the catalyst was the underlying "
    "announcement, which would have come through a press release / "
    "official filing / breaking news source.\n"
    "\n"
    "**Newness anchor rule**: classify as company-critical ONLY when the "
    "article itself reports something happening NOW — phrasing like "
    "'announced today', 'released earlier today', 'filed with the SEC "
    "today', 'said in a press release this morning', 'reported Q[N] "
    "results this morning', 'effective immediately'. If the article's "
    "subject is a fact that occurred days/weeks/months ago and is being "
    "re-cited as commentary, set is_company_critical=false with "
    "ignore_reason='recap_or_commentary'.\n"
    "\n"
    "Whenever you reference a stock ticker in any free-text field "
    "(expected_impact, summary, why_it_matters, ignore_reason), prefix it "
    "with a dollar sign — e.g. $AAPL, $0700, $TSLA — never write the bare "
    "symbol. "
    "\n\n"
    "**English-only output**: ALL free-text fields (summary, "
    "expected_impact, why_it_matters, ignore_reason) MUST be written in "
    "English even when the source article is in Chinese, Japanese, or "
    "Korean. Translate domain terms (电池管理系统 → battery management "
    "system, 储能 → energy storage, 解禁 → share unlock, 业绩预增 → "
    "preliminary earnings guidance up). Never embed a CJK character in "
    "any free-text field — not for product names, not for industry "
    "terms, not for the company itself (use its filed English / pinyin "
    "form if you need to refer to it; the alert card title carries the "
    "original name separately).\n"
    "\n"
    "Pick the closest matching event_subtype from the enum — use "
    "'analyst_action' for genuine rating/price-target changes from a "
    "real sell-side analyst (not opinion pieces), and 'other' only "
    "when nothing else fits. When the item is not company-critical, set "
    "event_subtype to 'other' and populate ignore_reason; otherwise set "
    "ignore_reason to an empty string."
    "\n\n"
    "**story_key** is a canonical identifier for the underlying event so "
    "two articles about the SAME event collapse to the same key for "
    "dedup. Format: `<symbol_lowercase>_<event_slug>`. Rules:\n"
    "- All lowercase, snake_case, ASCII only (transliterate CJK to pinyin "
    "or drop), max 80 chars.\n"
    "- **NEVER append a date.** The event date is stored separately. "
    "Including a date (`_2026-05-28`, `_2026-05`, etc.) fragments dedup "
    "across re-reporting — outlet A files on day N, outlet B files on "
    "day N+1, they would emit different keys for the same event.\n"
    "- **Controlled-vocabulary slugs for the most-confused subtypes** "
    "(if your subtype is one of these, the slug MUST follow the rule):\n"
    "    * event_subtype='uplisting' → slug is EXACTLY one of "
    "`ipo_acceptance` | `ipo_inquiry` | `ipo_review_scheduled` | "
    "`ipo_review_pass` | `ipo_review_reject` | `ipo_listed` | "
    "`ipo_withdrawn` | `ipo_resumed`. Pick the single stage the article "
    "reports; do NOT invent multi-noun phrases like "
    "`star_ipo_approval_and_q1_guidance`.\n"
    "    * event_subtype='partnership' → slug is "
    "`partnership_<counterparty>` where <counterparty> is the partner's "
    "ticker (lowercase, no dollar sign) if known, else the partner's "
    "company short name with corporate suffixes stripped "
    "(Corporation/Inc/Ltd/Co/Group/Robotics dropped) and "
    "non-alphanumeric replaced with `_`. Pick ONE counterparty per "
    "story — for a NVIDIA × Unitree deal both A26029 and NVDA articles "
    "emit slug `partnership_nvda` / `partnership_a26029` respectively.\n"
    "    * event_subtype='product_launch' → slug is "
    "`launch_<product_codename>` where <product_codename> is the "
    "shortest unique product name (e.g. `launch_h2_plus`, "
    "`launch_lpddr5x`, `launch_iphone17`).\n"
    "- For all other subtypes use a short slug: e.g. `q1_earnings`, "
    "`share_buyback_announcement`, `factory_expansion_arizona`, "
    "`acquires_<targetname>`. Still NO date.\n"
    "- Two articles about the same event MUST emit the same key "
    "regardless of source language, outlet, or wording. Do NOT include "
    "outlet-specific framing, opinion qualifiers, or paraphrasing.\n"
    "- Examples — these three articles all describe CXMT clearing its "
    "STAR Market IPO review and MUST all emit `a25310_ipo_review_pass` "
    "(no date, controlled-vocab slug):\n"
    "    * 'CXMT Corp passes IPO review, plans 2nd-largest fundraising'\n"
    "    * '长鑫科技科创板IPO通过上市委审议'\n"
    "    * '국산 메모리 굴기 CXMT 상장 초읽기'\n"
    "- For non-catalyst items (is_company_critical=false), set "
    "story_key to the empty string."
)


class OpenAIConfigError(RuntimeError):
    """Raised when the OpenAI API key is not configured."""


@dataclass(slots=True)
class ClassificationResult:
    status: str  # completed|failed|incomplete
    output: dict[str, Any] | None
    response_id: str | None = None
    model: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    error: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


def reasoning_off_for(model: str) -> dict[str, str]:
    """Strongest 'don't reason' value the model's Responses API accepts. The
    enum is per-model: gpt-5.4-* accepts 'none' (but not 'minimal'); gpt-5-*
    accepts 'minimal' (but not 'none'). Both reasoning_tokens drop to 0 with
    these. Defaults to 'minimal' for unknown models — universally supported
    on gpt-5-class snapshots."""
    if model.startswith("gpt-5.4"):
        return {"effort": "none"}
    return {"effort": "minimal"}


def _extract_refusal(response: Any) -> str | None:
    """The Responses API surfaces Structured-Outputs safety refusals as a
    `refusal` content item. The SDK leaves these in `response.output`
    rather than raising — we surface them as a failed-status result so the
    caller can log them and move on."""
    for item in getattr(response, "output", None) or []:
        if getattr(item, "type", None) != "message":
            continue
        for part in getattr(item, "content", None) or []:
            refusal = getattr(part, "refusal", None)
            if refusal:
                return str(refusal)
    return None


class OpenAIClassifier:
    def __init__(self, api_key: str | None = None) -> None:
        self.api_key = api_key if api_key is not None else settings.openai_api_key
        self.model = settings.catalyst_classifier_model
        self.timeout = settings.openai_timeout_seconds
        self._client = (
            AsyncOpenAI(api_key=self.api_key, timeout=self.timeout)
            if self.api_key
            else None
        )

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    async def aclose(self) -> None:
        """Close the underlying HTTP client deterministically. The OpenAI
        SDK otherwise schedules an aclose() from __del__ onto whatever
        event loop is running at GC time — under Celery's loop-per-task
        model that's a *later* task's loop, which closes before the
        scheduled close completes ('Task exception was never retrieved'
        noise in the worker log)."""
        if self._client is not None:
            await self._client.close()
            self._client = None

    def _require_client(self) -> AsyncOpenAI:
        if self._client is None:
            raise OpenAIConfigError("OPENAI_API_KEY is not configured")
        return self._client

    async def classify(
        self, *, company_name: str, symbol: str, title: str, content: str
    ) -> ClassificationResult:
        client = self._require_client()
        ticker = f"${symbol}" if symbol and not symbol.startswith("$") else (symbol or "")
        user = f"Company: {company_name} ({ticker})\nHeadline: {title}\nBody: {content[:4000]}"
        try:
            response = await client.responses.parse(
                model=self.model,
                input=[
                    {"role": "system", "content": _SYSTEM + current_date_line()},
                    {"role": "user", "content": user},
                ],
                text_format=CatalystClassification,
                temperature=settings.catalyst_classifier_temperature,
                max_output_tokens=settings.catalyst_classifier_max_output_tokens,
                reasoning=reasoning_off_for(self.model),
            )
        except APIError as exc:
            log.warning(
                "openai_classify_api_error",
                model=self.model,
                symbol=symbol,
                status=getattr(exc, "status_code", None),
                error=repr(exc),
            )
            return ClassificationResult("failed", None, error=repr(exc))

        usage = response.usage
        meta = {
            "response_id": response.id,
            "model": response.model,
            "input_tokens": getattr(usage, "input_tokens", None),
            "output_tokens": getattr(usage, "output_tokens", None),
        }
        # Per-call observability — answers "what did today cost?" without
        # paging the OpenAI dashboard. Sums roll up via log aggregation.
        log.info(
            "openai_classify_call",
            model=meta["model"],
            symbol=symbol,
            status=response.status,
            input_tokens=meta["input_tokens"],
            output_tokens=meta["output_tokens"],
        )

        if response.status != "completed":
            return ClassificationResult(
                response.status,
                None,
                **meta,
                error=str(response.incomplete_details or response.status),
            )

        refusal = _extract_refusal(response)
        if refusal is not None:
            return ClassificationResult(
                "failed", None, **meta, error=f"refusal: {refusal}"
            )

        parsed = response.output_parsed
        if parsed is None:
            return ClassificationResult(
                "failed", None, **meta, error="empty output_parsed"
            )

        return ClassificationResult(
            "completed", parsed.model_dump(), **meta
        )

    async def same_event(
        self, *, a_title: str, a_summary: str, b_title: str, b_summary: str
    ) -> bool | None:
        """Grey-zone dedup judge: do two items describe the SAME underlying
        catalyst (same announcement), not merely the same company/topic?

        Returns True/False, or None if the call could not be made/parsed — the
        caller treats None conservatively (do not merge)."""
        if self._client is None:
            return None
        try:
            response = await self._client.responses.parse(
                model=self.model,
                input=[
                    {
                        "role": "system",
                        "content": (
                            "You decide whether two news items report the SAME underlying "
                            "corporate event/announcement (e.g. the same earnings release, "
                            "the same deal), not just the same company or topic."
                            + current_date_line()
                        ),
                    },
                    {
                        "role": "user",
                        "content": (
                            f"A title: {a_title}\nA summary: {a_summary}\n\n"
                            f"B title: {b_title}\nB summary: {b_summary}"
                        ),
                    },
                ],
                text_format=SameEventJudgement,
                # Reasoning is off and the output is a single boolean, so 200
                # tokens is plenty. Old budget of 1000+ existed to absorb
                # chain-of-thought we no longer pay for.
                max_output_tokens=200,
                reasoning=reasoning_off_for(self.model),
            )
        except APIError as exc:
            log.warning(
                "openai_same_event_api_error",
                model=self.model,
                status=getattr(exc, "status_code", None),
                error=repr(exc),
            )
            return None
        if response.status != "completed" or response.output_parsed is None:
            return None
        return response.output_parsed.same
