"""Bedrooms and furnished: parsing, storage, filtering, settings."""

import sqlite3

import pytest
from conftest import make_listing

from monitor.config import Config, SearchCriteria, SourceConfig, TelegramConfig
from monitor.database import Database
from monitor.neighborhoods import NeighborhoodResolver
from monitor.scrapers.argenprop.parser import parse_search_page as parse_argenprop
from monitor.scrapers.cabaprop.parser import item_to_listing
from monitor.scrapers.mercadolibre.parser import parse_search_page as parse_mercadolibre
from monitor.scrapers.parsing import bedrooms_from_rooms, parse_furnished
from monitor.services.filtering import ListingFilter
from monitor.services.notification import format_listing
from monitor.telegram import TelegramCommandBot

# ---------------------------------------------------------------- parsing


@pytest.mark.parametrize(
    "text, expected",
    [
        ("Amoblado. Alquiler: $ 680.000", True),
        ("Depto AMUEBLADO con vista", True),
        ("Semi amoblado, luminoso", True),
        ("Se entrega sin muebles", False),
        ("No amoblado", False),
        ("Ideal para amoblar a gusto", None),
        ("Departamento luminoso", None),
        (None, None),
    ],
)
def test_furnished_is_read_only_from_what_the_ad_says(text, expected):
    assert parse_furnished(text) is expected


def test_bedrooms_fallback_from_ambientes():
    assert bedrooms_from_rooms(1) == 0
    assert bedrooms_from_rooms(3) == 2
    assert bedrooms_from_rooms(None) is None


def test_argenprop_uses_the_dormitorios_attribute_and_the_card_text(fixture_loader):
    listings = parse_argenprop(fixture_loader("argenprop_search.html"), NeighborhoodResolver())
    assert {listing.bedrooms for listing in listings} == {1}
    assert [listing.furnished for listing in listings].count(True) == 1


def test_mercadolibre_derives_bedrooms_and_reads_the_title(fixture_loader):
    listings = parse_mercadolibre(
        fixture_loader("mercadolibre_search.html"), NeighborhoodResolver()
    )
    assert {(listing.rooms, listing.bedrooms) for listing in listings} == {(1, 0), (2, 1)}
    furnished = [listing for listing in listings if listing.furnished]
    assert furnished and all("amoblado" in listing.title.lower() for listing in furnished)


def cabaprop_item(**overrides):
    item = {
        "_id": "x1",
        "title": "Departamento en Recoleta",
        "operation_type": 2,
        "property_type": 1,
        "location": {"barrios": [88]},
        "price": {"currency": 2, "total": 700000},
        "surface": {"totalSurface": 40},
        "characteristics": {"ambience": 1, "bedrooms": 0},
    }
    item.update(overrides)
    return item


def test_cabaprop_monoambiente_has_zero_bedrooms():
    assert item_to_listing(cabaprop_item(), NeighborhoodResolver()).bedrooms == 0


def test_cabaprop_structured_furnished_wins_over_the_text():
    item = cabaprop_item(
        title="Amoblado en Recoleta",
        publication={"attributes": [{"id": "FURNISHED", "value_name": "No"}]},
    )
    assert item_to_listing(item, NeighborhoodResolver()).furnished is False


def test_cabaprop_falls_back_to_the_description():
    item = cabaprop_item(description="Se alquila totalmente amoblado.")
    assert item_to_listing(item, NeighborhoodResolver()).furnished is True


# ---------------------------------------------------------------- storage


def test_both_fields_survive_the_database(database):
    database.upsert_listing(make_listing(bedrooms=2, furnished=True), matched=True)
    stored = database.pending_notifications()[0].listing
    assert (stored.bedrooms, stored.furnished) == (2, True)


def test_an_existing_database_gains_the_new_columns(tmp_path):
    path = str(tmp_path / "old.sqlite3")
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE listings (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "dedupe_key TEXT NOT NULL UNIQUE, source TEXT NOT NULL, external_id TEXT NOT NULL, "
        "url TEXT NOT NULL, title TEXT, property_type TEXT, operation TEXT, neighborhood TEXT, "
        "raw_neighborhood TEXT, address TEXT, price REAL, currency TEXT, area_m2 REAL, "
        "rooms INTEGER, expensas REAL, expensas_currency TEXT, published_at TEXT, "
        "price_is_approximate INTEGER NOT NULL DEFAULT 0, matched INTEGER NOT NULL DEFAULT 0, "
        "reject_reason TEXT, first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL, "
        "notified_at TEXT, notify_attempts INTEGER NOT NULL DEFAULT 0, extra TEXT)"
    )
    connection.commit()
    connection.close()

    database = Database(path)
    database.upsert_listing(make_listing(bedrooms=1), matched=True)
    assert database.pending_notifications()[0].listing.bedrooms == 1
    database.close()


