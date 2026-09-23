import hashlib
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

_TRACKING_PREFIXES = ("utm_", "fbclid", "gclid", "mc_", "ref")
# Path segments that mark a mobile/AMP variant of the same content. Unitree
# serves /cn/H2plus/ and /cn/mobile/H2plus/ as the same product page; Google
# AMP mirrors live at /amp/<id>. Stripping these segments collapses the
# variants to one canonical URL so dedup catches them as the same article.
_VARIANT_SEGMENTS = frozenset({"mobile", "amp"})


def stable_hash(*parts: str | None) -> str:
    """Deterministic short hash for dedup keys built from source fields."""
    joined = "|".join("" if p is None else str(p).strip() for p in parts)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:32]


def normalize_url(url: str | None) -> str:
    """Canonicalize a URL for cross-source URL-exact dedup.

    Lowercases scheme/host, drops default ports and fragments, trims a trailing
    slash, strips tracking query params (utm_*, fbclid, …) while preserving
    meaningful ones (e.g. ?id=123) in a stable order, and drops mobile/AMP
    path segments so /cn/mobile/H2plus/ and /cn/H2plus/ collapse. Conservative
    on purpose — we only want to merge URLs that are genuinely the same
    article, never two different ones. Never raises: malformed URLs fall
    back to a lowercased trimmed string.
    """
    if not url:
        return ""
    raw = url.strip()
    try:
        p = urlsplit(raw)
        scheme = (p.scheme or "https").lower()
        host = (p.hostname or "").lower()
        # p.port raises ValueError for an out-of-range/non-numeric port.
        port = "" if p.port in (None, 80, 443) else f":{p.port}"
        segments = [s for s in p.path.split("/") if s and s.lower() not in _VARIANT_SEGMENTS]
        path = ("/" + "/".join(segments)) if segments else "/"
        kept = sorted(
            (k, v)
            for k, v in parse_qsl(p.query)
            if not any(k.lower().startswith(pre) for pre in _TRACKING_PREFIXES)
        )
        return urlunsplit((scheme, host + port, path, urlencode(kept), ""))
    except ValueError:
        return raw.lower()


def content_dedup_key(symbol: str | None, exchange: str | None, url: str | None) -> str:
    """Per-company, source-agnostic dedup key keyed on the canonical URL.

    Includes symbol+exchange so two *different* tracked companies that happen to
    reference the same article URL do NOT collide (the spec's content_hash is
    keyed on the symbol). EODHD and web_search produce the same key for the same
    company+URL, so the second source is recognized as a duplicate."""
    return stable_hash("catalyst-url", symbol, exchange, normalize_url(url))


def content_text_key(symbol: str | None, exchange: str | None, title: str | None) -> str:
    """Per-company dedup key for link-less items, keyed on normalized title.

    Lets a link-less item from two different sources still collapse onto one
    alert (URL-exact can't, since there is no URL to key on)."""
    return stable_hash("catalyst-text", symbol, exchange, (title or "").strip().lower())


def source_event_id(source: str, *parts: str | None) -> str:
    """Stable cross-sync source event id, e.g. eodhd:earnings:AAPL.US:2026-05-07."""
    tail = ":".join("" if p is None else str(p).strip() for p in parts)
    return f"{source}:{tail}"


def dedup_key(*parts: str | None) -> str:
    """Deterministic dedup key for events/notifications."""
    return stable_hash(*parts)
