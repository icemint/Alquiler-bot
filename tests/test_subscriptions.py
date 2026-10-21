"""Per-subscriber searches: storage, the combined site query and delivery."""

import pytest
from conftest import make_listing
from test_scheduler import FakeScraper

from monitor.config import Config, SearchCriteria, SourceConfig, TelegramConfig
from monitor.models.listing import PropertyType
from monitor.scheduler import MonitorService
from monitor.services.deduplication import Deduplicator
from monitor.services.filtering import FilterDecision
from monitor.services.notification.notifier import NotificationService, RecipientNotifier
from monitor.services.subscriptions import criteria_for, save_criteria, search_scope

ACCEPT = FilterDecision(True)
DEFAULT = SearchCriteria()


class FakeRecipientNotifier(RecipientNotifier):
    def __init__(self, failing=()):
        self.sent = []
        self.failing = set(failing)

    def send(self, message):
        raise AssertionError("per-subscriber delivery must not broadcast")

    def send_to(self, chat_id, message):
        if chat_id in self.failing:
            return False
        self.sent.append((chat_id, message.telegram_html))
        return True

    def to(self, chat_id):
        return [text for chat, text in self.sent if chat == chat_id]


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr("monitor.services.notification.notifier.time.sleep", lambda _s: None)


def subscribe(database, chat_id, **changes):
    database.add_subscriber(chat_id)
    if changes:
        save_criteria(database, chat_id, DEFAULT.with_changes(changes))


# ---------------------------------------------------------------- criteria


def test_a_subscriber_without_settings_uses_the_default_search(database):
    subscribe(database, "1")
    assert criteria_for(database, "1", DEFAULT) == DEFAULT


def test_saved_settings_round_trip(database):
    subscribe(database, "1", neighborhoods=["Recoleta"], max_rent=600_000, property_types=["PH"])
    criteria = criteria_for(database, "1", DEFAULT)
    assert criteria.neighborhoods == ["CABA/Recoleta"]
    assert criteria.max_rent == 600_000
    assert criteria.property_types == [PropertyType.PH]
    assert criteria.min_area_m2 == DEFAULT.min_area_m2


def test_invalid_stored_values_fall_back_to_the_default():
    criteria = DEFAULT.with_changes({"max_rent": "lots", "property_types": ["Castle"]})
    assert criteria.max_rent == DEFAULT.max_rent
    assert criteria.property_types == DEFAULT.property_types


def test_the_site_query_covers_every_subscriber(database):
    subscribe(database, "1", neighborhoods=["Recoleta"], max_rent=600_000, min_area_m2=70)
    subscribe(
        database, "2", neighborhoods=["Barrio Norte"], max_rent=900_000, property_types=["PH"]
    )
    scope = search_scope(database, DEFAULT)
    assert scope.neighborhoods == ["CABA/Recoleta", "CABA/Barrio Norte"]
    assert scope.max_rent == 900_000
    assert scope.min_area_m2 == 50
    assert set(scope.property_types) == set(DEFAULT.property_types)


def test_without_subscribers_the_default_search_is_used(database):
    assert search_scope(database, DEFAULT) == DEFAULT


def test_unsubscribed_chats_do_not_widen_the_query(database):
    subscribe(database, "1", neighborhoods=["Recoleta"])
    subscribe(database, "2", neighborhoods=["Barrio Norte"])
    database.remove_subscriber("2")
    assert search_scope(database, DEFAULT).neighborhoods == ["CABA/Recoleta"]


# ---------------------------------------------------------------- delivery


def queue(database, *listings):
    Deduplicator(database).process([(listing, ACCEPT) for listing in listings])


def service_for(database, notifier):
    return NotificationService(
        database, notifier, TelegramConfig(bot_token="t", group_threshold=3), DEFAULT
    )


def test_each_subscriber_gets_only_their_own_matches(database):
    subscribe(database, "1", neighborhoods=["Recoleta"])
    subscribe(database, "2", neighborhoods=["Barrio Norte"], max_rent=700_000)
    queue(
        database,
        make_listing(external_id="R", neighborhood="Recoleta"),
        make_listing(external_id="N1", neighborhood="Barrio Norte", price=650_000),
        make_listing(external_id="N2", neighborhood="Barrio Norte", price=800_000),
    )
    notifier = FakeRecipientNotifier()

    assert service_for(database, notifier).notify_pending() == 2
    assert len(notifier.to("1")) == 1 and "Recoleta" in notifier.to("1")[0]
    assert len(notifier.to("2")) == 1 and "650.000" in notifier.to("2")[0]
    assert database.pending_notifications() == []


