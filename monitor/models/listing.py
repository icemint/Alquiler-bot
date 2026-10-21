"""Normalized listing model shared by every scraper.

Every scraper must emit `Listing` objects so that filtering, deduplication,
storage and notification stay completely independent from the source sites.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, Optional
from urllib.parse import urlsplit, urlunsplit


class PropertyType(str, Enum):
    DEPARTAMENTO = "Departamento"
    CASA = "Casa"
    PH = "PH"
    OTHER = "Otro"


class Operation(str, Enum):
    ALQUILER = "Alquiler"
    ALQUILER_TEMPORAL = "Alquiler temporal"
    VENTA = "Venta"
    OTHER = "Otro"


def canonical_url(url: str) -> str:
    """Strip query string, fragment and trailing slash so the same listing
    reached through different tracking links yields one identity."""
    parts = urlsplit(url.strip())
    path = parts.path.rstrip("/") or "/"
    netloc = parts.netloc.lower()
    if netloc.startswith("www."):
        netloc = netloc[4:]
    return urlunsplit((parts.scheme.lower() or "https", netloc, path, "", ""))


@dataclass
class Listing:
    """A property advertisement normalized to a single internal format.

    Optional fields are genuinely optional: a scraper must leave them as
    ``None`` rather than guessing, because the filtering stage refuses to
    notify about listings whose price or area could not be determined.
    """

    source: str
    external_id: Optional[str]
    url: str
    title: Optional[str] = None
    property_type: PropertyType = PropertyType.OTHER
    operation: Operation = Operation.OTHER
    neighborhood: Optional[str] = None
    raw_neighborhood: Optional[str] = None
    address: Optional[str] = None
    price: Optional[float] = None
    currency: Optional[str] = None
    area_m2: Optional[float] = None
    rooms: Optional[int] = None
    # Dormitorios. A monoambiente has 0; None means the site did not say.
    bedrooms: Optional[int] = None
    # True/False only when the listing states it; None when it is silent.
    furnished: Optional[bool] = None
    expensas: Optional[float] = None
    expensas_currency: Optional[str] = None
    published_at: Optional[datetime] = None
    # True when the site shows a "desde"/"a partir de" price, or when one
    # advertisement covers several units with different prices.
    price_is_approximate: bool = False
    extra: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.url = self.url.strip()
        if self.external_id is not None:
            self.external_id = str(self.external_id).strip() or None
        if self.currency:
            self.currency = self.currency.strip().upper()
        if self.title:
            self.title = re.sub(r"\s+", " ", self.title).strip()

    @property
    def dedupe_key(self) -> str:
        """Stable identity used for deduplication.

        Prefers the site's own listing id; falls back to a hash of the
        canonical URL so that a source without ids still dedupes reliably.
        """
        if self.external_id:
            return f"{self.source}:{self.external_id}"
        digest = hashlib.sha256(canonical_url(self.url).encode("utf-8")).hexdigest()[:20]
        return f"{self.source}:url:{digest}"

    @property
    def stored_external_id(self) -> str:
        """Non-null external id for the database column."""
        if self.external_id:
            return self.external_id
        return self.dedupe_key.split(":", 1)[1]
