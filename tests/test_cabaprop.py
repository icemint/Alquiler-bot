"""CabaProp: request body, response mapping and failure reporting."""

import json

import pytest

from monitor.config import Config, SearchCriteria, SourceConfig, TelegramConfig
from monitor.models.listing import Operation, PropertyType
from monitor.models.result import SourceStatus
from monitor.neighborhoods import NeighborhoodResolver
from monitor.scrapers.base import FetchError
from monitor.scrapers.cabaprop import CabaPropScraper
from monitor.scrapers.cabaprop.parser import parse_search_response
from monitor.services.filtering import ListingFilter


class FakeHttp:
    def __init__(self, body):
        self.body = body
        self.calls = []

    def post_json(self, url, payload, delay=None, headers=None):
        self.calls.append((url, payload))
        return self.body


def build_scraper(body="{}", criteria=None):
    config = Config(
        criteria=criteria or SearchCriteria(),
        telegram=TelegramConfig(),
        sources={"cabaprop": SourceConfig(name="cabaprop", options={"page_size": 30})},
    )
    http = FakeHttp(body)
    scraper = CabaPropScraper(config, config.sources["cabaprop"], NeighborhoodResolver(), http)
    return scraper, http


@pytest.fixture
def listings(fixture_loader):
    return parse_search_response(fixture_loader("cabaprop_search.json"), NeighborhoodResolver())


def test_query_carries_operation_types_barrios_price_and_area():
    scraper, _ = build_scraper()
    query = scraper.build_query()
    assert query["operationType"] == 2
    assert query["propertyTypes"] == [1, 2, 3]
    assert sorted(query["barrios"]) == [10, 11, 12, 13, 88]
    assert query["price"] == {"currency": "ARS", "min": 0, "max": 1_000_000}
    assert query["surface"] == {"type": "totalSurface", "min": 50, "max": ""}


def test_query_follows_the_configured_criteria():
    criteria = SearchCriteria(neighborhoods=["Recoleta"], property_types=[PropertyType.PH])
    scraper, _ = build_scraper(criteria=criteria)
    query = scraper.build_query()
    assert query["barrios"] == [88]
    assert query["propertyTypes"] == [3]


def test_results_are_requested_newest_first():
    scraper, _ = build_scraper()
    assert "orderBy=created_at&sort=desc" in scraper.build_url()


def test_items_map_onto_the_normalized_model(listings):
    first = listings[0]
    assert first.dedupe_key == "cabaprop:6ad8318ce1793443d26b4c05"
    assert first.url.startswith("https://cabaprop.com.ar/propiedad/6ad8318ce1793443d26b4c05")
    assert first.operation is Operation.ALQUILER
    assert first.property_type is PropertyType.DEPARTAMENTO
    assert first.neighborhood == "Recoleta"
    assert (first.price, first.currency) == (750_000, "ARS")
    assert first.area_m2 == 50
    assert first.expensas is None  # 0 means "not given", not "free"
    assert first.published_at is not None and first.published_at.year == 2026


def test_expensas_and_sub_barrios_are_read(listings):
    second = listings[1]
    assert second.neighborhood == "Barrio Norte"  # ids [10, 13]
    assert (second.expensas, second.expensas_currency) == (200_000, "ARS")
    assert second.area_m2 == 75


def test_usd_prices_are_kept_in_usd_and_rejected_by_the_ars_filter(listings):
    usd = listings[2]
    assert usd.currency == "USD"
    assert ListingFilter(SearchCriteria()).evaluate(usd).accepted is False


def test_collect_posts_the_query_once(fixture_loader):
    scraper, http = build_scraper(fixture_loader("cabaprop_search.json"))
    result = scraper.scrape()
    assert result.status is SourceStatus.OK
    assert len(result.listings) == 3
    assert len(http.calls) == 1
    assert result.requests_made == 1


def test_a_changed_response_shape_is_an_error_not_zero_listings():
    with pytest.raises(FetchError):
        parse_search_response(json.dumps({"data": []}), NeighborhoodResolver())
    scraper, _ = build_scraper(json.dumps({"unexpected": True}))
    assert scraper.scrape().status is SourceStatus.ERROR


def test_no_known_barrio_ids_means_no_city_wide_request():
    criteria = SearchCriteria(neighborhoods=["Nowhere"])
    scraper, http = build_scraper(criteria=criteria)
    assert scraper.collect() == []
    assert http.calls == []
