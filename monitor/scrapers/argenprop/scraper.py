"""ArgenProp scraper.

Access strategy (see README): ArgenProp serves plain server-rendered HTML with
no JavaScript challenge, but its AWS WAF starts answering ``202`` with an
empty body once it dislikes the request rate, and that block outlives short
backoffs. The scraper is therefore deliberately frugal:

* both neighborhoods in one URL - ``/departamentos/alquiler/barrio-norte-o-recoleta``
  (robots.txt forbids three-way ``-o-`` combinations, two are allowed);
* newest-first ordering via ``?orden-masnuevos`` so page 1 holds what is new;
* one page per property type by default, with a long delay between requests
  and its own polling interval (ARGENPROP_MIN_INTERVAL_SECONDS).
"""

from __future__ import annotations

from typing import List, Optional

from monitor.models.listing import Listing, PropertyType
from monitor.scrapers.argenprop.parser import BASE_URL, SOURCE, parse_search_page
from monitor.scrapers.base import BaseScraper

TYPE_SLUGS = {
    PropertyType.DEPARTAMENTO: "departamentos",
    PropertyType.CASA: "casas",
    PropertyType.PH: "ph",
}
ORDER_NEWEST = "orden-masnuevos"


class ArgenPropScraper(BaseScraper):
    name = SOURCE

    def _slug_for(self, canonical_neighborhood: str) -> Optional[str]:
        config = self.resolver.mapping.get(canonical_neighborhood, {})
        slugs = (config.get("slugs") or {}).get(SOURCE) or []  # type: ignore[union-attr]
        return slugs[0] if slugs else None

    def neighborhood_segment(self) -> Optional[str]:
        """Combine the configured neighborhoods into one URL segment.

        ArgenProp's robots.txt disallows paths with more than two values
        combined by ``-o-``, so with three or more neighborhoods we fall back
        to one request per neighborhood.
        """
        slugs = [self._slug_for(name) for name in self.config.criteria.neighborhoods]
        slugs = [slug for slug in slugs if slug]
        if not slugs:
            return None
        if len(slugs) > 2:
            return None
        return "-o-".join(sorted(slugs))

    def build_url(
        self, property_type: PropertyType, neighborhood_segment: str, page: int = 1
    ) -> str:
        url = (
            f"{BASE_URL}/{TYPE_SLUGS[property_type]}/alquiler/{neighborhood_segment}?{ORDER_NEWEST}"
        )
        if page > 1:
            url = f"{url}&pagina-{page}"
        return url

    def collect(self) -> List[Listing]:
        segments: List[str] = []
        combined = self.neighborhood_segment()
        if combined:
            segments.append(combined)
        else:
            for name in self.config.criteria.neighborhoods:
                slug = self._slug_for(name)
                if slug:
                    segments.append(slug)

        listings: List[Listing] = []
        for segment in segments:
            for property_type in self.config.criteria.property_types:
                if property_type not in TYPE_SLUGS:
                    continue
                for page in range(1, self.source_config.max_pages + 1):
                    html = self.fetch(self.build_url(property_type, segment, page))
                    page_listings = parse_search_page(
                        html, self.resolver, requested_neighborhood=segment
                    )
                    listings.extend(page_listings)
                    self.logger.debug(
                        "%s %s page %s: %s listings",
                        segment,
                        property_type.value,
                        page,
                        len(page_listings),
                    )
                    if len(page_listings) < 20:
                        break
        return listings
