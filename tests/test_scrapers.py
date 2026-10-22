"""Scraper wiring: URL construction, block detection, per-source isolation."""

import json

import pytest
import requests
from conftest import make_listing

from monitor.config import Config, SearchCriteria, SourceConfig, TelegramConfig
from monitor.models.listing import Listing, PropertyType
from monitor.models.result import SourceStatus
from monitor.neighborhoods import NeighborhoodResolver
from monitor.scrapers.argenprop import ArgenPropScraper
from monitor.scrapers.base import BaseScraper, BlockedError, FetchError, HttpClient
from monitor.scrapers.mercadolibre import MercadoLibreScraper
from monitor.scrapers.zonaprop import ZonaPropScraper


def build_config():
    return Config(
        criteria=SearchCriteria(),
        telegram=TelegramConfig(),
        sources={
            "mercadolibre": SourceConfig(name="mercadolibre"),
            "argenprop": SourceConfig(name="argenprop"),
            "zonaprop": SourceConfig(name="zonaprop"),
        },
    )


class FakeResponse:
    def __init__(self, status_code=200, text="", headers=None):
        self.status_code = status_code
        self.text = text
        self.headers = headers or {}

    def json(self):
        return json.loads(self.text)


# ---------------------------------------------------------------- http client


@pytest.mark.parametrize(
    "response,message",
    [
        (FakeResponse(403, "<html><title>Just a moment...</title>"), "Cloudflare"),
        (FakeResponse(403, "", {"cf-mitigated": "challenge"}), "Cloudflare"),
        (FakeResponse(202, "   "), "WAF throttling"),
        (FakeResponse(429, "slow down"), "rate limited"),
    ],
)
def test_anti_bot_responses_are_recognized(response, message):
    assert message in HttpClient._detect_block(response)


def test_normal_responses_are_not_treated_as_blocks():
    assert HttpClient._detect_block(FakeResponse(200, "<html>listings</html>")) is None
    assert HttpClient._detect_block(FakeResponse(404, "not found")) is None


def test_blocked_response_raises_immediately_without_retrying(monkeypatch):
    client = HttpClient("test-agent", max_retries=3, default_delay=0)
    calls = {"count": 0}

    def fake_get(*args, **kwargs):
        calls["count"] += 1
        return FakeResponse(202, "")

    monkeypatch.setattr(client.session, "get", fake_get)
    with pytest.raises(BlockedError):
        client.get("https://example.com/search")
    assert calls["count"] == 1  # retrying a block only makes it worse


def test_server_errors_are_retried_then_reported(monkeypatch):
    client = HttpClient("test-agent", max_retries=2, default_delay=0)
    calls = {"count": 0}

    def fake_get(*args, **kwargs):
        calls["count"] += 1
        return FakeResponse(503, "boom")

    monkeypatch.setattr(client.session, "get", fake_get)
    monkeypatch.setattr("monitor.scrapers.base.time.sleep", lambda *_: None)
    with pytest.raises(FetchError):
        client.get("https://example.com/search")
    assert calls["count"] == 2


def test_network_errors_are_retried(monkeypatch):
    client = HttpClient("test-agent", max_retries=2, default_delay=0)
    calls = {"count": 0}

    def fake_get(*args, **kwargs):
        calls["count"] += 1
        raise requests.ConnectTimeout("timeout")

    monkeypatch.setattr(client.session, "get", fake_get)
    monkeypatch.setattr("monitor.scrapers.base.time.sleep", lambda *_: None)
    with pytest.raises(FetchError):
        client.get("https://example.com/search")
    assert calls["count"] == 2


# ---------------------------------------------------------------- url building


def test_mercadolibre_url_carries_every_filter():
    config = build_config()
    scraper = MercadoLibreScraper(
        config, config.sources["mercadolibre"], NeighborhoodResolver(), HttpClient("ua")
    )
    url = scraper.build_url(PropertyType.DEPARTAMENTO, "capital-federal/barrio-norte")
    assert url == (
        "https://inmuebles.mercadolibre.com.ar/departamentos/alquiler/capital-federal/"
        "barrio-norte/_PriceRange_0ARS-1000000ARS_TOTAL*AREA_50-*"
    )
    assert "_Desde_49" in scraper.build_url(PropertyType.PH, "capital-federal/recoleta", offset=48)


def test_mercadolibre_searches_each_sub_zone_of_a_chosen_barrio():
    config = build_config()
    config.criteria.neighborhoods = ["CABA/Palermo"]
    scraper = MercadoLibreScraper(
        config, config.sources["mercadolibre"], NeighborhoodResolver(), HttpClient("ua")
    )
    searches = scraper.searches()
    assert ("capital-federal/palermo-soho", False) in searches
    assert all(not whole for _path, whole in searches)


def test_mercadolibre_searches_a_whole_city_by_todays_listings():
    config = build_config()
    config.criteria.cities = ["CABA", "AMBA"]
    config.criteria.neighborhoods = ["CABA/Recoleta"]
    scraper = MercadoLibreScraper(
        config, config.sources["mercadolibre"], NeighborhoodResolver(), HttpClient("ua")
    )
    searches = scraper.searches()
    assert ("capital-federal/recoleta", False) in searches
    assert ("bsas-gba-norte", True) in searches and ("bsas-gba-sur", True) in searches
    url = scraper.build_url(PropertyType.DEPARTAMENTO, "bsas-gba-norte", published_today=True)
    assert "/bsas-gba-norte/_PublishedToday_YES_PriceRange" in url


