"""Cycle behaviour: per-source isolation, bootstrap, backoff, logging counts."""

from conftest import make_listing

from monitor.config import Config, SearchCriteria, SourceConfig, TelegramConfig
from monitor.models.result import ScrapeResult, SourceStatus
from monitor.scheduler import MonitorService
from monitor.scrapers.base import BaseScraper
from monitor.services.notification.notifier import Notifier


class FakeNotifier(Notifier):
    def __init__(self):
        self.sent = []

    def send(self, message):
        self.sent.append(message)
        return True

    @property
    def messages(self):
        return [message.telegram_html for message in self.sent]


class FakeScraper(BaseScraper):
    def __init__(self, name, listings=None, status=SourceStatus.OK, error=None, raises=None):
        self.name = name
        self._listings = listings or []
        self._status = status
        self._error = error
        self._raises = raises
        self.source_config = SourceConfig(name=name)
        self.calls = 0

    def collect(self):
        raise NotImplementedError

    def scrape(self):
        self.calls += 1
        if self._raises:
            raise self._raises
        return ScrapeResult(
            source=self.name,
            status=self._status,
            listings=list(self._listings),
            error=self._error,
            partial=self._status is not SourceStatus.OK,
        )


def build_config(**overrides):
    config = Config(
        criteria=SearchCriteria(),
        telegram=TelegramConfig(bot_token="t", chat_id="1", group_threshold=3),
        sources={"mercadolibre": SourceConfig(name="mercadolibre")},
        notify_on_first_run=True,
    )
    for key, value in overrides.items():
        setattr(config, key, value)
    return config


def build_service(database, scrapers, **overrides):
    return MonitorService(
        build_config(**overrides), database, scrapers=scrapers, notifier=FakeNotifier()
    )


def test_cycle_counts_found_matching_and_new(database):
    listings = [
        make_listing(external_id="1"),
        make_listing(external_id="2", price=5_000_000),  # too expensive
        make_listing(external_id="3", area_m2=20),  # too small
    ]
    service = build_service(database, [FakeScraper("mercadolibre", listings)])

    report = service.run_cycle()
    assert report.found == 3
    assert report.matched == 1
    assert report.new == 1
    assert report.notifications_sent == 1


def test_second_cycle_reports_no_new_listings(database):
    scraper = FakeScraper("mercadolibre", [make_listing()])
    service = build_service(database, [scraper])

    service.run_cycle()
    report = service.run_cycle()
    assert report.found == 1
    assert report.new == 0
    assert report.notifications_sent == 0


def test_a_failing_source_does_not_stop_the_others(database):
    good = FakeScraper("mercadolibre", [make_listing()])
    bad = FakeScraper("argenprop", status=SourceStatus.ERROR, error="boom")
    service = build_service(database, [bad, good])

    report = service.run_cycle()
    statuses = {item.source: item.status for item in report.sources}
    assert statuses["argenprop"] is SourceStatus.ERROR
    assert statuses["mercadolibre"] is SourceStatus.OK
    assert report.new == 1


def test_the_loop_survives_a_cycle_that_raises(database):
    service = build_service(database, [FakeScraper("mercadolibre")], check_interval_seconds=1)
    calls = {"count": 0}

    def exploding_cycle():
        calls["count"] += 1
        if calls["count"] >= 2:
            service.stop()
        raise RuntimeError("unexpected failure")

    service.run_cycle = exploding_cycle
    service.run_forever()  # must return normally instead of propagating
    assert calls["count"] >= 2


def test_an_unexpected_scraper_error_is_contained_in_the_result(database):
    class ExplodingScraper(FakeScraper):
        def collect(self):
            raise ValueError("parser blew up")

        scrape = BaseScraper.scrape

    scraper = ExplodingScraper("argenprop")
    scraper.logger = __import__("logging").getLogger("test")
    scraper._requests_made = 0
    report = build_service(database, [scraper]).run_cycle()
    assert report.sources[0].status is SourceStatus.ERROR
    assert "parser blew up" in report.sources[0].error


def test_blocked_source_is_backed_off_and_skipped_next_cycle(database):
    blocked = FakeScraper("argenprop", status=SourceStatus.BLOCKED, error="WAF throttling")
    service = build_service(database, [blocked])

    service.run_cycle()
    assert blocked.calls == 1
    report = service.run_cycle()
    assert blocked.calls == 1  # not attempted again
    assert report.sources[0].status is SourceStatus.SKIPPED


