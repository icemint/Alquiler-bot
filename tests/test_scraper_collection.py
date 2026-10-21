"""How each scraper walks its searches, pages and failure modes.

The site is replaced by a fake HTTP client and, where only the page count
matters, by a parser stub returning N listings: these tests are about which
requests are made, not about parsing (see the *_parser tests for that).
"""

import logging

import pytest
import requests
from conftest import make_listing
from test_scrapers import FakeResponse, build_config

from monitor.config import SourceConfig
from monitor.models.listing import PropertyType
from monitor.models.result import SourceStatus
from monitor.neighborhoods import NeighborhoodResolver
from monitor.scrapers.argenprop import ArgenPropScraper
from monitor.scrapers.base import BaseScraper, BlockedError, FetchError, HttpClient, ScraperError
from monitor.scrapers.mercadolibre import MercadoLibreScraper
from monitor.scrapers.registry import build_scrapers
from monitor.scrapers.zonaprop import ZonaPropScraper


class FakeHttp:
    """Stands in for HttpClient: answers every GET with ``respond(url)``."""

    def __init__(self, respond=lambda url: ""):
        self.respond = respond
        self.urls = []
        self.headers = []

    def get(self, url, delay=None, headers=None):
        self.urls.append(url)
        self.headers.append(headers)
        return self.respond(url)


def listings(count):
    return [make_listing(external_id=str(i)) for i in range(count)]


def scraper_for(cls, source, http, **criteria):
    config = build_config()
    for key, value in criteria.items():
        setattr(config.criteria, key, value)
    source_config = config.sources.get(source) or SourceConfig(name=source)
    return cls(config, source_config, NeighborhoodResolver(), http)


# ---------------------------------------------------------------- http client


def test_requests_to_one_host_are_spaced_by_the_delay(monkeypatch):
    client = HttpClient("ua", default_delay=5)
    monkeypatch.setattr(client.session, "get", lambda *a, **k: FakeResponse(200, "ok"))
    clock = iter([100.0, 100.0, 102.0, 105.0])
    monkeypatch.setattr("monitor.scrapers.base.time.monotonic", lambda: next(clock))
    slept = []
    monkeypatch.setattr("monitor.scrapers.base.time.sleep", slept.append)

    assert client.get("https://example.com/a") == "ok"
    assert client.get("https://example.com/b") == "ok"
    assert slept == [3.0]  # 5 s apart, 2 s had already passed


def test_post_json_sends_the_body(monkeypatch):
    client = HttpClient("ua", default_delay=0)
    sent = {}

    def fake_post(url, **kwargs):
        sent.update(kwargs)
        return FakeResponse(200, '{"ok": true}')

    monkeypatch.setattr(client.session, "post", fake_post)
    assert client.post_json("https://example.com/api", {"q": 1}) == '{"ok": true}'
    assert sent["json"] == {"q": 1}


def test_client_errors_are_not_retried(monkeypatch):
    client = HttpClient("ua", max_retries=3, default_delay=0)
    calls = []
    monkeypatch.setattr(
        client.session, "get", lambda *a, **k: calls.append(1) or FakeResponse(404, "gone")
    )
    with pytest.raises(FetchError, match="HTTP 404"):
        client.get("https://example.com/missing")
    assert len(calls) == 1


def test_a_failed_request_can_succeed_on_retry(monkeypatch):
    client = HttpClient("ua", max_retries=2, default_delay=0)
    answers = iter([requests.ConnectionError("reset"), FakeResponse(200, "ok")])

    def fake_get(*args, **kwargs):
        answer = next(answers)
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(client.session, "get", fake_get)
    monkeypatch.setattr("monitor.scrapers.base.time.sleep", lambda *_: None)
    assert client.get("https://example.com/") == "ok"


def test_a_captcha_page_is_a_block():
    assert "captcha" in HttpClient._detect_block(FakeResponse(403, "please solve the CAPTCHA"))
    assert HttpClient._detect_block(FakeResponse(200, "captcha mentioned in a listing")) is None


# ---------------------------------------------------------------- base scraper


