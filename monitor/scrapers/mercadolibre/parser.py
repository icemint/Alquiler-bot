"""Parser for MercadoLibre Inmuebles search result pages.

MercadoLibre renders its search results server-side as "poly-card" blocks, so
no JavaScript execution is required. Each card exposes the property type and
operation in a headline ("Departamento en alquiler"), the price with an
``aria-label`` that states the currency in words, an attribute list with
ambientes/baths/area, and a structured location line.
"""

from __future__ import annotations

import logging
import re
from typing import List, Optional
from urllib.parse import urlsplit, urlunsplit

from bs4 import BeautifulSoup

from monitor.models.listing import Listing, Operation, PropertyType
from monitor.neighborhoods import NeighborhoodResolver
from monitor.scrapers.parsing import (
    normalize_spaces,
    parse_area,
    parse_number,
    parse_operation,
    parse_price,
    parse_property_type,
    parse_rooms,
)

logger = logging.getLogger(__name__)

SOURCE = "mercadolibre"
ITEM_ID_RE = re.compile(r"(ML[A-Z])-?(\d{6,})")
_CURRENCY_WORDS = {
    "pesos argentinos": "ARS",
    "peso argentino": "ARS",
    "dolares": "USD",
    "dólares": "USD",
    "dolar": "USD",
    "dólar": "USD",
}


def extract_item_id(url: str) -> Optional[str]:
    match = ITEM_ID_RE.search(url or "")
    if not match:
        return None
    return f"{match.group(1)}{match.group(2)}"


def clean_url(url: str) -> str:
    """Drop MercadoLibre's per-session tracking query and fragment."""
    parts = urlsplit((url or "").strip())
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def _price_from_card(card) -> tuple:
    """Return (amount, currency, approximate) for a poly-card."""
    amount_node = card.select_one(".poly-price__current .andes-money-amount, .andes-money-amount")
    if amount_node is None:
        return None, None, False

    label = amount_node.get("aria-label") or ""
    lowered = label.lower()
    currency = None
    for word, code in _CURRENCY_WORDS.items():
        if word in lowered:
            currency = code
            break
    amount = parse_number(label) if label else None

    if amount is None or currency is None:
        symbol = amount_node.select_one(".andes-money-amount__currency-symbol")
        fraction = amount_node.select_one(".andes-money-amount__fraction")
        text = "{0} {1}".format(
            normalize_spaces(symbol.get_text()) if symbol else "",
            normalize_spaces(fraction.get_text()) if fraction else "",
        )
        parsed_amount, parsed_currency, approximate = parse_price(text)
        amount = amount if amount is not None else parsed_amount
        currency = currency or parsed_currency
        if approximate:
            return amount, currency, True

    previous = card.select_one(".poly-price__previous, .poly-component__price-from")
    text_blob = normalize_spaces(card.get_text(" ")) or ""
    approximate = bool(re.search(r"\bdesde\b|a partir de", text_blob, re.I))
    if previous is not None and re.search(r"\bdesde\b", previous.get_text(), re.I):
        approximate = True
    return amount, currency, approximate


def parse_search_page(
    html: str,
    resolver: NeighborhoodResolver,
    requested_neighborhood: Optional[str] = None,
) -> List[Listing]:
    """Turn one search results page into normalized listings.

    ``requested_neighborhood`` is only recorded for troubleshooting: the
    neighborhood is always taken from the card's own location field, never
    from the search we happened to run.
    """
    soup = BeautifulSoup(html, "html.parser")
    listings: List[Listing] = []

    cards = soup.select("li.ui-search-layout__item")
    if not cards:
        cards = soup.select("div.poly-card")

    for card in cards:
        link = card.select_one("a.poly-component__title") or card.select_one("a.ui-search-link")
        if link is None or not link.get("href"):
            continue
        url = clean_url(link.get("href"))
        item_id = extract_item_id(url)
        title = normalize_spaces(link.get_text())

        headline_node = card.select_one(".poly-component__headline")
        headline = normalize_spaces(headline_node.get_text()) if headline_node else None
        property_type = parse_property_type(headline) if headline else PropertyType.OTHER
        operation = parse_operation(headline) if headline else Operation.OTHER
        if property_type is PropertyType.OTHER and title:
            property_type = parse_property_type(title)
        if operation is Operation.OTHER and title:
            operation = parse_operation(title)

        amount, currency, approximate = _price_from_card(card)

        area: Optional[float] = None
        rooms: Optional[int] = None
        for attribute in card.select(".poly-attributes_list__item"):
            text = normalize_spaces(attribute.get_text()) or ""
            lowered = text.lower()
            if "m²" in lowered or "m2" in lowered:
                # Prefer total area; only fall back to covered area.
                parsed = parse_area(text)
                if parsed is not None and ("total" in lowered or area is None):
                    area = parsed
            elif "amb" in lowered:
                rooms = parse_rooms(text) or rooms

        location_node = card.select_one(".poly-component__location")
        location = normalize_spaces(location_node.get_text()) if location_node else None
        parts = [part.strip() for part in (location or "").split(",") if part.strip()]
        neighborhood = resolver.from_location_path(SOURCE, parts)
        address = parts[0] if len(parts) > 1 else None

        listings.append(
            Listing(
                source=SOURCE,
                external_id=item_id,
                url=url,
                title=title,
                property_type=property_type,
                operation=operation,
                neighborhood=neighborhood,
                raw_neighborhood=location,
                address=address,
                price=amount,
                currency=currency,
                area_m2=area,
                rooms=rooms,
                expensas=None,
                published_at=None,
                price_is_approximate=approximate,
                extra={"requested_neighborhood": requested_neighborhood}
                if requested_neighborhood
                else {},
            )
        )
    return listings
