"""Gather UKVI pages + web search hits and score confidence for Q&A."""

from __future__ import annotations

from dataclasses import dataclass

from visa_agent.ai_capabilities.ukvi_lookup import UkviSnippet, lookup_ukvi_snippets
from visa_agent.ai_capabilities.web_search import (
    DEFAULT_CONFIDENCE_THRESHOLD,
    SearchHit,
    build_visa_search_query,
    duckduckgo_search,
    host_authority,
    score_research_confidence,
)


@dataclass(frozen=True, slots=True)
class ResearchBundle:
    hits: tuple[SearchHit, ...]
    confidence: float
    used_ukvi: bool
    used_web_search: bool
    threshold: float = DEFAULT_CONFIDENCE_THRESHOLD

    @property
    def high_confidence(self) -> bool:
        return self.confidence >= self.threshold

    @property
    def snippets_for_prompt(self) -> tuple[UkviSnippet, ...]:
        """Reuse UkviSnippet shape for existing reply builders."""
        return tuple(
            UkviSnippet(url=h.url, title=h.title, text=h.snippet)
            for h in self.hits
            if h.snippet or h.title
        )


def gather_research(
    *,
    question: str,
    route: str | None,
    enable_web_search: bool = True,
    ukvi_snippets: tuple[UkviSnippet, ...] | None = None,
    web_hits: tuple[SearchHit, ...] | None = None,
) -> ResearchBundle:
    """UKVI allowlisted pages first, then general web search if needed/useful."""
    ukvi = (
        ukvi_snippets
        if ukvi_snippets is not None
        else lookup_ukvi_snippets(question=question, route=route)
    )
    hits: list[SearchHit] = []
    for s in ukvi:
        auth = max(host_authority(s.url), 0.9)
        hits.append(
            SearchHit(
                url=s.url,
                title=s.title or "GOV.UK",
                snippet=(s.text or "")[:800],
                authority=auth,
                source="ukvi_page",
            )
        )

    used_web = False
    if enable_web_search:
        if web_hits is not None:
            hits.extend(web_hits)
            used_web = bool(web_hits)
        else:
            # Always search for broader coverage; gov.uk alone can be thin.
            query = build_visa_search_query(question, route=route)
            found = duckduckgo_search(query, limit=5)
            used_web = bool(found)
            # Deduplicate by URL.
            seen = {h.url for h in hits}
            for h in found:
                if h.url in seen:
                    continue
                seen.add(h.url)
                hits.append(h)

    # Keep top 6 by authority.
    hits.sort(key=lambda h: h.authority, reverse=True)
    top = tuple(hits[:6])
    conf = score_research_confidence(top, question=question)
    return ResearchBundle(
        hits=top,
        confidence=conf,
        used_ukvi=bool(ukvi),
        used_web_search=used_web,
        threshold=DEFAULT_CONFIDENCE_THRESHOLD,
    )