@pytest.mark.parametrize(
    "error,status,message",
    [
        (BlockedError("WAF"), SourceStatus.BLOCKED, "WAF"),
        (ScraperError("bad page"), SourceStatus.ERROR, "bad page"),
        (KeyError("price"), SourceStatus.ERROR, "KeyError: 'price'"),
    ],
)
def test_every_failure_becomes_a_result(error, status, message):
    class Failing(BaseScraper):
        name = "failing"

        def collect(self):
            self.fetch("https://example.com/")
            raise error

    scraper = scraper_for(Failing, "failing", FakeHttp())
    result = scraper.scrape()
    assert result.status is status
    assert result.error == message
    assert result.requests_made == 1
    assert result.partial is True


# ---------------------------------------------------------------- registry


def test_only_enabled_known_sources_get_a_scraper():
    config = build_config()
    config.sources["zonaprop"].enabled = False
    config.sources["nosuchsite"] = SourceConfig(name="nosuchsite")
    scrapers = build_scrapers(config, NeighborhoodResolver())
    assert [scraper.name for scraper in scrapers] == ["mercadolibre", "argenprop"]
    # One HTTP client, so pacing per host holds across sources.
    assert scrapers[0].http is scrapers[1].http


# ---------------------------------------------------------------- mercadolibre


def test_mercadolibre_asks_for_another_page_only_after_a_full_one(monkeypatch):
    pages = iter([listings(48), listings(3)])
    monkeypatch.setattr(
        "monitor.scrapers.mercadolibre.scraper.parse_search_page", lambda *a, **k: next(pages)
    )
    http = FakeHttp()
    scraper = scraper_for(
        MercadoLibreScraper,
        "mercadolibre",
        http,
        neighborhoods=["CABA/Recoleta"],
        property_types=[PropertyType.DEPARTAMENTO],
    )
    assert len(scraper.collect()) == 51
    assert len(http.urls) == 2
    assert "_Desde_49" in http.urls[1]


def test_mercadolibre_whole_city_stops_at_its_page_cap(monkeypatch, caplog):
    monkeypatch.setattr(
        "monitor.scrapers.mercadolibre.scraper.parse_search_page", lambda *a, **k: listings(48)
    )
    http = FakeHttp()
    scraper = scraper_for(
        MercadoLibreScraper,
        "mercadolibre",
        http,
        neighborhoods=[],
        property_types=[PropertyType.DEPARTAMENTO],
    )
    scraper.source_config.options["city_max_pages"] = 2
    with caplog.at_level(logging.WARNING):
        scraper.collect()
    assert len(http.urls) == 2
    assert all("_PublishedToday_YES" in url for url in http.urls)
    assert "stopped after 2 pages" in caplog.text


def test_mercadolibre_skips_types_it_has_no_url_for(monkeypatch):
    monkeypatch.setattr(
        "monitor.scrapers.mercadolibre.scraper.parse_search_page", lambda *a, **k: []
    )
    http = FakeHttp()
    scraper = scraper_for(
        MercadoLibreScraper,
        "mercadolibre",
        http,
        neighborhoods=["CABA/Recoleta"],
        property_types=[PropertyType.OTHER, PropertyType.PH],
    )
    scraper.collect()
    assert len(http.urls) == 1 and "/ph/alquiler/" in http.urls[0]


def test_mercadolibre_warns_about_places_it_cannot_search(caplog):
    scraper = scraper_for(MercadoLibreScraper, "mercadolibre", FakeHttp())
    scraper.resolver = NeighborhoodResolver({"Nowhere": {"barrios": {"Centro": {}}}})
    scraper.criteria.cities = ["Nowhere"]
    scraper.criteria.neighborhoods = ["Nowhere/Centro"]
    with caplog.at_level(logging.WARNING):
        assert scraper.searches() == []
        scraper.criteria.neighborhoods = []
        assert scraper.searches() == []
    assert "no mercadolibre path for Nowhere/Centro" in caplog.text
    assert "no mercadolibre path for Nowhere" in caplog.text


def test_mercadolibre_api_mode_uses_the_api_adapter(monkeypatch, fixture_loader):
    monkeypatch.setenv("MELI_ACCESS_TOKEN", "token")
    http = FakeHttp(lambda url: fixture_loader("mercadolibre_api_search.json"))
    scraper = scraper_for(MercadoLibreScraper, "mercadolibre", http)
    scraper.source_config.options["mode"] = "api"

    found = scraper.collect()
    assert [listing.external_id for listing in found] == ["MLA1234567890", "MLA9876543210"]
    assert http.urls[0].startswith("https://api.mercadolibre.com/sites/MLA/search?")
    assert "price=0-1000000" in http.urls[0]
    assert http.headers[0] == {"Authorization": "Bearer token"}


