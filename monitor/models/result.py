"""Per-source outcome of a monitoring cycle."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional

from monitor.models.listing import Listing


class SourceStatus(str, Enum):
    OK = "ok"
    BLOCKED = "blocked"
    ERROR = "error"
    DISABLED = "disabled"
    SKIPPED = "skipped"


@dataclass
class ScrapeResult:
    """What a scraper returns. Never raises through to the scheduler."""

    source: str
    status: SourceStatus = SourceStatus.OK
    listings: List[Listing] = field(default_factory=list)
    error: Optional[str] = None
    requests_made: int = 0
    # Set by a scraper that could not read the site at all. Distinguishes
    # "the site returned nothing" from "nothing matched", which matters:
    # a blocked source must never be reported as an empty result.
    partial: bool = False

    @property
    def found(self) -> int:
        return len(self.listings)

    @property
    def ok(self) -> bool:
        return self.status is SourceStatus.OK
