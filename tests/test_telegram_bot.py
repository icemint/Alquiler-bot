"""Telegram command replies."""

import pytest
from conftest import make_listing

from monitor.config import Config, SearchCriteria, SourceConfig, TelegramConfig
from monitor.telegram import TelegramCommandBot


@pytest.fixture
def bot(database):
    config = Config(
        criteria=SearchCriteria(),
        telegram=TelegramConfig(bot_token="t", chat_id="42"),
        sources={
            "mercadolibre": SourceConfig(name="mercadolibre"),
            "argenprop": SourceConfig(name="argenprop"),
        },
    )
    return TelegramCommandBot(config, database)


def test_start_describes_the_current_search(bot):
    message = bot.handle_command("/start")
    assert "CABA rental monitor" in message
    assert "Recoleta, Barrio Norte" in message
    assert "1.000.000 ARS" in message
    assert "50 m²" in message
    assert "5 minutes" in message


def test_status_reports_last_check_sources_and_filters(bot, database):
    database.update_source_state("mercadolibre", "ok", None, found=10, new_listings=1)
    cycle_id = database.start_cycle()
    database.finish_cycle(cycle_id, 2, 1, 10, 2, 1, 1)

    message = bot.handle_command("/status")
    assert "Last successful check:" in message
    assert "never" not in message
    assert "Sources working: 1/2" in message
    assert "Listings stored: 0" in message
    assert "expensas excluded" in message


def test_status_before_the_first_cycle(bot):
    assert "never" in bot.handle_command("/status")


def test_stats_counts_listings_and_source_health(bot, database):
    database.upsert_listing(make_listing(external_id="1"), matched=True)
    database.upsert_listing(make_listing(external_id="2", source="argenprop"), matched=False)
    database.update_source_state("mercadolibre", "ok", None, found=2, new_listings=1)
    database.update_source_state("argenprop", "blocked", "WAF throttling", found=0, new_listings=0)

    message = bot.handle_command("/stats")
    assert "Listings monitored: 2" in message
    assert "MercadoLibre: OK" in message
    assert "ArgenProp: blocked" in message


def test_unknown_command_is_answered_politely(bot):
    assert "Unknown command" in bot.handle_command("/nonsense")


def test_command_suffix_with_bot_name_is_accepted(bot):
    assert "Status" in bot.handle_command("/status@my_rental_bot")
