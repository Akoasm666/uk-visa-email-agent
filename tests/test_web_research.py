"""Web search + research confidence for customer Q&A."""

from __future__ import annotations

import unittest

from visa_agent.ai_capabilities.research import gather_research
from visa_agent.ai_capabilities.ukvi_lookup import UkviSnippet
from visa_agent.ai_capabilities.web_search import (
    SearchHit,
    duckduckgo_search,
    score_research_confidence,
)


_SAMPLE_DDG = """
<html><body>
<a class="result__a" href="https://duckduckgo.com/l/?uddg=https%3A%2F%2Fwww.gov.uk%2Fstandard-visitor">
UK Standard Visitor visa - GOV.UK</a>
<a class="result__snippet">Show you can support yourself during your trip.</a>
<a class="result__a" href="https://duckduckgo.com/l/?uddg=https%3A%2F%2Fwww.citizensadvice.org.uk%2Fimmigration%2F">
Visitor visas - Citizens Advice</a>
<div class="result__snippet">Evidence of funds for your stay in the UK.</div>
</body></html>
"""


class WebResearchTests(unittest.TestCase):
    def test_parse_ddg_html(self) -> None:
        hits = duckduckgo_search("UK visitor funds", html=_SAMPLE_DDG)
        self.assertGreaterEqual(len(hits), 1)
        self.assertTrue(any("gov.uk" in h.url for h in hits))
        self.assertGreater(hits[0].authority, 0.5)

    def test_high_confidence_with_gov_uk(self) -> None:
        hits = (
            SearchHit(
                url="https://www.gov.uk/standard-visitor",
                title="Standard Visitor",
                snippet="funds support yourself",
                authority=0.95,
                source="ukvi_page",
            ),
            SearchHit(
                url="https://www.citizensadvice.org.uk/immigration/",
                title="Visitor visas",
                snippet="evidence of funds",
                authority=0.72,
                source="duckduckgo",
            ),
        )
        conf = score_research_confidence(hits)
        self.assertGreaterEqual(conf, 0.62)

    def test_gather_merges_ukvi_and_web(self) -> None:
        bundle = gather_research(
            question="如何准备资金证明？",
            route="standard_visitor",
            ukvi_snippets=(
                UkviSnippet(
                    url="https://www.gov.uk/standard-visitor",
                    title="Visitor",
                    text="support yourself",
                ),
            ),
            web_hits=(
                SearchHit(
                    url="https://www.citizensadvice.org.uk/x",
                    title="Funds",
                    snippet="bank statements",
                    authority=0.7,
                    source="duckduckgo",
                ),
            ),
        )
        self.assertTrue(bundle.used_ukvi)
        self.assertTrue(bundle.used_web_search)
        self.assertTrue(bundle.high_confidence)
        self.assertGreaterEqual(len(bundle.hits), 2)


if __name__ == "__main__":
    unittest.main()