def test_blocked_source_is_never_reported_as_zero_listings(database):
    blocked = FakeScraper("argenprop", status=SourceStatus.BLOCKED, error="Cloudflare challenge")
    report = build_service(database, [blocked]).run_cycle()
    assert report.sources[0].status is SourceStatus.BLOCKED
    assert report.sources[0].found == 0
    assert report.sources_ok == 0


def test_first_run_records_listings_without_notifying(database):
    service = build_service(
        database,
        [FakeScraper("mercadolibre", [make_listing(external_id=str(i)) for i in range(4)])],
        notify_on_first_run=False,
    )
    report = service.run_cycle()
    assert report.suppressed_on_bootstrap == 4
    assert report.notifications_sent == 0
    assert service.notifier.messages == []


def test_listings_found_after_the_first_run_are_notified(database):
    scraper = FakeScraper("mercadolibre", [make_listing(external_id="old")])
    service = build_service(database, [scraper], notify_on_first_run=False)
    service.run_cycle()

    scraper._listings.append(
        make_listing(
            external_id="fresh", url="https://departamento.mercadolibre.com.ar/MLA-fresh-_JM"
        )
    )
    report = service.run_cycle()
    assert report.new == 1
    assert report.notifications_sent == 1
    assert "MLA-fresh" in service.notifier.messages[0]


def test_a_source_added_later_starts_quietly(database):
    old = FakeScraper("mercadolibre", [make_listing(external_id="old")])
    service = build_service(database, [old], notify_on_first_run=False)
    service.run_cycle()

    added = FakeScraper(
        "cabaprop",
        [
            make_listing(
                source="cabaprop", external_id=str(i), url=f"https://cabaprop.com.ar/p/{i}"
            )
            for i in range(3)
        ],
    )
    old._listings.append(
        make_listing(
            external_id="fresh", url="https://departamento.mercadolibre.com.ar/MLA-fresh-_JM"
        )
    )
    service.scrapers = [old, added]
    report = service.run_cycle()
    assert report.suppressed_on_bootstrap == 3
    # The established source's new listing still goes out in the same cycle.
    assert report.notifications_sent == 1
    assert "MLA-fresh" in service.notifier.messages[-1]


def test_a_source_blocked_on_its_first_run_bootstraps_once_it_works(database):
    scraper = FakeScraper("cabaprop", status=SourceStatus.BLOCKED, error="WAF")
    service = build_service(database, [scraper], notify_on_first_run=False)
    service.run_cycle()

    scraper._status = SourceStatus.OK
    scraper._error = None
    scraper._listings = [make_listing(source="cabaprop", external_id="1")]
    service._should_skip = lambda _scraper: None  # skip the post-block backoff
    report = service.run_cycle()
    assert report.suppressed_on_bootstrap == 1
    assert report.notifications_sent == 0


def test_a_source_that_notified_before_is_not_silenced(database):
    scraper = FakeScraper("mercadolibre", [make_listing(external_id="old")])
    service = build_service(database, [scraper])  # notify_on_first_run=True
    service.run_cycle()

    service.config.notify_on_first_run = False
    scraper._listings.append(
        make_listing(
            external_id="fresh", url="https://departamento.mercadolibre.com.ar/MLA-fresh-_JM"
        )
    )
    report = service.run_cycle()
    assert report.suppressed_on_bootstrap == 0
    assert report.notifications_sent == 1


def test_min_interval_skips_a_source_until_it_elapses(database):
    scraper = FakeScraper("argenprop", [make_listing()])
    scraper.source_config = SourceConfig(name="argenprop", min_interval_seconds=600)
    service = build_service(database, [scraper])

    service.run_cycle()
    report = service.run_cycle()
    assert scraper.calls == 1
    assert report.sources[0].status is SourceStatus.SKIPPED


def test_rejection_reasons_are_reported_for_logging(database):
    listings = [
        make_listing(external_id="1", currency="USD"),
        make_listing(external_id="2", area_m2=10),
    ]
    report = build_service(database, [FakeScraper("mercadolibre", listings)]).run_cycle()
    assert report.sources[0].reject_reasons == {"currency_not_allowed": 1, "area_below_min": 1}
