"""Bounded RFC Message-ID parsing for mail thread attribution and replies."""

from __future__ import annotations

import re

_MESSAGE_ID = re.compile(r"<[!#$%&'*+/=?^_`{|}~.0-9A-Za-z-]{1,250}@[.0-9A-Za-z-]{1,250}>")
_MAX_ANCESTORS = 32


def message_ids(value: str | None) -> tuple[str, ...]:
    if not value:
        return ()
    matches = _MESSAGE_ID.findall(value[-16384:])
    return tuple(dict.fromkeys(matches[-_MAX_ANCESTORS:]))


def single_message_id(value: str | None) -> str | None:
    stripped = (value or "").strip()
    return stripped if _MESSAGE_ID.fullmatch(stripped) else None


def case_message_id(outbox_id: str, from_addr: str) -> str:
    domain = from_addr.rsplit("@", 1)[-1].strip().removesuffix(">")
    return single_message_id(f"<{outbox_id}@{domain}>") or f"<{outbox_id}@visa-agent.invalid>"


def reply_references(
    *, parent_id: str, references: str | None, parent_in_reply_to: str | None
) -> tuple[str, ...]:
    ancestors = message_ids(references)
    earlier = single_message_id(parent_in_reply_to)
    if earlier and earlier not in ancestors:
        ancestors = (*ancestors, earlier)
    chain = tuple(item for item in ancestors if item != parent_id) + (parent_id,)
    return chain[-_MAX_ANCESTORS:]


_QUOTED_REPLY = re.compile(
    r"^[ \t]*(?:>[ \t]*)*(?:-+[ \t]*(?:original message|forwarded message|原始邮件|转发邮件)[ \t]*-+"
    r"|begin forwarded message[:：]"
    r"|on[ \t]+[^\n]+(?:\n[^\n]*@[^\n]+)?[ \t]+wrote[:：]"
    r"|(?:在|于)[^\n]+(?:写道|的来信)[:：]"
    r"|_{5,}[ \t]*\n[ \t]*(?:from|发件人)[:：])[ \t]*",
    re.I | re.M,
)
def new_reply_body(body: str) -> str:
    text = body.replace("\r\n", "\n")
    separator = _QUOTED_REPLY.search(text)
    if separator:
        text = text[:separator.start()]
    return "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith(">")
    ).strip()
