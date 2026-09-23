"""Cheap deterministic prefilters. Runs BEFORE the LLM so obvious
low-value items never cost a classification call (PRD Module 5)."""

import re

_NOISE_KEYWORDS = (
    "hackathon",
    "webinar",
    "sponsorship",
    "sponsors ",
    "to sponsor",
    "award nomination",
    "wins award",
    "best places to work",
    "employee appreciation",
    "community outreach",
    "charity",
    "donation",
    "csr ",
    "esg report",
    "diversity and inclusion",
    "to attend",
    "will attend",
    "to present at",
    "to exhibit",
    "booth ",
    "career fair",
    "internship program",
    "newsletter",
)


# Clickbait / opinion-piece / listicle title patterns. These articles
# *reference* corporate facts but don't break news themselves — Yahoo
# Finance editorial, Insider Monkey, Zacks, Motley Fool, etc. recycle
# known catalysts (e.g. Micron's 1α DRAM ramp from a year ago) as
# reasons to buy/sell, and the classifier reads the embedded fact as
# fresh news. Match on the title (lower-cased) only, not the body —
# bodies contain too many normal phrases to safely match on.
_TITLE_NOISE_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p)
    for p in (
        # "X Stocks to Buy", "Top 10 ... Stocks", "15 High Growth Stocks"
        r"\bstocks? to (buy|hold|own|watch|consider|avoid|sell)\b",
        r"\btop \d+\b.*\bstocks?\b",
        r"\b\d+ (high[- ]?growth|undervalued|cheap|hot|best|top|killer|safest|"
        r"smartest|magnificent)\b",
        r"\b(best|top) \d+\b.*\b(stocks?|companies|picks)\b",
        # "Here's Why X" / "Here is Why X" / "Why X is a great"
        r"^here'?s\s+why\b",
        r"^here is\s+why\b",
        r"\bwhy\s+\w+\s+(is|are)\s+(a|the)?\s*(great|solid|terrific|top|best|smart|hot|brilliant|genius)\b",
        # "Should You Buy X", "Is X a Buy", "Is X Still Undervalued",
        # "Is X Stock Still Worth Buying"
        r"\bshould you (buy|sell|hold|own|consider)\b",
        r"\bis\s+\w+\s+(stock\s+)?(\w+\s+){0,2}"
        r"(a buy|a sell|a hold|worth|destined|undervalued|overvalued)\b",
        # "Prediction:" / "Forecast:" opinion preambles
        r"^prediction:",
        r"^forecast:",
        r"^if you'?d invested\b",
        # "X vs Y / X versus Y" comparison pieces
        r"\b\w+\s+vs\.?\s+\w+\s*:\s*which\b",
        r"\b\w+\s+versus\s+\w+\s*:\s*which\b",
        # "Ridiculously cheap", "Trillion-Dollar Club", "Worth $X in N years"
        r"\bridiculously\s+(cheap|undervalued)\b",
        r"\btrillion[- ]dollar club\b",
        r"\bworth\s+(at least\s+)?\$[\d,]+\s+in\s+\d+\s+(year|month)s?\b",
        # "Missed the X Rally", "Missed Out on the AI Rally", "Missed the
        # Initial AI Rally" — allow any words between "the" and "rally".
        r"\bmissed (out on |out )?the\b[^.?!]*\brally\b",
        r"\bmissed (out on |out )?\bthe\s+\w+\s+rally\b",
        # "X% Problem: Why ..." / Apostrophe-style possessive opinion
        r"^\w[\w\s]*'?s\s+\d+\s*%\s+problem\b",
        # "Cramer Says ...", "Jim Cramer Says ..."
        r"^(jim\s+)?cramer\s+(says|told)\b",
        # "Reasons to Buy", "Reasons to Think"
        r"\b\d+\s+reasons\s+to\s+(buy|sell|think|own|avoid|consider)\b",
    )
)


def prefilter_reason(title: str | None, content: str | None) -> str | None:
    """Return an ignore reason if the item is obviously low value, else
    None (meaning: worth sending to the LLM)."""
    title_l = (title or "").lower().strip()
    text = f"{title_l} {(content or '').lower().strip()}".strip()
    if not text:
        return "empty_content"
    if len(text) < 40:
        return "too_short"
    for kw in _NOISE_KEYWORDS:
        if kw in text:
            return f"deterministic_noise:{kw.strip()}"
    if title_l:
        for pat in _TITLE_NOISE_PATTERNS:
            if pat.search(title_l):
                return f"clickbait_title:{pat.pattern[:48]}"
    return None
