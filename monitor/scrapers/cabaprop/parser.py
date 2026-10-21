"""CabaProp API response parser.

CabaProp's search page is filled from a JSON endpoint, so there is no HTML to
parse: every field we need is structured. Codes observed live on 2026-10-21:

* ``price.currency`` / ``price.expensesCurrency``: 1 = USD, 2 = ARS
  (0 or absent = not given);
* ``property_type``: 1 = Departamento, 2 = Casa, 3 = PH;
* ``operation_type``: 1 = venta, 2 = alquiler, 3 = temporario;
* ``location.barrios``: CabaProp barrio ids, most specific last;
* ``publication.attributes``: the MercadoLibre attributes of the same ad, when
  the broker also published it there (``FURNISHED``: "Sí"/"No").
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from monitor.models.listing import Listing, Operation, PropertyType
from monitor.neighborhoods import NeighborhoodResolver
from monitor.scrapers.base import FetchError
from monitor.scrapers.parsing import bedrooms_from_rooms, parse_furnished
from monitor.timeutil import from_iso

logger = logging.getLogger(__name__)

SOURCE = "cabaprop"
CITY = "CABA"  # CabaProp lists nothing else
BASE_URL = "https://cabaprop.com.ar"

CURRENCIES = {1: "USD", 2: "ARS"}
PROPERTY_TYPES = {1: PropertyType.DEPARTAMENTO, 2: PropertyType.CASA, 3: PropertyType.PH}
OPERATIONS = {1: Operation.VENTA, 2: Operation.ALQUILER, 3: Operation.ALQUILER_TEMPORAL}


def _positive(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _meli_attribute(item: Dict[str, Any], attribute_id: str) -> Optional[str]:
    for attribute in (item.get("publication") or {}).get("attributes") or []:
        if attribute.get("id") == attribute_id:
            return attribute.get("value_name")
    return None


def _bedrooms(item: Dict[str, Any], rooms: Optional[int]) -> Optional[int]:
    # 0 is a real answer here (a monoambiente), unlike for prices and areas.
    for value in (
        (item.get("characteristics") or {}).get("bedrooms"),
        _meli_attribute(item, "BEDROOMS"),
    ):
        try:
            number = int(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue
        if number >= 0:
            return number
    return bedrooms_from_rooms(rooms)


def _furnished(item: Dict[str, Any]) -> Optional[bool]:
    stated = (_meli_attribute(item, "FURNISHED") or "").strip().lower()
    if stated in ("sí", "si"):
        return True
    if stated == "no":
        return False
    return parse_furnished(item.get("title"), item.get("description"))


def _created_at(value: Any):
    # Python 3.9's fromisoformat does not accept the trailing "Z".
    if isinstance(value, str) and value.endswith("Z"):
        value = value[:-1] + "+00:00"
    return from_iso(value)


def item_to_listing(item: Dict[str, Any], resolver: NeighborhoodResolver) -> Listing:
    location = item.get("location") or {}
    price = item.get("price") or {}
    surface = item.get("surface") or {}
    characteristics = item.get("characteristics") or {}

    barrio_ids = [str(b) for b in location.get("barrios") or []]
    found = resolver.from_ids(SOURCE, reversed(barrio_ids))

    street = " ".join(
        str(part) for part in (location.get("street"), location.get("number")) if part
    ).strip()

    expensas = _positive(price.get("expenses"))
    area = _positive(surface.get("totalSurface")) or _positive(surface.get("coveredSurface"))
    ambience = characteristics.get("ambience")
    rooms = int(ambience) if isinstance(ambience, (int, float)) and ambience > 0 else None

    return Listing(
        source=SOURCE,
        external_id=item.get("_id"),
        url=item.get("permalink") or f"{BASE_URL}/propiedad/{item.get('_id')}",
        title=item.get("title"),
        property_type=PROPERTY_TYPES.get(item.get("property_type"), PropertyType.OTHER),
        operation=OPERATIONS.get(item.get("operation_type"), Operation.OTHER),
        city=CITY,
        neighborhood=found.neighborhood if found else None,
        raw_neighborhood=",".join(barrio_ids) or None,
        address=street or None,
        price=_positive(price.get("total")),
        # An unknown currency code stays None: the filter then refuses the
        # listing instead of comparing a USD amount against an ARS limit.
        currency=CURRENCIES.get(price.get("currency")),
        area_m2=area,
        rooms=rooms,
        bedrooms=_bedrooms(item, rooms),
        furnished=_furnished(item),
        expensas=expensas,
        expensas_currency=CURRENCIES.get(price.get("expensesCurrency")) if expensas else None,
        published_at=_created_at(item.get("created_at")),
        price_is_approximate=False,
        extra={"barrios": barrio_ids},
    )


def parse_search_response(raw: str, resolver: NeighborhoodResolver) -> List[Listing]:
    try:
        payload = json.loads(raw)
    except ValueError as exc:
        raise FetchError(f"invalid JSON from CabaProp: {exc}") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("result"), list):
        # The endpoint is undocumented; a changed shape must surface as an
        # error, never as "0 listings found".
        raise FetchError("unexpected CabaProp response shape")
    listings: List[Listing] = []
    for item in payload["result"]:
        try:
            listings.append(item_to_listing(item, resolver))
        except Exception:  # noqa: BLE001 - one odd item must not drop the page
            logger.warning("skipping unparseable CabaProp item %s", item.get("_id"))
    return listings
