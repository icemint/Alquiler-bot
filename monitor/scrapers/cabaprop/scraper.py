"""CabaProp scraper.

Access strategy (see README): CabaProp is the CABA real-estate brokers'
board portal (CUCICBA). Its search page is a client-rendered app that POSTs
the filters to ``/api/v1/properties/find-properties`` and renders the JSON it
gets back; robots.txt allows everything but account pages, and there is no
anti-bot challenge. We send the same request the page sends:

* operation, property types, barrio ids, price ceiling and minimum total area
  in the body, so the site does the filtering;
* newest first (``orderBy=created_at&sort=desc``), so one request per cycle
  holds everything new.

The endpoint is undocumented. A change in its shape is reported as a source
error rather than as "0 listings found".
"""

from __future__ import annotations

from typing import Any, Dict, List

from monitor.models.listing import Listing, PropertyType
from monitor.scrapers.base import BaseScraper
from monitor.scrapers.cabaprop.parser import BASE_URL, CITY, SOURCE, parse_search_response

SEARCH_URL = f"{BASE_URL}/api/v1/properties/find-properties"
OPERATION_RENT = 2
TYPE_CODES = {
    PropertyType.DEPARTAMENTO: 1,
    PropertyType.CASA: 2,
    PropertyType.PH: 3,
}
HEADERS = {
    "Accept": "application/json",
    "Origin": BASE_URL,
    "Referer": f"{BASE_URL}/propiedades/alquilar",
}


class CabaPropScraper(BaseScraper):
    name = SOURCE

    @property
    def searches_caba(self) -> bool:
        return CITY in self.criteria.cities

    def barrio_ids(self) -> List[int]:
        """CabaProp ids of the chosen CABA barrios; empty for all of CABA."""
        ids: List[int] = []
        for barrio in self.criteria.barrios_in(CITY):
            source_ids = self.resolver.ids(SOURCE, CITY, barrio)
            if not source_ids:
                self.logger.warning("no %s ids for %s/%s", SOURCE, CITY, barrio)
            ids.extend(int(identifier) for identifier in source_ids)
        return ids

    def build_query(self) -> Dict[str, Any]:
        criteria = self.criteria
        query: Dict[str, Any] = {
            "operationType": OPERATION_RENT,
            "propertyTypes": [TYPE_CODES[t] for t in criteria.property_types if t in TYPE_CODES],
            "barrios": self.barrio_ids(),
            # The site ignores the currency when comparing; the filter stage
            # still rejects anything not priced in the configured currency.
            "price": {"currency": criteria.currency, "min": 0, "max": int(criteria.max_rent)},
        }
        if criteria.min_area_m2:
            query["surface"] = {"type": "totalSurface", "min": int(criteria.min_area_m2), "max": ""}
        return query

    def build_url(self, page: int = 0) -> str:
        limit = int(self.source_config.options.get("page_size", 30))
        return f"{SEARCH_URL}?offset={page}&limit={limit}&orderBy=created_at&sort=desc"

    def collect(self) -> List[Listing]:
        if not self.searches_caba:
            return []  # CabaProp only lists CABA
        query = self.build_query()
        if self.criteria.barrios_in(CITY) and not query["barrios"]:
            # Barrios were chosen but none is known here: an empty list would
            # search all of CABA instead.
            return []
        listings: List[Listing] = []
        page_size = int(self.source_config.options.get("page_size", 30))
        for page in range(self.source_config.max_pages):
            self._requests_made += 1
            raw = self.http.post_json(
                self.build_url(page),
                query,
                delay=self.source_config.request_delay_seconds,
                headers=HEADERS,
            )
            page_listings = parse_search_response(raw, self.resolver)
            listings.extend(page_listings)
            if len(page_listings) < page_size:
                break
        return listings
