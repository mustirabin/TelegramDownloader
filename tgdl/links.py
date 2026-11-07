"""Find Telegram message links (t.me/...) in message text."""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlparse

LINK_RE = re.compile(r"(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me|telegram\.dog)/[^\s<>\"')\]]+", re.I)


@dataclass(frozen=True)
class MessageLink:
    url: str
    chat: int | str  # channel id (positive, as in PeerChannel) or public username
    msg_id: int

    @property
    def is_private_channel(self) -> bool:
        return isinstance(self.chat, int)


def parse_link(url: str) -> MessageLink | None:
    """Parse t.me/c/<id>/<msg>, t.me/c/<id>/<topic>/<msg>, t.me/<user>/<msg>, t.me/<user>/<topic>/<msg>."""
    u = url.strip()
    if not re.match(r"https?://", u, re.I):
        u = "https://" + u
    p = urlparse(u)
    host = (p.hostname or "").lower().removeprefix("www.")
    if host not in ("t.me", "telegram.me", "telegram.dog"):
        return None
    parts = [x for x in p.path.split("/") if x]
    if parts and parts[0] == "s":  # t.me/s/<channel>/<id> web preview
        parts = parts[1:]
    if len(parts) < 2:
        return None
    if parts[0] == "c":
        nums = parts[1:]
        if len(nums) not in (2, 3) or not all(n.isdigit() for n in nums):
            return None
        return MessageLink(url=url, chat=int(nums[0]), msg_id=int(nums[-1]))
    user, rest = parts[0], parts[1:]
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{3,}", user) or user.lower() in ("joinchat", "addstickers", "share", "proxy"):
        return None
    if len(rest) not in (1, 2) or not all(n.isdigit() for n in rest):
        return None
    return MessageLink(url=url, chat=user, msg_id=int(rest[-1]))


def find_links(text: str, extra_urls: list[str] | None = None) -> list[MessageLink]:
    seen: set[tuple] = set()
    out: list[MessageLink] = []
    for raw in [m.group(0) for m in LINK_RE.finditer(text or "")] + list(extra_urls or []):
        raw = raw.rstrip(".,;:!?")
        link = parse_link(raw)
        if link and (link.chat, link.msg_id) not in seen:
            seen.add((link.chat, link.msg_id))
            out.append(link)
    return out
