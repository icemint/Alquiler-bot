"""What the parsers do with cards and values the live captures do not have."""

import json

import pytest
from conftest import load_fixture

from monitor.models.listing import Operation, PropertyType
from monitor.neighborhoods import Location
from monitor.scrapers import parsing
from monitor.scrapers.argenprop.parser import parse_search_page as parse_argenprop
from monitor.scrapers.base import FetchError
from monitor.scrapers.cabaprop.parser import parse_search_response
from monitor.scrapers.mercadolibre.parser import parse_search_page as parse_mercadolibre

# ---------------------------------------------------------------- argenprop


def argenprop_card(href="/departamento-en-alquiler-en-palermo--123", body="", **attributes):
    # ArgenProp marks every result card with data-item-card (its id, or empty).
    attributes.setdefault("data-item-card", "")
    attrs = " ".join(f'{key}="{value}"' for key, value in attributes.items())
    href_attr = f'href="{href}"' if href is not None else ""
    return f'<a class="card" {href_attr} {attrs}>{body}</a>'


def parse_one(card, resolver):
    [listing] = parse_argenprop(f"<div>{card}</div>", resolver)
    return listing


def test_argenprop_cards_without_a_link_are_skipped(resolver):
    assert parse_argenprop(f"<div>{argenprop_card(href=None)}</div>", resolver) == []


def test_argenprop_id_and_type_fall_back_to_url_and_title(resolver):
    card = argenprop_card(
        href="https://www.argenprop.com/aviso--987",
        body='<h2 class="card__title">PH en alquiler</h2>',
    )
    listing = parse_one(card, resolver)
    assert listing.url == "https://www.argenprop.com/aviso--987"
    assert listing.external_id == "987"
    assert listing.property_type is PropertyType.PH
    assert listing.operation is Operation.ALQUILER


def test_argenprop_without_ids_reads_the_barrio_label(resolver):
    card = argenprop_card(body='<p class="card__address">Gurruchaga 1200, Palermo</p>')
    listing = parse_one(card, resolver)
    assert (listing.city, listing.neighborhood) == ("CABA", "Palermo")
    assert listing.address == "Gurruchaga 1200"


def test_argenprop_city_id_plus_label_gives_the_barrio(resolver):
    card = argenprop_card(
        idlocalidad="2102", body='<p class="card__address">Junín 1200, Recoleta</p>'
    )
    assert parse_one(card, resolver).neighborhood == "Recoleta"


@pytest.mark.parametrize(
    "ids,label,expected",
    [
        ({"idlocalidad": "1936", "idpartido": "124"}, "Nordelta", ("AMBA", "Tigre")),
        ({"idlocalidad": "10973", "idpartido": "328"}, "Godoy Cruz", ("Mendoza", "Godoy Cruz")),
        (
            {"idlocalidad": "4202", "idpartido": "193"},
            "Nueva Córdoba",
            ("Córdoba", "Nueva Córdoba"),
        ),
        ({"idlocalidad": "20921", "idpartido": "500"}, "Barrio Norte", ("Tucumán", "Barrio Norte")),
        ({"idlocalidad": "13017", "idpartido": "366"}, "Neuquén", ("Neuquén", None)),
    ],
)
def test_argenprop_cards_outside_caba_find_their_city_by_partido(resolver, ids, label, expected):
    card = argenprop_card(body=f'<p class="card__address">Calle 1, {label}</p>', **ids)
    listing = parse_one(card, resolver)
    assert (listing.city, listing.neighborhood) == expected


def test_argenprop_price_from_attributes_when_the_text_has_none(resolver):
    card = argenprop_card(idmoneda="1", montooperacion="850000")
    listing = parse_one(card, resolver)
    assert (listing.price, listing.currency) == (850000.0, "ARS")
    assert listing.price_is_approximate is False


def test_argenprop_features_prefer_total_area_and_skip_noise(resolver):
    features = (
        '<ul class="card__main-features"><li> </li><li>muchos m2</li>'
        "<li>40 m² cubie.</li><li>55 m² tot.</li></ul>"
    )
    listing = parse_one(argenprop_card(body=features, ambientes="3 ambientes"), resolver)
    assert listing.area_m2 == 55
    assert listing.rooms == 3
    assert listing.bedrooms == 2  # from ambientes, no dormitorios attribute


# ---------------------------------------------------------------- cabaprop


def cabaprop_item(**changes):
    item = json.loads(load_fixture("cabaprop_search.json"))["result"][0]
    item.update(changes)
    return item


def parse_items(resolver, *items):
    return parse_search_response(json.dumps({"result": list(items)}), resolver)


def test_cabaprop_invalid_json_is_an_error(resolver):
    with pytest.raises(FetchError, match="invalid JSON"):
        parse_search_response("<html>", resolver)


def test_cabaprop_skips_an_item_it_cannot_read(resolver):
    listings = parse_items(resolver, {"_id": "broken", "price": "lots"}, cabaprop_item())
    assert len(listings) == 1


def test_cabaprop_unreadable_bedrooms_fall_back_to_rooms(resolver):
    item = cabaprop_item(characteristics={"ambience": 3, "bedrooms": "n/a"})
    [listing] = parse_items(resolver, item)
    assert listing.bedrooms == 2


def test_cabaprop_reads_a_stated_unfurnished(resolver):
    publication = {"attributes": [{"id": "FURNISHED", "value_name": "No"}]}
    [listing] = parse_items(resolver, cabaprop_item(publication=publication))
    assert listing.furnished is False


# ---------------------------------------------------------------- mercadolibre


def meli_card(inner):
    return f'<ol><li class="ui-search-layout__item">{inner}</li></ol>'


def test_mercadolibre_cards_without_a_link_are_skipped(resolver):
    assert parse_mercadolibre(meli_card("<span>no link</span>"), resolver) == []


def test_mercadolibre_price_from_symbol_and_fraction(resolver):
    html = meli_card(
        '<a class="poly-component__title" href="https://x.mercadolibre.com.ar/MLA-5-x">Depto</a>'
        '<span class="andes-money-amount"><span class="andes-money-amount__currency-symbol">'
        'US$</span><span class="andes-money-amount__fraction">700</span></span>'
    )
    [listing] = parse_mercadolibre(html, resolver)
    assert (listing.price, listing.currency) == (700.0, "USD")


# ---------------------------------------------------------------- parsing helpers


def test_parsing_helpers_reject_what_is_not_a_value():
    assert parsing.normalize_spaces(None) is None
    assert parsing.normalize_spaces(" \xa0 ") is None
    assert parsing.parse_number(None) is None
    assert parsing.parse_number("sin precio") is None
    assert parsing.parse_number("38.5") == 38.5
    assert parsing.parse_area("12.5 m²") == 12.5
    assert parsing.detect_currency("consultar") is None
    assert parsing.parse_area("250.000 m²") is None  # not a flat
    assert parsing.parse_rooms(None) is None
    assert parsing.parse_property_type(None) is PropertyType.OTHER


def test_resolver_ignores_empty_input(resolver):
    assert resolver.city_from_label(None) is None
    assert resolver.from_slug("mercadolibre", None) is None
    assert resolver.resolve_key("Narnia/Centro") is None
    assert resolver.from_ids("argenprop", ["", None]) is None
    assert Location("CABA") == resolver.from_id("argenprop", "localidad:2102")
