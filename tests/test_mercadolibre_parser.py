"""Parser tests against HTML captured from MercadoLibre's live search page."""

from monitor.models.listing import Operation, PropertyType
from monitor.scrapers.mercadolibre.parser import clean_url, extract_item_id, parse_search_page


def test_parses_every_card_from_the_live_capture(fixture_loader, resolver):
    listings = parse_search_page(fixture_loader("mercadolibre_search.html"), resolver)
    assert len(listings) == 4
    assert all(listing.source == "mercadolibre" for listing in listings)
    assert all(listing.external_id for listing in listings)
    assert all(listing.url.startswith("https://") for listing in listings)


def test_normalizes_type_operation_and_neighborhood(fixture_loader, resolver):
    listing = parse_search_page(fixture_loader("mercadolibre_search.html"), resolver)[0]
    assert listing.external_id == "MLA2090769683"
    assert listing.property_type is PropertyType.DEPARTAMENTO
    assert listing.operation is Operation.ALQUILER
    assert listing.neighborhood == "Recoleta"
    assert listing.price == 850000.0
    assert listing.currency == "ARS"
    assert listing.area_m2 == 39.0
    assert listing.rooms == 1


def test_detects_usd_listings(fixture_loader, resolver):
    listings = parse_search_page(fixture_loader("mercadolibre_search.html"), resolver)
    usd = [listing for listing in listings if listing.currency == "USD"]
    assert usd, "the capture contains a USD listing"
    assert usd[0].price == 550.0


def test_tracking_parameters_are_stripped_from_urls(fixture_loader, resolver):
    listing = parse_search_page(fixture_loader("mercadolibre_search.html"), resolver)[0]
    assert "#" not in listing.url and "?" not in listing.url


def test_edge_cases_fixture(fixture_loader, resolver):
    listings = parse_search_page(fixture_loader("mercadolibre_edge_cases.html"), resolver)
    by_id = {listing.external_id: listing for listing in listings}

    passing = by_id["MLA1111111111"]
    assert (passing.area_m2, passing.price, passing.neighborhood) == (72.0, 900000.0, "Recoleta")

    desde = by_id["MLA2222222222"]
    assert desde.property_type is PropertyType.PH
    assert desde.neighborhood == "Barrio Norte"
    assert desde.price_is_approximate is True

    elsewhere = by_id["MLA3333333333"]
    assert (elsewhere.city, elsewhere.neighborhood) == ("CABA", "Palermo")

    temporary = by_id["MLA4444444444"]
    assert temporary.operation is Operation.ALQUILER_TEMPORAL
    assert temporary.price is None  # no price node at all


def test_extract_item_id_and_clean_url():
    url = "https://departamento.mercadolibre.com.ar/MLA-123456789-depto-_JM#polycard_client=x"
    assert extract_item_id(url) == "MLA123456789"
    assert clean_url(url) == "https://departamento.mercadolibre.com.ar/MLA-123456789-depto-_JM"
    assert extract_item_id("https://example.com/no-id") is None


def test_empty_page_returns_no_listings(resolver):
    assert parse_search_page("<html><body>nothing here</body></html>", resolver) == []


def card_page(location):
    return f"""<ol><li class="ui-search-layout__item">
      <a class="poly-component__title" href="https://departamento.mercadolibre.com.ar/MLA-1-x">Depto</a>
      <span class="poly-component__location">{location}</span>
    </li></ol>"""


def test_a_barrio_search_fills_in_the_barrio_a_card_leaves_out(resolver):
    # The centre of Mendoza: the card names the province only.
    [listing] = parse_search_page(card_page("Necochea 80, Mendoza"), resolver, "mendoza/mendoza")
    assert (listing.city, listing.neighborhood) == ("Mendoza", "Ciudad de Mendoza")


def test_what_the_card_says_wins_over_the_search(resolver):
    [listing] = parse_search_page(
        card_page("Calle 1, Godoy Cruz, Mendoza"), resolver, "mendoza/mendoza"
    )
    assert listing.neighborhood == "Godoy Cruz"
    [listing] = parse_search_page(
        card_page("Ayacucho 800, Recoleta, Capital Federal"), resolver, "mendoza/mendoza"
    )
    assert (listing.city, listing.neighborhood) == ("CABA", "Recoleta")


def test_a_whole_city_search_fills_in_nothing(resolver):
    [listing] = parse_search_page(
        card_page("Calle 46 Al 525, La Plata, Bs.As. G.B.A. Sur"), resolver, "bsas-gba-sur/la-plata"
    )
    assert (listing.city, listing.neighborhood) == ("La Plata", None)
