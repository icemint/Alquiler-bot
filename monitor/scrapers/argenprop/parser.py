"""Parser for ArgenProp search result pages.

ArgenProp renders results server-side and, helpfully, repeats the important
values as attributes on the card's anchor tag (``idaviso``, ``idbarrio``,
``montooperacion``, ...). We use those attributes where they are unambiguous
and the rendered text everywhere else, then cross-check the two: when the
structured amount and the printed amount disagree, the price is treated as
unreliable rather than guessed.
"""

from __future__ import annotations

import logging
import re
from typing import List, Optional

from bs4 import BeautifulSoup

from monitor.models.listing import Listing, Operation, PropertyType
from monitor.neighborhoods import NeighborhoodResolver
from monitor.scrapers.parsing import (
    bedrooms_from_rooms,
    normalize_spaces,
    parse_area,
    parse_furnished,
    parse_number,
    parse_operation,
    parse_price,
    parse_property_type,
    parse_rooms,
)

logger = logging.getLogger(__name__)

SOURCE = "argenprop"
BASE_URL = "https://www.argenprop.com"
_ID_FROM_URL_RE = re.compile(r"--(\d+)\s*$")
# Observed live on 2026-09-18; configurable only in the sense that a mismatch
# with the printed symbol makes the price unreliable rather than wrong.
CURRENCY_BY_ID = {"1": "ARS", "2": "USD"}
_TOTAL_AREA_RE = re.compile(r"\btot\b|total", re.I)
_COVERED_AREA_RE = re.compile(r"\bcubie|cubierta", re.I)


def _absolute(href: str) -> str:
    href = (href or "").strip()
    if href.startswith("http"):
        return href
    return f"{BASE_URL}{href}"


def _slug_type_and_operation(href: str):
    """ArgenProp encodes both in the detail URL slug, e.g.
    ``/departamento-en-alquiler-en-recoleta-2-ambientes--20172118``."""
    slug = (href or "").split("/")[-1].replace("-", " ")
    return parse_property_type(slug), parse_operation(slug)


def parse_search_page(
    html: str,
    resolver: NeighborhoodResolver,
    requested_neighborhood: Optional[str] = None,
) -> List[Listing]:
    soup = BeautifulSoup(html, "html.parser")
    listings: List[Listing] = []

    for card in soup.select("a.card[data-item-card]"):
        href = card.get("href")
        if not href:
            continue
        url = _absolute(href)
        external_id = card.get("data-item-card") or card.get("idaviso")
        if not external_id:
            match = _ID_FROM_URL_RE.search(url)
            external_id = match.group(1) if match else None

        property_type, operation = _slug_type_and_operation(href)

        title_node = card.select_one(".card__title")
        title = normalize_spaces(title_node.get_text(" ")) if title_node else None
        if property_type is PropertyType.OTHER:
            property_type = parse_property_type(title)
        if operation is Operation.OTHER:
            operation = parse_operation(title)

        address_node = card.select_one(".card__address")
        address_text = normalize_spaces(address_node.get_text(" ")) if address_node else None
        address_parts = [p.strip() for p in (address_text or "").split(",") if p.strip()]
        # ArgenProp's own ids win: the card may print a sub-barrio ("Parque
        # Las Heras") for a listing filed under Barrio Norte. The rendered
        # barrio label is the fallback, within the city the ids gave.
        location = resolver.from_ids(
            SOURCE,
            [
                f"barrio:{card.get('idbarrio')}" if card.get("idbarrio") else None,
                f"partido:{card.get('idpartido')}" if card.get("idpartido") else None,
                f"localidad:{card.get('idlocalidad')}" if card.get("idlocalidad") else None,
            ],
        )
        label = address_parts[-1] if address_parts else None
        if location is None:
            location = resolver.from_location_path(SOURCE, [label] if label else [])
        elif location.neighborhood is None:
            location = resolver.from_label(label, location.city) or location
        street = ", ".join(address_parts[:-1]) if len(address_parts) > 1 else address_text

        currency_from_id = CURRENCY_BY_ID.get((card.get("idmoneda") or "").strip())
        price_node = card.select_one(".card__price")
        price_text = normalize_spaces(price_node.get_text(" ")) if price_node else None
        price, currency, approximate = parse_price(price_text)

        if currency is None:
            currency = currency_from_id
        elif currency_from_id and currency != currency_from_id:
            # The printed symbol and the structured currency disagree: an ARS
            # limit must never be applied to a USD amount, so refuse to guess.
            logger.debug("currency mismatch on %s: %s vs %s", url, currency, currency_from_id)
            currency = None
            approximate = True

        structured_amount = parse_number(card.get("montooperacion") or "")
        if structured_amount is not None:
            if price is None:
                price = structured_amount
            elif abs(price - structured_amount) > 1:
                # Printed and structured amounts disagree: do not guess.
                logger.debug(
                    "price mismatch on %s: text=%s attribute=%s", url, price, structured_amount
                )
                approximate = True

        expenses_node = card.select_one(".card__expenses")
        expensas_text = normalize_spaces(expenses_node.get_text(" ")) if expenses_node else None
        expensas, expensas_currency, _ = parse_price(expensas_text)

        area: Optional[float] = None
        covered_area: Optional[float] = None
        rooms: Optional[int] = None
        for feature in card.select(".card__main-features li, .card__main-features span"):
            text = normalize_spaces(feature.get_text(" ")) or ""
            if not text:
                continue
            lowered = text.lower()
            if "m²" in lowered or "m2" in lowered:
                value = parse_area(text)
                if value is None:
                    continue
                if _TOTAL_AREA_RE.search(lowered):
                    area = value
                elif _COVERED_AREA_RE.search(lowered) or covered_area is None:
                    covered_area = value
            elif "ambiente" in lowered:
                rooms = parse_rooms(text) or rooms
        if area is None:
            area = covered_area
        if rooms is None:
            rooms = parse_rooms(card.get("ambientes") or "")
        dormitorios = (card.get("dormitorios") or "").strip()
        bedrooms = int(dormitorios) if dormitorios.isdigit() else bedrooms_from_rooms(rooms)
        info_node = card.select_one(".card__info")
        furnished = parse_furnished(
            title, normalize_spaces(info_node.get_text(" ")) if info_node else None
        )

        listings.append(
            Listing(
                source=SOURCE,
                external_id=external_id,
                url=url,
                title=title,
                property_type=property_type,
                operation=operation,
                city=location.city if location else None,
                neighborhood=location.neighborhood if location else None,
                raw_neighborhood=label,
                address=street,
                price=price,
                currency=currency,
                area_m2=area,
                rooms=rooms,
                bedrooms=bedrooms,
                furnished=furnished,
                expensas=expensas,
                expensas_currency=expensas_currency,
                published_at=None,
                price_is_approximate=approximate,
                extra={
                    "idbarrio": card.get("idbarrio"),
                    "idmoneda": card.get("idmoneda"),
                    "requested_neighborhood": requested_neighborhood,
                },
            )
        )
    return listings
