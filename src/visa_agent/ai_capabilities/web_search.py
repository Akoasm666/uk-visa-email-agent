"""General web search (DuckDuckGo HTML, no API key) + confidence scoring."""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import parse_qs, unquote, urlparse

import httpx

# Hosts we may fetch full page text for (in addition to search snippets).
_FETCH_HOSTS = frozenset(
    {
        "www.gov.uk",
        "gov.uk",
        "www.legislation.gov.uk",
        "legislation.gov.uk",
        "www.gov.scot",
        "gov.scot",
        "www.citizensadvice.org.uk",
        "citizensadvice.org.uk",
        "www.nidirect.gov.uk",
        "nidirect.gov.uk",
    }
)

_AUTHORITY: tuple[tuple[str, float], ...] = (
    ("www.gov.uk", 0.95),
    ("gov.uk", 0.95),
    ("legislation.gov.uk", 0.92),
    ("www.legislation.gov.uk", 0.92),
    ("gov.scot", 0.88),
    ("nidirect.gov.uk", 0.85),
    ("citizensadvice.org.uk", 0.72),
    ("www.citizensadvice.org.uk", 0.72),
)

# Default: answer without advisor when confidence >= this.
DEFAULT_CONFIDENCE_THRESHOLD = 0.62


@dataclass(frozen=True, slots=True)
class SearchHit:
    url: str
    title: str
    snippet: str
    authority: float
    source: str  # duckduckgo | ukvi_page


_RESULT_A_RE = re.compile(
    r'<a[^>]*class="[^"]*result__a[^"]*"[^>]*href="(?P<href>[^"]+)"[^>]*>'
    r"(?P<title>.*?)</a>",
    re.I | re.S,
)
_SNIPPET_RE = re.compile(
    r'class="[^"]*result__snippet[^"]*"[^>]*>(?P<snippet>.*?)</(?:a|div)>',
    re.I | re.S,
)