# ---------------------------------------------------------------- filtering


def accepted(listing, **criteria):
    return ListingFilter(SearchCriteria(**criteria)).evaluate(listing).accepted


def test_no_bedroom_filter_accepts_unknown_counts():
    assert accepted(make_listing(bedrooms=None))


def test_an_unknown_bedroom_count_is_left_to_the_subscriber():
    assert accepted(make_listing(bedrooms=None), bedrooms=[1, 2])
    assert accepted(make_listing(bedrooms=2), bedrooms=[1, 2])
    assert not accepted(make_listing(bedrooms=3), bedrooms=[1, 2])


def test_the_last_bedroom_option_means_that_many_or_more():
    assert accepted(make_listing(bedrooms=6), bedrooms=[4])
    assert accepted(make_listing(bedrooms=0), bedrooms=[0])


def test_furnished_only_drops_ads_that_say_unfurnished():
    assert accepted(make_listing(furnished=True), furnished="yes")
    assert accepted(make_listing(furnished=None), furnished="yes")
    assert not accepted(make_listing(furnished=False), furnished="yes")


def test_unfurnished_only_drops_ads_that_say_furnished():
    assert accepted(make_listing(furnished=None), furnished="no")
    assert accepted(make_listing(furnished=False), furnished="no")
    assert not accepted(make_listing(furnished=True), furnished="no")


# ---------------------------------------------------------------- criteria


def test_an_empty_bedroom_list_is_a_choice_not_a_missing_value():
    criteria = SearchCriteria(bedrooms=[2]).with_changes({"bedrooms": []})
    assert criteria.bedrooms == []
    assert SearchCriteria(bedrooms=[2]).with_changes({}).bedrooms == [2]


def test_invalid_stored_values_are_ignored():
    criteria = SearchCriteria().with_changes({"bedrooms": [1, "x", 9], "furnished": "maybe"})
    assert criteria.bedrooms == [1]
    assert criteria.furnished == "any"


def test_the_union_is_unfiltered_when_any_search_is():
    assert SearchCriteria.union([SearchCriteria(bedrooms=[1]), SearchCriteria()]).bedrooms == []
    union = SearchCriteria.union([SearchCriteria(bedrooms=[1]), SearchCriteria(bedrooms=[3])])
    assert union.bedrooms == [1, 3]
    furnished = [SearchCriteria(furnished="yes"), SearchCriteria(furnished="no")]
    assert SearchCriteria.union(furnished).furnished == "any"
    assert SearchCriteria.union([SearchCriteria(furnished="yes")] * 2).furnished == "yes"


# ---------------------------------------------------------------- message


def test_the_message_shows_bedrooms_and_furnished_when_known():
    message = format_listing(make_listing(bedrooms=0, furnished=True))
    assert "Bedrooms: 0" in message
    assert "Furnished: yes" in message
    assert "Furnished" not in format_listing(make_listing())


# ---------------------------------------------------------------- settings


@pytest.fixture
def bot(database):
    config = Config(
        criteria=SearchCriteria(),
        telegram=TelegramConfig(bot_token="t"),
        sources={"cabaprop": SourceConfig(name="cabaprop")},
    )
    return TelegramCommandBot(config, database)


def labels(reply):
    return [button["text"] for row in reply.keyboard for button in row]


def test_bedroom_buttons_toggle_and_any_clears(bot):
    reply = bot.handle_callback("7", "s:beds")
    assert "✅ Any" in labels(reply)

    bot.handle_callback("7", "b:1")
    reply = bot.handle_callback("7", "b:4")
    assert {"✅ 1", "✅ 4+", "▫️ Any"} <= set(labels(reply))
    assert bot._criteria("7").bedrooms == [1, 4]

    bot.handle_callback("7", "b:any")
    assert bot._criteria("7").bedrooms == []


def test_furnished_choice_is_saved(bot):
    reply = bot.handle_callback("7", "f:yes")
    assert "Furnished updated" in reply.text
    assert "Furnished: furnished only" in reply.text
    assert bot._criteria("7").furnished == "yes"


def test_settings_lists_the_new_filters(bot):
    reply = bot.reply_to_command("/settings", chat_id="7")
    assert "Bedrooms: any" in reply.text
    data = [button["callback_data"] for row in reply.keyboard for button in row]
    assert {"s:beds", "s:furn"} <= set(data)