def test_mercadolibre_api_mode_needs_a_token(monkeypatch):
    monkeypatch.delenv("MELI_ACCESS_TOKEN", raising=False)
    scraper = scraper_for(MercadoLibreScraper, "mercadolibre", FakeHttp())
    scraper.source_config.options["mode"] = "api"
    result = scraper.scrape()
    assert result.status is SourceStatus.ERROR
    assert "MELI_ACCESS_TOKEN" in result.error


def test_mercadolibre_api_rejects_invalid_json(monkeypatch):
    monkeypatch.setenv("MELI_ACCESS_TOKEN", "token")
    scraper = scraper_for(MercadoLibreScraper, "mercadolibre", FakeHttp(lambda url: "<html>"))
    scraper.source_config.options["mode"] = "api"
    with pytest.raises(FetchError, match="invalid JSON"):
        scraper.collect()


def test_api_attributes_that_are_not_numbers_are_ignored():
    from monitor.scrapers.mercadolibre.api import _number, item_to_listing

    assert _number("n/a") is None and _number("") is None and _number(None) is None
    listing = item_to_listing(
        {"id": "MLA1", "attributes": [{"id": "TOTAL_AREA", "value_name": "unknown"}]},
        NeighborhoodResolver(),
    )
    assert listing.area_m2 is None
    assert listing.property_type is PropertyType.OTHER


# ---------------------------------------------------------------- argenprop


def test_argenprop_pages_until_a_page_is_short(monkeypatch):
    pages = iter([listings(20), listings(5)])
    monkeypatch.setattr(
        "monitor.scrapers.argenprop.scraper.parse_search_page", lambda *a, **k: next(pages)
    )
    http = FakeHttp()
    scraper = scraper_for(
        ArgenPropScraper, "argenprop", http, property_types=[PropertyType.DEPARTAMENTO]
    )
    scraper.source_config.max_pages = 3
    assert len(scraper.collect()) == 25
    assert http.urls == [
        "https://www.argenprop.com/departamentos/alquiler/barrio-norte-o-recoleta?orden-masnuevos",
        "https://www.argenprop.com/departamentos/alquiler/barrio-norte-o-recoleta"
        "?orden-masnuevos&pagina-2",
    ]


def test_argenprop_skips_cities_it_has_no_search_for(monkeypatch, caplog):
    monkeypatch.setattr("monitor.scrapers.argenprop.scraper.parse_search_page", lambda *a, **k: [])
    http = FakeHttp()
    scraper = scraper_for(
        ArgenPropScraper,
        "argenprop",
        http,
        cities=["CABA", "AMBA"],
        neighborhoods=["CABA/Recoleta"],
        property_types=[PropertyType.OTHER, PropertyType.DEPARTAMENTO],
    )
    with caplog.at_level(logging.WARNING):
        scraper.collect()
    assert len(http.urls) == 1 and "/recoleta?" in http.urls[0]
    assert "no argenprop search for AMBA" in caplog.text


# ---------------------------------------------------------------- zonaprop


def test_zonaprop_provider_strategy_has_no_bundled_adapter(monkeypatch):
    monkeypatch.setenv("ZONAPROP_PROVIDER_URL", "https://provider.example")
    monkeypatch.setenv("ZONAPROP_API_KEY", "key")
    scraper = scraper_for(ZonaPropScraper, "zonaprop", FakeHttp())
    scraper.source_config.options = {"strategy": "provider"}
    result = scraper.scrape()
    assert result.status is SourceStatus.ERROR
    assert "no scraping provider adapter" in result.error


def test_zonaprop_http_strategy_fetches_one_page_and_reports_it_cannot_parse():
    http = FakeHttp(lambda url: "<html></html>")
    scraper = scraper_for(ZonaPropScraper, "zonaprop", http, neighborhoods=["CABA/Recoleta"])
    scraper.source_config.options = {"strategy": "http"}
    result = scraper.scrape()
    assert result.status is SourceStatus.ERROR
    assert "no parser exists" in result.error
    assert http.urls == [
        "https://www.zonaprop.com.ar/departamentos-alquiler-recoleta-desde-50-m2-menos-"
        "1000000-pesos.html"
    ]
