"""Several links in one message: one download per link.

A message with two or more links (one per line, or several on a line) is
split into its links; each is started on its own and waits for a download
slot like any other job. Lines without a link are reported back, not fatal.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_LINK = re.compile(r"magnet:\?\S+|https?://\S+", re.IGNORECASE)
_TRAILING = ").,]}>\"'"


@dataclass
class LinkBatch:
    links: list[str] = field(default_factory=list)
    # Non-empty lines that held no link, as typed (shortened by the caller).
    invalid: list[str] = field(default_factory=list)
    duplicates: int = 0

    @property
    def is_batch(self) -> bool:
        return len(self.links) >= 2


def split_links(text: str) -> LinkBatch:
    """Every magnet and http(s) link in ``text``, in order, without repeats."""
    batch = LinkBatch()
    seen: set[str] = set()
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        found = [m.group(0).rstrip(_TRAILING) for m in _LINK.finditer(line)]
        found = [link for link in found if len(link) > len("https://")]
        if not found:
            batch.invalid.append(line)
            continue
        for link in found:
            if link in seen:
                batch.duplicates += 1
                continue
            seen.add(link)
            batch.links.append(link)
    return batch