def test_a_failed_chat_is_retried_without_resending_to_the_others(database):
    subscribe(database, "1")
    subscribe(database, "2")
    queue(database, make_listing())
    notifier = FakeRecipientNotifier(failing={"2"})
    service = service_for(database, notifier)

    service.notify_pending()
    assert len(database.pending_notifications()) == 1

    notifier.failing.clear()
    service.notify_pending()
    assert len(notifier.to("1")) == 1
    assert len(notifier.to("2")) == 1
    assert database.pending_notifications() == []


def test_many_matches_become_one_digest_per_subscriber(database):
    subscribe(database, "1")
    queue(database, *[make_listing(external_id=str(i)) for i in range(5)])
    notifier = FakeRecipientNotifier()

    assert service_for(database, notifier).notify_pending() == 5
    assert len(notifier.to("1")) == 1
    assert "5 new apartments" in notifier.to("1")[0]


def test_with_nobody_subscribed_the_queue_is_cleared(database):
    queue(database, make_listing())
    notifier = FakeRecipientNotifier()

    assert service_for(database, notifier).notify_pending() == 0
    assert notifier.sent == []
    assert database.pending_notifications() == []


# ---------------------------------------------------------------- scheduler


def build_service(database, scraper, notifier):
    config = Config(
        criteria=SearchCriteria(),
        telegram=TelegramConfig(bot_token="t", group_threshold=3),
        sources={"mercadolibre": SourceConfig(name="mercadolibre")},
        notify_on_first_run=True,
    )
    return MonitorService(config, database, scrapers=[scraper], notifier=notifier)


def test_scrapers_are_asked_for_the_combined_search(database):
    subscribe(database, "1", neighborhoods=["Recoleta"], max_rent=1_500_000)
    scraper = FakeScraper("mercadolibre")
    build_service(database, scraper, FakeRecipientNotifier()).run_cycle()
    assert scraper.criteria.max_rent == 1_500_000
    assert scraper.criteria.neighborhoods == ["CABA/Recoleta"]


def test_widening_a_search_does_not_flood_with_old_listings(database):
    subscribe(database, "1", neighborhoods=["Recoleta"])
    scraper = FakeScraper("mercadolibre", [make_listing(external_id="old-R")])
    notifier = FakeRecipientNotifier()
    service = build_service(database, scraper, notifier)
    service.run_cycle()
    assert len(notifier.to("1")) == 1

    # The subscriber adds Barrio Norte: the site now also returns what has
    # been listed there all along, plus one genuinely new Recoleta listing.
    save_criteria(
        database, "1", DEFAULT.with_changes({"neighborhoods": ["Recoleta", "Barrio Norte"]})
    )
    scraper._listings = [
        make_listing(external_id="old-R"),
        make_listing(external_id="old-N", neighborhood="Barrio Norte"),
        make_listing(external_id="new-R"),
    ]
    report = service.run_cycle()

    assert report.suppressed_on_bootstrap == 1
    assert len(notifier.to("1")) == 2  # only new-R was added
    assert database.pending_notifications() == []

    # From now on Barrio Norte is part of the search: its new listings arrive.
    scraper._listings.append(make_listing(external_id="new-N", neighborhood="Barrio Norte"))
    service.run_cycle()
    assert len(notifier.to("1")) == 3


def test_narrowing_a_search_suppresses_nothing(database):
    subscribe(database, "1")
    scraper = FakeScraper("mercadolibre")
    notifier = FakeRecipientNotifier()
    service = build_service(database, scraper, notifier)
    service.run_cycle()

    save_criteria(database, "1", DEFAULT.with_changes({"neighborhoods": ["Recoleta"]}))
    scraper._listings = [make_listing(external_id="new-R")]
    report = service.run_cycle()
    assert report.suppressed_on_bootstrap == 0
    assert len(notifier.to("1")) == 1
