"""Parser tests against HTML captured from ArgenProp's live search page."""

from monitor.models.listing import Operation, PropertyType
from monitor.scrapers.argenprop.parser import parse_search_page


def test_parses_cards_from_the_live_capture(fixture_loader, resolver):
    listings = parse_search_page(fixture_loader("argenprop_search.html"), resolver)
    assert len(listings) == 4
    assert all(listing.source == "argenprop" for listing in listings)


def test_normalizes_a_complete_card(fixture_loader, resolver):
    listing = parse_search_page(fixture_loader("argenprop_search.html"), resolver)[0]
    assert listing.external_id == "20172118"
    assert listing.url.startswith("https://www.argenprop.com/")
    assert listing.property_type is PropertyType.DEPARTAMENTO
    assert listing.operation is Operation.ALQUILER
    assert listing.neighborhood == "Recoleta"
    assert listing.price == 680000.0
    assert listing.currency == "ARS"
    assert listing.expensas == 130000.0
    assert listing.area_m2 == 38.0
    assert listing.rooms == 2
    assert listing.address == "Charcas 4000, Piso 3"


def test_expensas_are_parsed_but_never_added_to_the_price(fixture_loader, resolver, criteria):
    from monitor.services.filtering import ListingFilter

    listings = parse_search_page(fixture_loader("argenprop_search.html"), resolver)
    listing = listings[2]  # $ 900.000 + $ 260.000 expensas
    assert listing.price == 900000.0
    assert listing.expensas == 260000.0
    # price + expensas would exceed the limit; price alone does not.
    assert listing.price + listing.expensas > criteria.max_rent
    listing.area_m2 = 60.0
    assert ListingFilter(criteria).evaluate(listing).accepted is True


def test_card_without_square_meters_yields_no_area(fixture_loader, resolver):
    listings = parse_search_page(fixture_loader("argenprop_search.html"), resolver)
    without_area = [listing for listing in listings if listing.area_m2 is None]
    assert without_area, "the capture contains a card with no m² shown"
    assert without_area[0].external_id == "9050610"


def test_usd_card_keeps_its_currency(fixture_loader, resolver):
    listings = parse_search_page(fixture_loader("argenprop_search.html"), resolver)
    usd = [listing for listing in listings if listing.currency == "USD"]
    assert usd and usd[0].price == 1700.0


def test_structured_amount_mismatch_marks_price_unreliable(fixture_loader, resolver):
    html = fixture_loader("argenprop_search.html").replace(
        'montooperacion="680000"', 'montooperacion="999999"'
    )
    listing = parse_search_page(html, resolver)[0]
    assert listing.price_is_approximate is True


def test_empty_page_returns_no_listings(resolver):
    assert parse_search_page("<html><body></body></html>", resolver) == []


def test_neighborhood_comes_from_the_barrio_id_not_the_printed_label(fixture_loader, resolver):
    # Live behaviour: ArgenProp prints the sub-barrio "Parque Las Heras" on a
    # card it files under idbarrio=4 (Barrio Norte).
    html = fixture_loader("argenprop_search.html")
    html = html.replace('idbarrio="12618"', 'idbarrio="4"', 1)
    html = html.replace("Recoleta</span>", "Parque Las Heras</span>", 1)
    listing = parse_search_page(html, resolver)[0]
    assert listing.neighborhood == "Barrio Norte"
    assert listing.raw_neighborhood in ("Parque Las Heras", "Recoleta")


def test_unknown_barrio_id_falls_back_to_the_printed_label(fixture_loader, resolver):
    html = fixture_loader("argenprop_search.html").replace(
        'idbarrio="12618"', 'idbarrio="99999"', 1
    )
    assert parse_search_page(html, resolver)[0].neighborhood == "Recoleta"


def test_currency_mismatch_between_symbol_and_id_is_refused(fixture_loader, resolver):
    # idmoneda=2 is USD while the card prints "$": the amount cannot be
    # attributed to a currency, so it must not be compared against an ARS cap.
    html = fixture_loader("argenprop_search.html").replace('idmoneda="1"', 'idmoneda="2"', 1)
    listing = parse_search_page(html, resolver)[0]
    assert listing.currency is None
    assert listing.price_is_approximate is True
