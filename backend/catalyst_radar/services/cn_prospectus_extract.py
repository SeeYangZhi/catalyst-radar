"""Section-aware extraction for A-share prospectus PDFs.

Mainland-China 招股说明书 run 200-800 pages — the existing front-matter
slice that works for HK (`hkex_prospectus.extract_summary_from_pdf`)
captures < 10 % of an A-share document and reliably misses the
business section. A-share prospectuses follow a stable章节 (chapter)
template; the business chapter is conventionally `第五节 业务与技术`
or `第六节 业务与技术` — sometimes 业务概况, 主要业务, or 业务及技术.

This extractor:
  1. Reads pages sequentially.
  2. Locates the business-chapter heading.
  3. Returns text from that page forward, stopping at the next
     `第X节` chapter heading or after `max_chars` whichever comes first.
  4. Falls back to the front-matter (first N pages) when no heading is
     found — keeps a degraded path so the downstream summarizer still
     gets *something* to work with.

Pure helpers are exposed for fixture-based testing without a real PDF.
"""

from __future__ import annotations

import io
import re

from catalyst_radar.logging import get_logger

log = get_logger(__name__)

# Match the business-chapter heading. 第[一-十]节 + a "business" keyword.
# Examples seen in real filings:
#   "第五节  业务与技术"
#   "第六节 业务与技术"
#   "第六节 业务和技术"
#   "第四节 业务概况"
# Allow 1-3 spaces between 节 and the section name; the heading often
# also appears in the front-matter table-of-contents, so we look for the
# heading on a page whose first appearance is preceded by enough text
# that it's clearly content, not the TOC entry.
_HEADING_RE = re.compile(
    r"第\s*[一二三四五六七八九十]\s*节\s*(?:业务\s*[与和及]\s*技术|业务\s*概况|主要\s*业务|业务\s*情况)"
)

# A next-chapter heading we stop at (any 第X节 that isn't the one we
# entered). We capture the chapter marker number to avoid stopping at
# the same one if it gets repeated as a page header.
_NEXT_CHAPTER_RE = re.compile(r"第\s*[一二三四五六七八九十]\s*节\s*[^\s]")


def _is_business_heading(text: str) -> bool:
    """Return True if this page contains the *real* business-chapter
    heading, not the TOC line. The heuristic: the heading must appear
    after at least ~200 chars of other text on the page (TOC pages dump
    a stack of chapter headings near the top with dot-leader '....')."""
    m = _HEADING_RE.search(text)
    if m is None:
        return False
    # TOC entries always contain `..........` dot-leaders within ~50
    # chars of the heading. Real chapter pages do not.
    window = text[max(0, m.start() - 60) : m.end() + 60]
    if "....." in window:
        return False
    return True


def extract_business_section(
    pdf_bytes: bytes, max_chars: int = 80_000, max_pages: int = 200
) -> tuple[str, dict]:
    """Pure: prospectus PDF bytes -> (text slice, meta dict).

    meta carries `pages_total`, `start_page` (1-indexed of business
    chapter), `end_page`, and `fallback` (True if heading not found and
    front-matter was returned instead).
    """
    from pypdf import PdfReader

    meta: dict = {"pages_total": 0, "start_page": None, "end_page": None, "fallback": False}
    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
    except Exception as exc:  # noqa: BLE001 - corrupt PDF
        log.warning("cn_prospectus_unreadable", error=repr(exc))
        return ("", meta)
    pages = reader.pages
    meta["pages_total"] = len(pages)
    if not pages:
        return ("", meta)

    cap = min(max_pages, len(pages))

    # 1. Locate the business-chapter heading.
    start_idx: int | None = None
    for i in range(cap):
        try:
            text = pages[i].extract_text() or ""
        except Exception:  # noqa: BLE001
            continue
        if _is_business_heading(text):
            start_idx = i
            break

    if start_idx is None:
        # Fallback: dump the first 30 pages as a front-matter slice.
        # Better than nothing — covers the offering summary up front.
        meta["fallback"] = True
        chunks: list[str] = []
        for i in range(min(30, cap)):
            try:
                chunks.append(pages[i].extract_text() or "")
            except Exception:  # noqa: BLE001
                chunks.append("")
        text = " ".join(re.sub(r"\s+", " ", c) for c in chunks)
        return (text[:max_chars], meta)

    # 2. Extract from the heading forward until the next chapter or until
    # we hit the char budget.
    out: list[str] = []
    out_len = 0
    end_idx = start_idx
    for i in range(start_idx, cap):
        try:
            page_text = pages[i].extract_text() or ""
        except Exception:  # noqa: BLE001
            page_text = ""

        # On pages AFTER the start, if we see a next-chapter heading
        # (one we did not enter at), stop — we've left the business chapter.
        if i > start_idx:
            for m in _NEXT_CHAPTER_RE.finditer(page_text):
                # Skip the case where it's just the running header of the
                # SAME chapter — the start page's heading frequently
                # repeats on every page header.
                snippet = page_text[m.start() : m.end() + 30]
                if not _HEADING_RE.search(snippet):
                    # Truncate this page to before the next chapter.
                    page_text = page_text[: m.start()]
                    out.append(re.sub(r"\s+", " ", page_text))
                    out_len += len(page_text)
                    end_idx = i
                    text = " ".join(out)
                    meta["start_page"] = start_idx + 1
                    meta["end_page"] = end_idx + 1
                    return (text[:max_chars], meta)

        cleaned = re.sub(r"\s+", " ", page_text)
        out.append(cleaned)
        out_len += len(cleaned)
        end_idx = i
        if out_len >= max_chars:
            break

    meta["start_page"] = start_idx + 1
    meta["end_page"] = end_idx + 1
    return ((" ".join(out))[:max_chars], meta)
