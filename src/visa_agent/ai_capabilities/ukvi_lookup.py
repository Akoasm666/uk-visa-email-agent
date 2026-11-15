"""Allowlisted GOV.UK / UKVI page fetch for grounded customer Q&A."""

from __future__ import annotations

import re
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Callable
from urllib.parse import urlparse

import httpx

from visa_agent.domain.enums import RouteCode
from visa_agent.rules.seed import (
    GRADUATE_SOURCE_URL,
    SKILLED_WORKER_SOURCE_URL,
    STUDENT_SOURCE_URL,
    VISITOR_SOURCE_URL,
)

# Only these hosts may be fetched — never follow arbitrary model-suggested URLs.
_ALLOWED_HOSTS = frozenset({"www.gov.uk", "gov.uk"})

_ROUTE_HOME: dict[str, str] = {
    RouteCode.STANDARD_VISITOR.value: "https://www.gov.uk/standard-visitor",
    RouteCode.STUDENT.value: STUDENT_SOURCE_URL,
    RouteCode.SKILLED_WORKER.value: SKILLED_WORKER_SOURCE_URL,
    RouteCode.GRADUATE.value: GRADUATE_SOURCE_URL,
}

_TOPIC_URLS: tuple[tuple[tuple[str, ...], str], ...] = (
    (
        ("passport", "护照", "travel document", "bio-data", "资料页"),
        VISITOR_SOURCE_URL,
    ),
    (
        ("bank", "funds", "资金", "流水", "money", "financial"),
        VISITOR_SOURCE_URL,
    ),
    (
        ("visitor", "访问", "tourism", "探亲", "standard visitor"),
        "https://www.gov.uk/standard-visitor",
    ),
    (
        ("student", "学生", "cas", "tuition"),
        STUDENT_SOURCE_URL,
    ),
    (
        ("skilled", "工签", "cos", "sponsorship", "sponsor licence"),
        SKILLED_WORKER_SOURCE_URL,
    ),
    (
        ("graduate", "毕业生"),
        GRADUATE_SOURCE_URL,
    ),
    (
        ("biometric", "brp", "appointment", "vfs"),
        "https://www.gov.uk/visa-to-come-to-the-uk",
    ),
)


@dataclass(frozen=True, slots=True)
class UkviSnippet:
    url: str
    title: str
    text: str


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._chunks: list[str] = []
        self._skip = 0
        self.title = ""
        self._in_title = False

    def handle_starttag(self, tag: str, attrs) -> None:  # noqa: ANN001
        if tag in {"script", "style", "noscript", "svg"}:
            self._skip += 1
        if tag == "title":
            self._in_title = True

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript", "svg"} and self._skip:
            self._skip -= 1
        if tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._skip:
            return
        text = " ".join(data.split())
        if not text:
            return
        if self._in_title and not self.title:
            self.title = text
        else:
            self._chunks.append(text)

    def text(self) -> str:
        return " ".join(self._chunks)


def _host_allowed(url: str) -> bool:
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:  # noqa: BLE001
        return False
    return host in _ALLOWED_HOSTS


def select_ukvi_urls(*, question: str, route: str | None) -> tuple[str, ...]:
    """Pick a small allowlisted set of GOV.UK pages for this question + route."""
    q = (question or "").lower()
    chosen: list[str] = []
    for needles, url in _TOPIC_URLS:
        if any(n.lower() in q for n in needles):
            if url not in chosen:
                chosen.append(url)
    route_key = (route or RouteCode.STANDARD_VISITOR.value).strip()
    home = _ROUTE_HOME.get(route_key, _ROUTE_HOME[RouteCode.STANDARD_VISITOR.value])
    if home not in chosen:
        chosen.insert(0, home)
    # Always include the supporting-documents guide for visitor material Qs.
    if route_key == RouteCode.STANDARD_VISITOR.value and VISITOR_SOURCE_URL not in chosen:
        chosen.append(VISITOR_SOURCE_URL)
    return tuple(chosen[:3])


def _html_to_snippet(html: str, *, max_chars: int = 2400) -> tuple[str, str]:
    parser = _TextExtractor()
    try:
        parser.feed(html)
        parser.close()
    except Exception:  # noqa: BLE001
        return "", ""
    text = re.sub(r"\s+", " ", parser.text()).strip()
    if len(text) > max_chars:
        text = text[: max_chars - 1].rstrip() + "…"
    return parser.title, text


FetchFn = Callable[[str], str]


def _default_fetch(url: str) -> str:
    with httpx.Client(
        timeout=20.0,
        follow_redirects=True,
        headers={"User-Agent": "visa-email-agent/0.1 (+allowlisted-ukvi-lookup)"},
    ) as client:
        resp = client.get(url)
        resp.raise_for_status()
        return resp.text


def lookup_ukvi_snippets(
    *,
    question: str,
    route: str | None,
    fetch: FetchFn | None = None,
) -> tuple[UkviSnippet, ...]:
    """Fetch allowlisted GOV.UK pages and return plain-text snippets.

    Network failures are skipped (empty tuple / partial) so Q&A can still use a
    safe template fallback — never invent policy from a failed fetch.
    """
    fetch_fn = fetch or _default_fetch
    out: list[UkviSnippet] = []
    for url in select_ukvi_urls(question=question, route=route):
        if not _host_allowed(url):
            continue
        try:
            html = fetch_fn(url)
        except Exception:  # noqa: BLE001
            # Keep the allowlisted URL so Q&A can still cite GOV.UK when offline.
            out.append(UkviSnippet(url=url, title="GOV.UK", text=""))
            continue
        title, text = _html_to_snippet(html)
        out.append(UkviSnippet(url=url, title=title or "GOV.UK", text=text or ""))
    return tuple(out)
