"""Neighborhood canonicalization.

Every site names CABA neighborhoods slightly differently, and "Barrio Norte"
is not an official CABA barrio at all: it overlaps Recoleta and parts of
Palermo/Retiro, so some portals fold it into Recoleta while others expose it
as its own zone. We therefore never match on the address string; we map the
*structured* location value each site gives us (URL slug, location field or
barrio id) onto a canonical name through this table.

The table can be replaced at runtime with NEIGHBORHOOD_MAP_FILE (JSON with the
same shape) without touching the code.
"""

from __future__ import annotations

import json
import re
import unicodedata
from typing import Dict, List, Optional

# canonical name -> {"aliases": [...], "slugs": {source: [...]}, "ids": {source: [...]}}
#
# "ids" holds a site's own internal location identifier, which is the most
# reliable signal of all: ArgenProp renders sub-barrio names such as
# "Parque Las Heras" on the card while classifying the listing under
# idbarrio=4 (Barrio Norte). Values observed live on 2026-09-18; CabaProp ids
# from its /api/v1/utils/barrios-obj on 2026-10-21 (it splits Barrio Norte
# into four sub-zones: 10 Barrio Norte, 11 La Isla, 12 Parque Las Heras,
# 13 Barrio Norte Recoleta).
DEFAULT_NEIGHBORHOOD_MAP: Dict[str, Dict[str, object]] = {
    "Recoleta": {
        "aliases": ["recoleta", "barrio recoleta", "recoleta caba"],
        "slugs": {
            "mercadolibre": ["recoleta"],
            "argenprop": ["recoleta"],
            "zonaprop": ["recoleta"],
        },
        "ids": {"argenprop": ["12618"], "cabaprop": ["88"]},
    },
    "Barrio Norte": {
        "aliases": ["barrio norte", "b norte", "bo norte", "barrio-norte"],
        "slugs": {
            "mercadolibre": ["barrio-norte"],
            "argenprop": ["barrio-norte"],
            "zonaprop": ["barrio-norte"],
        },
        "ids": {"argenprop": ["4"], "cabaprop": ["10", "11", "12", "13"]},
    },
}


def normalize_text(value: str) -> str:
    """Lowercase, strip accents and collapse separators."""
    decomposed = unicodedata.normalize("NFKD", value)
    without_accents = "".join(c for c in decomposed if not unicodedata.combining(c))
    cleaned = re.sub(r"[^a-z0-9]+", " ", without_accents.lower())
    return cleaned.strip()


class NeighborhoodResolver:
    def __init__(self, mapping: Optional[Dict[str, Dict[str, object]]] = None) -> None:
        self.mapping = mapping if mapping is not None else DEFAULT_NEIGHBORHOOD_MAP
        self._by_alias: Dict[str, str] = {}
        self._by_slug: Dict[str, str] = {}
        self._by_id: Dict[str, str] = {}
        for canonical, cfg in self.mapping.items():
            self._by_alias[normalize_text(canonical)] = canonical
            for alias in cfg.get("aliases", []) or []:
                self._by_alias[normalize_text(str(alias))] = canonical
            slugs = cfg.get("slugs", {}) or {}
            for source, source_slugs in slugs.items():  # type: ignore[union-attr]
                for slug in source_slugs:
                    self._by_slug[f"{source}|{normalize_text(str(slug))}"] = canonical
            ids = cfg.get("ids", {}) or {}
            for source, source_ids in ids.items():  # type: ignore[union-attr]
                for identifier in source_ids:
                    self._by_id[f"{source}|{str(identifier).strip()}"] = canonical

    @classmethod
    def from_file(cls, path: Optional[str]) -> NeighborhoodResolver:
        if not path:
            return cls()
        with open(path, encoding="utf-8") as handle:
            return cls(json.load(handle))

    def canonical_names(self) -> List[str]:
        return list(self.mapping.keys())

    def from_slug(self, source: str, slug: Optional[str]) -> Optional[str]:
        """Resolve the location slug we asked the site for (most reliable)."""
        if not slug:
            return None
        return self._by_slug.get(f"{source}|{normalize_text(slug)}")

    def from_id(self, source: str, identifier: Optional[str]) -> Optional[str]:
        """Resolve the site's internal location id - the strongest signal.

        ArgenProp shows "Parque Las Heras" on a card that it files under
        idbarrio=4 (Barrio Norte); only the id gets that listing right.
        """
        if identifier is None or str(identifier).strip() == "":
            return None
        return self._by_id.get(f"{source}|{str(identifier).strip()}")

    def from_label(self, label: Optional[str]) -> Optional[str]:
        """Resolve a structured location label rendered by the site.

        Only exact alias matches count. A free-text address is deliberately
        NOT accepted here: "Av. Santa Fe 1234, Palermo" must not become
        "Barrio Norte" just because the description mentions the zone.
        """
        if not label:
            return None
        return self._by_alias.get(normalize_text(label))

    def from_location_path(self, source: str, parts: List[str]) -> Optional[str]:
        """Resolve from a site's structured location breadcrumb.

        Checks the most specific part first, e.g. ["Capital Federal",
        "Recoleta"] resolves to Recoleta.
        """
        for part in reversed([p for p in parts if p]):
            resolved = self.from_label(part) or self.from_slug(source, part)
            if resolved:
                return resolved
        return None
