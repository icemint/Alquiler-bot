"""Deduplication and restart safety."""

from conftest import make_listing

from monitor.services.deduplication import Deduplicator
from monitor.services.filtering import FilterDecision, RejectReason

ACCEPT = FilterDecision(True)
REJECT = FilterDecision(False, RejectReason.PRICE_ABOVE_MAX)


def test_dedupe_key_prefers_the_site_listing_id():
    assert make_listing(external_id="MLA42").dedupe_key == "mercadolibre:MLA42"


def test_dedupe_key_falls_back_to_the_canonical_url():
    without_id = make_listing(external_id=None, url="https://www.example.com/depto/1?utm_source=x")
    same_listing = make_listing(external_id=None, url="https://example.com/depto/1/")
    assert without_id.dedupe_key == same_listing.dedupe_key
    assert without_id.dedupe_key.startswith("mercadolibre:url:")


def test_first_sighting_is_new_and_second_is_not(database):
    dedupe = Deduplicator(database)
    first = dedupe.process([(make_listing(), ACCEPT)])
    assert first.new_count == 1 and first.known == 0

    second = dedupe.process([(make_listing(), ACCEPT)])
    assert second.new_count == 0 and second.known == 1


def test_duplicates_within_one_cycle_are_collapsed(database):
    dedupe = Deduplicator(database)
    outcome = dedupe.process([(make_listing(), ACCEPT), (make_listing(), ACCEPT)])
    assert outcome.new_count == 1
    assert outcome.duplicates_in_cycle == 1


def test_non_matching_listings_are_stored_but_not_new(database):
    dedupe = Deduplicator(database)
    outcome = dedupe.process([(make_listing(price=2_000_000), REJECT)])
    assert outcome.new_count == 0
    assert outcome.matched == 0
    assert database.count_listings() == 1


def test_listing_that_becomes_eligible_is_queued_but_not_counted_as_new(database):
    dedupe = Deduplicator(database)
    dedupe.process([(make_listing(price=2_000_000), REJECT)])
    outcome = dedupe.process([(make_listing(price=900_000), ACCEPT)])
    assert outcome.new_count == 0
    assert outcome.reactivated == 1
    assert [item.dedupe_key for item in database.pending_notifications()] == ["mercadolibre:MLA1"]


def test_already_notified_listings_never_return_to_the_queue(database):
    dedupe = Deduplicator(database)
    dedupe.process([(make_listing(), ACCEPT)])
    database.mark_notified(["mercadolibre:MLA1"])
    dedupe.process([(make_listing(), ACCEPT)])
    assert database.pending_notifications() == []


def test_state_survives_a_restart(tmp_path):
    from monitor.database import Database

    path = str(tmp_path / "restart.sqlite3")
    first = Database(path)
    Deduplicator(first).process([(make_listing(), ACCEPT)])
    first.mark_notified(["mercadolibre:MLA1"])
    first.close()

    # A brand new process opening the same file must not resend anything.
    second = Database(path)
    outcome = Deduplicator(second).process([(make_listing(), ACCEPT)])
    assert outcome.new_count == 0
    assert second.pending_notifications() == []
    second.close()


def test_listings_from_different_sources_are_not_confused(database):
    dedupe = Deduplicator(database)
    outcome = dedupe.process(
        [
            (make_listing(source="mercadolibre", external_id="1"), ACCEPT),
            (make_listing(source="argenprop", external_id="1"), ACCEPT),
        ]
    )
    assert outcome.new_count == 2


def test_last_seen_is_refreshed_on_every_sighting(database):
    from datetime import timedelta

    from monitor.timeutil import utcnow

    listing = make_listing()
    database.upsert_listing(listing, matched=True, seen_at=utcnow() - timedelta(hours=2))
    _, stored = database.upsert_listing(listing, matched=True)
    assert stored.last_seen_at > stored.first_seen_at