def test_argenprop_combines_both_neighborhoods_into_one_request():
    config = build_config()
    scraper = ArgenPropScraper(
        config, config.sources["argenprop"], NeighborhoodResolver(), HttpClient("ua")
    )
    assert scraper.segments() == ["barrio-norte-o-recoleta"]
    url = scraper.build_url(PropertyType.DEPARTAMENTO, "barrio-norte-o-recoleta")
    assert url == (
        "https://www.argenprop.com/departamentos/alquiler/barrio-norte-o-recoleta?orden-masnuevos"
    )


def test_argenprop_searches_the_whole_city_when_robots_forbids_combining():
    # robots.txt disallows /*-o-*-o-* , i.e. three or more combined values;
    # the city's newest-first search then covers them in one request.
    config = build_config()
    config.criteria.neighborhoods = ["CABA/Recoleta", "CABA/Barrio Norte", "CABA/Palermo"]
    scraper = ArgenPropScraper(
        config, config.sources["argenprop"], NeighborhoodResolver(), HttpClient("ua")
    )
    assert scraper.segments() == ["capital-federal"]


def test_argenprop_uses_the_city_search_for_barrios_without_a_slug():
    config = build_config()
    config.criteria.neighborhoods = ["CABA/Agronomía"]
    scraper = ArgenPropScraper(
        config, config.sources["argenprop"], NeighborhoodResolver(), HttpClient("ua")
    )
    assert scraper.segments() == ["capital-federal"]


def argenprop_segments(cities, neighborhoods):
    config = build_config()
    config.criteria.cities = cities
    config.criteria.neighborhoods = neighborhoods
    scraper = ArgenPropScraper(
        config, config.sources["argenprop"], NeighborhoodResolver(), HttpClient("ua")
    )
    return scraper.segments()


def test_argenprop_searches_other_cities_by_their_own_slug():
    assert argenprop_segments(["Córdoba", "Rosario"], []) == ["cordoba", "rosario"]
    # A barrio without a slug of its own: the city's search, then its name.
    assert argenprop_segments(["Córdoba"], ["Córdoba/Nueva Córdoba"]) == ["cordoba"]
    assert argenprop_segments(["Tucumán"], ["Tucumán/Yerba Buena"]) == ["yerba-buena"]


def test_argenprop_searches_amba_partidos_one_by_one_past_two():
    two = argenprop_segments(["AMBA"], ["AMBA/Tigre", "AMBA/Pilar"])
    assert two == ["partido-de-pilar-o-partido-de-tigre"]
    three = argenprop_segments(["AMBA"], ["AMBA/Tigre", "AMBA/Pilar", "AMBA/Vicente López"])
    assert three == ["partido-de-tigre", "partido-de-pilar", "partido-de-vicente-lopez"]


# ---------------------------------------------------------------- zonaprop


def test_zonaprop_is_disabled_by_default_and_makes_no_requests():
    config = build_config()
    source_config = config.sources["zonaprop"]
    source_config.options = {"strategy": "disabled"}
    scraper = ZonaPropScraper(config, source_config, NeighborhoodResolver(), HttpClient("ua"))

    result = scraper.scrape()
    assert result.status is SourceStatus.DISABLED
    assert result.requests_made == 0
    assert "Cloudflare" in (result.error or "")
    assert result.partial is True


def test_zonaprop_provider_strategy_requires_credentials(monkeypatch):
    config = build_config()
    source_config = config.sources["zonaprop"]
    source_config.options = {"strategy": "provider"}
    monkeypatch.delenv("ZONAPROP_PROVIDER_URL", raising=False)
    monkeypatch.delenv("ZONAPROP_API_KEY", raising=False)
    scraper = ZonaPropScraper(config, source_config, NeighborhoodResolver(), HttpClient("ua"))

    result = scraper.scrape()
    assert result.status is SourceStatus.ERROR
    assert "ZONAPROP_PROVIDER_URL" in (result.error or "")


# ---------------------------------------------------------------- base class


def test_scrape_converts_failures_into_results():
    class Failing(BaseScraper):
        name = "failing"

        def collect(self):
            raise BlockedError("Cloudflare challenge (HTTP 403)")

    config = build_config()
    scraper = Failing(
        config, SourceConfig(name="failing"), NeighborhoodResolver(), HttpClient("ua")
    )
    result = scraper.scrape()
    assert result.status is SourceStatus.BLOCKED
    assert result.listings == []
    assert result.partial is True


def test_scrape_returns_listings_on_success():
    class Working(BaseScraper):
        name = "working"

        def collect(self):
            return [make_listing()]

    config = build_config()
    scraper = Working(
        config, SourceConfig(name="working"), NeighborhoodResolver(), HttpClient("ua")
    )
    result = scraper.scrape()
    assert result.status is SourceStatus.OK
    assert result.found == 1


# ---------------------------------------------------------------- api adapter


def test_api_results_map_onto_the_same_normalized_model(fixture_loader):
    from monitor.scrapers.mercadolibre.api import item_to_listing

    payload = json.loads(fixture_loader("mercadolibre_api_search.json"))
    listings = [item_to_listing(item, NeighborhoodResolver()) for item in payload["results"]]

    first, second = listings
    assert isinstance(first, Listing)
    assert first.external_id == "MLA1234567890"
    assert first.property_type is PropertyType.DEPARTAMENTO
    assert first.neighborhood == "Recoleta"
    assert first.price == 900000 and first.currency == "ARS"
    assert first.area_m2 == 72  # total area preferred over covered
    assert first.rooms == 3
    assert first.expensas == 150000

    assert second.currency == "USD"
    assert second.neighborhood == "Barrio Norte"
    assert second.area_m2 == 55  # falls back to covered area
