"""ArgenProp scraper.

Access strategy (see README): ArgenProp serves plain server-rendered HTML with
no JavaScript challenge, but its AWS WAF starts answering ``202`` with an
empty body once it dislikes the request rate, and that block outlives short
backoffs. The scraper is therefore deliberately frugal:

* one or two chosen barrios go in one URL -
  ``/departamentos/alquiler/barrio-norte-o-recoleta`` (robots.txt forbids
  three-way ``-o-`` combinations, two are allowed); with more barrios, or
  none (a whole city), the city's own search - ``/capital-federal`` - is
  used and the barrios are picked out of it afterwards; AMBA has no such
  search, so its chosen partidos are searched one by one;
* newest-first ordering via ``?orden-masnuevos`` so page 1 holds what is new;
* one page per property type by default, with a long delay between requests
  and its own polling interval (ARGENPROP_MIN_INTERVAL_SECONDS).
"""

from __future__ import annotations

from typing import List

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

    def segments(self) -> List[str]:
        """The location part of each search URL, one per city searched."""
        segments: List[str] = []
        for city in self.criteria.cities:
            barrios = self.criteria.barrios_in(city)
            slugs = [self.resolver.slugs(SOURCE, city, barrio)[:1] for barrio in barrios]
            if barrios and all(slugs) and len(slugs) <= 2:
                segments.append("-o-".join(sorted(slug[0] for slug in slugs)))
                continue
            city_slugs = self.resolver.slugs(SOURCE, city)
            if not city_slugs:
                # No search for the whole city (AMBA): each chosen barrio
                # that has a search of its own, one request each.
                city_slugs = [slug[0] for slug in slugs if slug]
            if not city_slugs:
                self.logger.warning("no %s search for %s", SOURCE, city)
            segments.extend(slug for slug in city_slugs if slug not in segments)
        return segments

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
        listings: List[Listing] = []
        for segment in self.segments():
            for property_type in self.criteria.property_types:
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
