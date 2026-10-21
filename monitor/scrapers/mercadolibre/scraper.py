"""MercadoLibre Inmuebles scraper.

Access strategy (see README): MercadoLibre serves fully rendered search pages
to ordinary HTTP clients, and its URL grammar supports exactly the filters we
need - operation, property type, location, ARS price range and minimum total
area - which keeps the request volume tiny:

    /{tipo}/alquiler/{location path}/_PriceRange_0ARS-1000000ARS_TOTAL*AREA_50-*

The location path comes from monitor/data/locations.json, e.g.
``capital-federal/recoleta`` or ``bsas-gba-norte/tigre``. A barrio MercadoLibre
splits into sub-zones (Palermo: Palermo Soho, Palermo Hollywood, ...) has one
path per sub-zone.

* Chosen barrios: one request per (path, property type), and another page
  only when the previous one came back full.
* A whole city: MercadoLibre cannot sort by date, so the city-wide search is
  narrowed to "Publicados hoy" and paged up to ``city_max_pages``. On a busy
  day in CABA that cap can be reached; choosing barrios avoids it.

``MERCADOLIBRE_MODE=api`` switches to the official API adapter, which needs
your own application credentials (see mercadolibre/api.py).
"""

from __future__ import annotations

import logging
from typing import List, Tuple

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
        location_path: str,
        offset: int = 0,
        published_today: bool = False,
    ) -> str:
        criteria = self.criteria
        segments = []
        if offset:
            segments.append(f"_Desde_{offset + 1}")
        if published_today:
            segments.append("_PublishedToday_YES")
        segments.append(
            f"_PriceRange_0{criteria.currency}-{int(criteria.max_rent):d}{criteria.currency}"
        )
        if criteria.min_area_m2:
            # Total area is always >= covered area, so filtering on it keeps
            # the result set a superset of what the area filter will accept.
            segments.append(f"_TOTAL*AREA_{int(criteria.min_area_m2):d}-*")
        return "{base}/{type_slug}/alquiler/{path}/{filters}".format(
            base=BASE_URL,
            type_slug=TYPE_SLUGS[property_type],
            path=location_path.strip("/"),
            filters="".join(segments),
        )

    def searches(self) -> List[Tuple[str, bool]]:
        """(location path, whole city?) for every place in the search."""
        searches: List[Tuple[str, bool]] = []
        for city in self.criteria.cities:
            barrios = self.criteria.barrios_in(city)
            if barrios:
                for barrio in barrios:
                    paths = self.resolver.slugs(SOURCE, city, barrio)
                    if not paths:
                        self.logger.warning("no %s path for %s/%s", SOURCE, city, barrio)
                    searches.extend((path, False) for path in paths)
            else:
                paths = self.resolver.slugs(SOURCE, city)
                if not paths:
                    self.logger.warning("no %s path for %s", SOURCE, city)
                searches.extend((path, True) for path in paths)
        unique: List[Tuple[str, bool]] = []
        for search in searches:
            if search not in unique:
                unique.append(search)
        return unique

    def collect(self) -> List[Listing]:
        mode = str(self.source_config.options.get("mode", "html")).lower()
        if mode == "api":
            from monitor.scrapers.mercadolibre.api import MercadoLibreApiFetcher

            return MercadoLibreApiFetcher(self).collect()

        listings: List[Listing] = []
        for path, whole_city in self.searches():
            for property_type in self.criteria.property_types:
                if property_type not in TYPE_SLUGS:
                    continue
                listings.extend(self._collect_search(property_type, path, whole_city))
        return listings

    def _collect_search(
        self, property_type: PropertyType, path: str, whole_city: bool
    ) -> List[Listing]:
        max_pages = (
            int(self.source_config.options.get("city_max_pages", 5))
            if whole_city
            else self.source_config.max_pages
        )
        collected: List[Listing] = []
        for page in range(max_pages):
            url = self.build_url(
                property_type, path, offset=page * RESULTS_PER_PAGE, published_today=whole_city
            )
            html = self.fetch(url)
            page_listings = parse_search_page(html, self.resolver, requested_neighborhood=path)
            collected.extend(page_listings)
            self.logger.debug(
                "%s %s page %s: %s listings",
                path,
                property_type.value,
                page + 1,
                len(page_listings),
            )
            # Only ask for another page when this one was completely full.
            if len(page_listings) < RESULTS_PER_PAGE:
                break
        else:
            if whole_city:
                self.logger.warning(
                    "%s %s: stopped after %s pages of today's listings",
                    path,
                    property_type.value,
                    max_pages,
                )
        return collected
