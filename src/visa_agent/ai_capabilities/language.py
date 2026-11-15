"""Detect customer language for monolingual agent replies."""

from __future__ import annotations

import re

_CJK = re.compile(r"[\u4e00-\u9fff]")


def detect_reply_language(*texts: str) -> str:
    """Return ``zh`` or ``en`` from customer-facing text (body / subject / filename).

    Heuristic: enough CJK → Chinese; otherwise English. Mixed text with any
    substantial Chinese defaults to Chinese so CN customers get CN replies.
    """
    blob = "\n".join(t or "" for t in texts).strip()
    if not blob:
        return "en"
    cjk = len(_CJK.findall(blob))
    if cjk >= 2:
        return "zh"
    # Single Chinese char in short body still counts (e.g. "见附件").
    if cjk >= 1 and len(blob) <= 40:
        return "zh"
    return "en"
