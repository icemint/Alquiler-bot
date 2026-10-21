"""Optional adapter for MercadoLibre's official API.

NOT VERIFIED AGAINST THE LIVE API: every endpoint of api.mercadolibre.com now
answers 403 without OAuth credentials, and this project has none. The mapping
below follows MercadoLibre's documented /sites/MLA/search response shape and
is covered by unit tests built from that documented shape, not from a live
capture. Treat it as a starting point: enable it with MERCADOLIBRE_MODE=api
and MELI_ACCESS_TOKEN, and check the first cycle's output before relying on it.

The HTML scraper remains the default precisely because it is the one that was
tested end to end.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, List, Optional

from monitor.models.listing import Listing, Operation, PropertyType
from monitor.neighborhoods import NeighborhoodResolver
from monitor.scrapers.base import FetchError
from monitor.scrapers.parsing import parse_operation, parse_property_type

logger = logging.getLogger(__name__)

SOURCE = "mercadolibre"
API_SEARCH_URL = "https://api.mercadolibre.com/sites/MLA/search"


def _attribute(item: Dict[str, Any], attribute_id: str) -> Optional[str]:
    for attribute in item.get("attributes") or []:
        if attribute.get("id") == attribute_id:
            value = attribute.get("value_name") or attribute.get("value_struct", {}).get("number")
            return str(value) if value is not None else None
    return None


def _number(value: Optional[str]) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(str(value).split()[0].replace(",", "."))
    except (ValueError, IndexError):
        return None


def item_to_listing(item: Dict[str, Any], resolver: NeighborhoodResolver) -> Listing:
    """Map one /sites/MLA/search result onto the normalized model."""
    location = item.get("location") or {}
    neighborhood_name = (location.get("neighborhood") or {}).get("name")
    city_name = (location.get("city") or {}).get("name")
    location = resolver.from_location_path(SOURCE, [city_name, neighborhood_name])
    address_line = (item.get("seller_address") or {}).get("address_line") or None

    total_area = _number(_attribute(item, "TOTAL_AREA"))
    covered_area = _number(_attribute(item, "COVERED_AREA"))
    rooms = _number(_attribute(item, "ROOMS"))
    property_type_raw = _attribute(item, "PROPERTY_TYPE")
    operation_raw = _attribute(item, "OPERATION")

    return Listing(
        source=SOURCE,
        external_id=item.get("id"),
        url=item.get("permalink") or "",
        title=item.get("title"),
        property_type=parse_property_type(property_type_raw)
        if property_type_raw
        else PropertyType.OTHER,
        operation=parse_operation(operation_raw) if operation_raw else Operation.OTHER,
        city=location.city if location else None,
        neighborhood=location.neighborhood if location else None,
        raw_neighborhood=neighborhood_name,
        address=address_line,
        price=item.get("price"),
        currency=item.get("currency_id"),
        area_m2=total_area if total_area is not None else covered_area,
        rooms=int(rooms) if rooms else None,
        expensas=_number(_attribute(item, "MAINTENANCE_FEE")),
        published_at=None,
        price_is_approximate=False,
    )


class MercadoLibreApiFetcher:
    def __init__(self, scraper) -> None:
        self.scraper = scraper
        self.token = os.getenv("MELI_ACCESS_TOKEN")

    def collect(self) -> List[Listing]:
        if not self.token:
            raise FetchError(
                "MERCADOLIBRE_MODE=api requires MELI_ACCESS_TOKEN "
                "(create an app at developers.mercadolibre.com)"
            )
        criteria = self.scraper.criteria
        params = {
            "category": "MLA1473",  # Inmuebles
            "operation": "rent",
            "limit": "50",
            "price": f"0-{criteria.max_rent:.0f}",
        }
        query = "&".join(f"{k}={v}" for k, v in params.items())
        url = f"{API_SEARCH_URL}?{query}"
        raw = self.scraper.http.get(
            url,
            delay=self.scraper.source_config.request_delay_seconds,
            headers={"Authorization": f"Bearer {self.token}"},
        )
        try:
            payload = json.loads(raw)
        except ValueError as exc:
            raise FetchError(f"invalid JSON from MercadoLibre API: {exc}") from exc
        return [item_to_listing(item, self.scraper.resolver) for item in payload.get("results", [])]