def _clean_html_text(raw: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", raw or "")).strip()


def _unwrap_ddg(href: str) -> str:
    raw = (href or "").strip()
    if not raw:
        return ""
    if raw.startswith("//"):
        raw = "https:" + raw
    if "uddg=" in raw:
        try:
            q = urlparse(raw if "://" in raw else "https://duckduckgo.com" + raw).query
            uddg = parse_qs(q).get("uddg", [""])[0]
            if uddg:
                return unquote(uddg)
        except Exception:  # noqa: BLE001
            pass
    if raw.startswith("http"):
        return raw
    return ""


def host_authority(url: str) -> float:
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:  # noqa: BLE001
        return 0.35
    for needle, score in _AUTHORITY:
        if host == needle or host.endswith("." + needle):
            return score
    if host.endswith(".gov.uk"):
        return 0.88
    if host.endswith(".ac.uk"):
        return 0.65
    if host.endswith(".org.uk"):
        return 0.55
    return 0.4


def _may_fetch(url: str) -> bool:
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:  # noqa: BLE001
        return False
    if host in _FETCH_HOSTS:
        return True
    return any(host.endswith("." + h) for h in _FETCH_HOSTS if not h.startswith("www."))


def _relevance(question: str, title: str, snippet: str) -> float:
    q_tokens = {t for t in re.findall(r"[\w\u4e00-\u9fff]{2,}", (question or "").lower())}
    if not q_tokens:
        return 0.5
    blob = f"{title} {snippet}".lower()
    hit = sum(1 for t in q_tokens if t in blob)
    return min(1.0, 0.35 + 0.65 * (hit / max(len(q_tokens), 1)))


def duckduckgo_search(
    query: str,
    *,
    limit: int = 5,
    html: str | None = None,
) -> tuple[SearchHit, ...]:
    """Keyless DuckDuckGo HTML search. Never raises — empty on failure.

    Pass ``html`` in tests to skip the network.
    """
    q = (query or "").strip()
    if not q:
        return ()
    if html is None:
        try:
            with httpx.Client(
                timeout=18.0,
                follow_redirects=True,
                headers={"User-Agent": "visa-email-agent/0.1 (+research)"},
            ) as client:
                resp = client.post(
                    "https://html.duckduckgo.com/html/",
                    data={"q": q},
                )
                resp.raise_for_status()
                html = resp.text
        except Exception:  # noqa: BLE001
            try:
                with httpx.Client(
                    timeout=18.0,
                    follow_redirects=True,
                    headers={"User-Agent": "visa-email-agent/0.1 (+research)"},
                ) as client:
                    resp = client.get(
                        "https://html.duckduckgo.com/html/",
                        params={"q": q},
                    )
                    resp.raise_for_status()
                    html = resp.text
            except Exception:  # noqa: BLE001
                return ()

    hits: list[SearchHit] = []
    seen: set[str] = set()
    matches = list(_RESULT_A_RE.finditer(html or ""))
    for idx, match in enumerate(matches):
        if len(hits) >= limit:
            break
        url = _unwrap_ddg(match.group("href"))
        if not url or url in seen:
            continue
        host = (urlparse(url).hostname or "").lower()
        if "duckduckgo.com" in host:
            continue
        next_start = matches[idx + 1].start() if idx + 1 < len(matches) else len(html)
        window = html[match.end() : next_start]
        snip_m = _SNIPPET_RE.search(window)
        snip = _clean_html_text(snip_m.group("snippet")) if snip_m else ""
        title = _clean_html_text(match.group("title"))
        seen.add(url)
        auth = host_authority(url)
        rel = _relevance(q, title, snip)
        score = min(0.98, 0.55 * auth + 0.45 * rel)
        hits.append(
            SearchHit(
                url=url,
                title=title[:200],
                snippet=(snip or "")[:600],
                authority=score,
                source="duckduckgo",
            )
        )
    return tuple(hits)


_EVIDENCE_TOPICS = (
    (r"资金|流水|存款|余额|\b(?:funds|savings|money|bank|balance|support yourself)\b",
     r"资金|流水|存款|\b(?:funds|savings|financial evidence|bank statements?|support yourself|support themselves)\b"),
    (r"多久.{0,8}出签|出签|审理|processing time|decision time|how long.{0,25}decision",
     r"办理时间|\b(?:processing times?|working days|weeks?|get a decision)\b"),
    (r"护照|资料页|\bpassport\b", r"护照|\b(?:passport|travel document)\b"),
    (r"翻译|译文|\btranslat", r"翻译|\btranslat"),
    (r"担保|资助|\b(?:sponsor|sponsorship)\b", r"担保|资助|\b(?:sponsor|sponsorship|third party)\b"),
    (r"费用|多少钱|\b(?:fees?|charge)\b", r"费用|\b(?:fees?|charges?|costs?)\b|£\s*\d"),
    (r"预约|生物识别|指纹|\b(?:biometric|appointment)\b", r"预约|\b(?:biometric|appointment)"),
)


def score_research_confidence(
    hits: tuple[SearchHit, ...] | list[SearchHit], *, question: str = "",
) -> float:
    """Require excerpt relevance before publisher authority can clear an answer."""
    if not hits:
        return 0.0
    official = [hit for hit in hits if host_authority(hit.url) >= 0.85 and hit.snippet.strip()]
    if not official:
        return 0.0
    topics = [match for ask, match in _EVIDENCE_TOPICS if re.search(ask, question, re.I)]
    if not topics or not all(
        any(re.search(topic, hit.snippet, re.I) for hit in official) for topic in topics
    ):
        return 0.0
    return round(max(host_authority(hit.url) for hit in official), 3)


def build_visa_search_query(question: str, *, route: str | None) -> str:
    base = (question or "").strip()
    route_bit = {
        "standard_visitor": "UK Standard Visitor visa",
        "student": "UK Student visa",
        "skilled_worker": "UK Skilled Worker visa",
        "graduate": "UK Graduate visa",
    }.get((route or "").strip(), "UK visa")
    return f"{route_bit} GOV.UK {base}"[:240]
