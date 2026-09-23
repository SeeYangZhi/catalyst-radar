"""Shared HK stock-code canonicalizer.

The AAStocks calendar, the HKEXnews New Listing Report, and the
HKEXnews prospectus adapters all key HK IPO rows on the stock code
(``hkex:ipo:<code>``), so they MUST canonicalize identically or a
footnote-contaminated cell on one side silently misses the match on the
other (e.g. the listed-flip in ``hk_ipo_enrich``). One implementation
lives here; the adapters import it.
"""

import re
from typing import Any

_DIGIT_RUN = re.compile(r"\d+")


def pad_code(raw: Any) -> str | None:
    """HK board lot codes are <=5 digits; normalise to zero-padded 5.

    Only the first contiguous digit run counts, so a footnoted cell like
    "2618 (Note 1)" yields "02618", not "26181". Returns ``None`` when
    the input carries no digits at all."""
    m = _DIGIT_RUN.search(str(raw or ""))
    if m is None:
        return None
    return m.group().zfill(5)[-5:]
