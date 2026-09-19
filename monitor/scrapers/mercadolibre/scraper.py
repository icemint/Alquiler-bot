"""MercadoLibre Inmuebles scraper.

Access strategy (see README): MercadoLibre serves fully rendered search pages
to ordinary HTTP clients, and its URL grammar supports exactly the filters we
need - operation, property type, neighborhood, ARS price range and minimum
total area - which keeps the request volume tiny:

    /{tipo}/alquiler/capital-federal/{barrio}/_PriceRange_0ARS-1000000ARS_TOTAL*AREA_50-*

One request per (neighborhood, property type) pair, and a second page only
when the first one came back full.

``MERCADOLIBRE_MODE=api`` switches to the official API adapter, which needs
your own application credentials (see mercadolibre/api.py).
"""

from __future__ import annotations

import logging
from typing import List, Optional

from monitor.models.listing import Listing, PropertyType
from monitor.scrapers.base import BaseScraper
from monitor.scrapers.mercadolibre.parser import SOURCE, parse_search_page

logger = logging.getLogger(__name__)

BASE_URL = "https://inmuebles.mercadolibre.com.ar"
RESULTS_PER_PAGE = 48

TYPE_SLUGS = {
    PropertyType.DEPARTAMENTO: "departamentos",
    PropertyType.CASA: "casas",
    PropertyType.PH: "ph",
}


class MercadoLibreScraper(BaseScraper):
    name = SOURCE

    def build_url(
        self,
        property_type: PropertyType,
        neighborhood_slug: str,
        offset: int = 0,
    ) -> str:
        criteria = self.config.criteria
        segments = []
        if offset:
            segments.append(f"_Desde_{offset + 1}")
        segments.append(
            f"_PriceRange_0{criteria.currency}-{int(criteria.max_rent):d}{criteria.currency}"
        )
        if criteria.min_area_m2:
            # Total area is always >= covered area, so filtering on it keeps
            # the result set a superset of what the area filter will accept.
            segments.append(f"_TOTAL*AREA_{int(criteria.min_area_m2):d}-*")
        return "{base}/{type_slug}/alquiler/capital-federal/{hood}/{filters}".format(
            base=BASE_URL,
            type_slug=TYPE_SLUGS[property_type],
            hood=neighborhood_slug,
            filters="".join(segments),
        )

    def _slug_for(self, canonical_neighborhood: str) -> Optional[str]:
        config = self.resolver.mapping.get(canonical_neighborhood, {})
        slugs = (config.get("slugs") or {}).get(SOURCE) or []  # type: ignore[union-attr]
        return slugs[0] if slugs else None

    def collect(self) -> List[Listing]:
        mode = str(self.source_config.options.get("mode", "html")).lower()
        if mode == "api":
            from monitor.scrapers.mercadolibre.api import MercadoLibreApiFetcher

            return MercadoLibreApiFetcher(self).collect()

        criteria = self.config.criteria
        listings: List[Listing] = []
        for neighborhood in criteria.neighborhoods:
            slug = self._slug_for(neighborhood)
            if not slug:
                self.logger.warning(
                    "no %s slug configured for neighborhood %s", SOURCE, neighborhood
                )
                continue
            for property_type in criteria.property_types:
                if property_type not in TYPE_SLUGS:
                    continue
                listings.extend(self._collect_search(property_type, slug, neighborhood))
        return listings

    def _collect_search(
        self, property_type: PropertyType, slug: str, neighborhood: str
    ) -> List[Listing]:
        collected: List[Listing] = []
        for page in range(self.source_config.max_pages):
            url = self.build_url(property_type, slug, offset=page * RESULTS_PER_PAGE)
            html = self.fetch(url)
            page_listings = parse_search_page(html, self.resolver, requested_neighborhood=slug)
            collected.extend(page_listings)
            self.logger.debug(
                "%s %s page %s: %s listings",
                slug,
                property_type.value,
                page + 1,
                len(page_listings),
            )
            # Only ask for another page when this one was completely full.
            if len(page_listings) < RESULTS_PER_PAGE:
                break
        return collected
