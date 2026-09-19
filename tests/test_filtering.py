"""Filtering rules: price, area, type, neighborhood, currency, missing data."""

import pytest
from conftest import make_listing

from monitor.config import SearchCriteria
from monitor.models.listing import Operation, PropertyType
from monitor.services.filtering import ListingFilter, RejectReason


@pytest.fixture
def listing_filter(criteria):
    return ListingFilter(criteria)


def test_accepts_a_fully_matching_listing(listing_filter):
    assert listing_filter.evaluate(make_listing()).accepted is True


# ---------------------------------------------------------------- price


def test_rejects_price_above_maximum(listing_filter):
    decision = listing_filter.evaluate(make_listing(price=1_000_001))
    assert (decision.accepted, decision.reason) == (False, RejectReason.PRICE_ABOVE_MAX)


def test_accepts_price_exactly_at_the_maximum(listing_filter):
    assert listing_filter.evaluate(make_listing(price=1_000_000)).accepted is True


def test_expensas_are_excluded_from_the_price_comparison(listing_filter):
    # 950.000 rent + 300.000 expensas: the rent alone is what counts.
    assert listing_filter.evaluate(make_listing(price=950_000, expensas=300_000)).accepted is True


def test_rejects_missing_price(listing_filter):
    decision = listing_filter.evaluate(make_listing(price=None))
    assert decision.reason is RejectReason.PRICE_MISSING


def test_rejects_desde_prices(listing_filter):
    decision = listing_filter.evaluate(make_listing(price_is_approximate=True))
    assert decision.reason is RejectReason.PRICE_UNRELIABLE


def test_rejects_zero_or_negative_price(listing_filter):
    assert listing_filter.evaluate(make_listing(price=0)).reason is RejectReason.PRICE_UNRELIABLE


# ---------------------------------------------------------------- currency


def test_rejects_usd_listings(listing_filter):
    decision = listing_filter.evaluate(make_listing(price=900, currency="USD"))
    assert decision.reason is RejectReason.CURRENCY


def test_rejects_unknown_currency(listing_filter):
    assert listing_filter.evaluate(make_listing(currency=None)).reason is RejectReason.CURRENCY


# ---------------------------------------------------------------- area


def test_rejects_area_below_minimum(listing_filter):
    decision = listing_filter.evaluate(make_listing(area_m2=49.9))
    assert decision.reason is RejectReason.AREA_BELOW_MIN


def test_accepts_area_exactly_at_the_minimum(listing_filter):
    assert listing_filter.evaluate(make_listing(area_m2=50)).accepted is True


def test_rejects_missing_area(listing_filter):
    assert listing_filter.evaluate(make_listing(area_m2=None)).reason is RejectReason.AREA_MISSING


# ---------------------------------------------------------------- type


@pytest.mark.parametrize(
    "property_type", [PropertyType.DEPARTAMENTO, PropertyType.CASA, PropertyType.PH]
)
def test_accepts_configured_property_types(listing_filter, property_type):
    assert listing_filter.evaluate(make_listing(property_type=property_type)).accepted is True


def test_rejects_other_property_types(listing_filter):
    decision = listing_filter.evaluate(make_listing(property_type=PropertyType.OTHER))
    assert decision.reason is RejectReason.PROPERTY_TYPE


def test_property_types_are_configurable():
    criteria = SearchCriteria(property_types=[PropertyType.CASA])
    decision = ListingFilter(criteria).evaluate(
        make_listing(property_type=PropertyType.DEPARTAMENTO)
    )
    assert decision.reason is RejectReason.PROPERTY_TYPE


# ---------------------------------------------------------------- neighborhood


def test_rejects_unknown_neighborhood(listing_filter):
    decision = listing_filter.evaluate(make_listing(neighborhood=None))
    assert decision.reason is RejectReason.NEIGHBORHOOD_UNKNOWN


def test_rejects_neighborhood_outside_the_search(listing_filter):
    decision = listing_filter.evaluate(make_listing(neighborhood="Palermo"))
    assert decision.reason is RejectReason.NEIGHBORHOOD_NOT_ALLOWED


def test_accepts_barrio_norte(listing_filter):
    assert listing_filter.evaluate(make_listing(neighborhood="Barrio Norte")).accepted is True


# ---------------------------------------------------------------- operation


def test_rejects_sales(listing_filter):
    decision = listing_filter.evaluate(make_listing(operation=Operation.VENTA))
    assert decision.reason is RejectReason.OPERATION


def test_rejects_temporary_rentals(listing_filter):
    decision = listing_filter.evaluate(make_listing(operation=Operation.ALQUILER_TEMPORAL))
    assert decision.reason is RejectReason.TEMPORARY_RENTAL


def test_rejects_unknown_operation(listing_filter):
    assert (
        listing_filter.evaluate(make_listing(operation=Operation.OTHER)).reason
        is RejectReason.OPERATION
    )


def test_criteria_can_be_reconfigured():
    criteria = SearchCriteria(max_rent=500_000, min_area_m2=100)
    strict = ListingFilter(criteria)
    assert strict.evaluate(make_listing()).accepted is False
    assert strict.evaluate(make_listing(price=400_000, area_m2=120)).accepted is True
