"""Telegram command replies."""

import pytest

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


def test_status_reports_last_check_and_filters_only(bot, database):
    database.update_source_state("mercadolibre", "ok", None, found=10, new_listings=1)
    cycle_id = database.start_cycle()
    database.finish_cycle(cycle_id, 2, 1, 10, 2, 1, 1)

    message = bot.handle_command("/status")
    assert "Last successful check:" in message
    assert "never" not in message
    assert "Sources working" not in message
    assert "Listings stored" not in message
    assert "expensas excluded" in message


def test_status_before_the_first_cycle(bot):
    assert "never" in bot.handle_command("/status")


def test_unknown_command_is_answered_politely(bot):
    assert "Unknown command" in bot.handle_command("/nonsense")


def test_command_suffix_with_bot_name_is_accepted(bot):
    assert "Status" in bot.handle_command("/status@my_rental_bot")


# ---------------------------------------------------------------- subscriptions


def test_start_subscribes_any_chat(bot, database):
    message = bot.handle_command("/start", chat_id="777", username="someone")
    assert "You are subscribed" in message
    assert database.active_subscribers() == ["777"]


def test_repeated_start_does_not_duplicate_the_subscriber(bot, database):
    bot.handle_command("/start", chat_id="777")
    bot.handle_command("/start", chat_id="777")
    assert database.count_subscribers() == 1


def test_stop_unsubscribes(bot, database):
    bot.handle_command("/start", chat_id="777")
    assert "Unsubscribed" in bot.handle_command("/stop", chat_id="777")
    assert database.active_subscribers() == []
    assert "not subscribed" in bot.handle_command("/stop", chat_id="777")


def test_start_after_stop_subscribes_again(bot, database):
    bot.handle_command("/start", chat_id="777")
    bot.handle_command("/stop", chat_id="777")
    bot.handle_command("/start", chat_id="777")
    assert database.active_subscribers() == ["777"]


def test_owner_seeding_respects_a_previous_stop(database):
    database.add_subscriber("42", reactivate=False)
    database.remove_subscriber("42")
    assert database.add_subscriber("42", reactivate=False) is False
    assert database.active_subscribers() == []


def test_settings_shows_the_chat_search_with_buttons(bot):
    reply = bot.reply_to_command("/settings", chat_id="7")
    assert "Your search" in reply.text
    assert "Recoleta, Barrio Norte" in reply.text
    data = [button["callback_data"] for row in reply.keyboard for button in row]
    assert {"s:hoods", "s:types", "s:rent", "s:area", "s:done"} <= set(data)


def test_toggling_a_neighborhood_saves_it_for_that_chat_only(bot):
    reply = bot.handle_callback("7", "s:hoods")
    labels = [button["text"] for row in reply.keyboard for button in row]
    assert "✅ Recoleta" in labels

    index = bot.neighborhoods.index("Barrio Norte")
    reply = bot.handle_callback("7", f"h:{index}")
    assert "▫️ Barrio Norte" in [button["text"] for row in reply.keyboard for button in row]
    assert bot._criteria("7").neighborhoods == ["Recoleta"]
    assert bot._criteria("8").neighborhoods == ["Recoleta", "Barrio Norte"]


def test_the_last_neighborhood_cannot_be_removed(bot):
    bot.handle_callback("7", "h:{0}".format(bot.neighborhoods.index("Barrio Norte")))
    reply = bot.handle_callback("7", "h:{0}".format(bot.neighborhoods.index("Recoleta")))
    assert reply.toast
    assert bot._criteria("7").neighborhoods == ["Recoleta"]


def test_toggling_property_types(bot):
    bot.handle_callback("7", "t:Casa")
    assert [t.value for t in bot._criteria("7").property_types] == ["Departamento", "PH"]
    bot.handle_callback("7", "t:Casa")
    assert "Casa" in [t.value for t in bot._criteria("7").property_types]


def test_rent_preset_button(bot):
    reply = bot.handle_callback("7", "r:1500000")
    assert "Max rent updated" in reply.text
    assert bot._criteria("7").max_rent == 1_500_000


def test_typed_rent_after_choosing_max_rent(bot):
    bot.handle_callback("7", "s:rent")
    reply = bot.handle_text("7", "1.250.000 ARS")
    assert "1.250.000" in reply.text
    assert bot._criteria("7").max_rent == 1_250_000
    # The prompt is answered: further text is not taken as a price.
    assert "/settings" in bot.handle_text("7", "900000").text
    assert bot._criteria("7").max_rent == 1_250_000


def test_typed_area_out_of_range_is_refused(bot):
    bot.handle_callback("7", "s:area")
    assert "between" in bot.handle_text("7", "5").text
    assert bot._criteria("7").min_area_m2 == 50
    bot.handle_text("7", "65 m2")
    assert bot._criteria("7").min_area_m2 == 65


def test_leaving_the_prompt_cancels_typed_input(bot):
    bot.handle_callback("7", "s:rent")
    bot.handle_callback("7", "s:menu")
    assert "/settings" in bot.handle_text("7", "900000").text
    assert bot._criteria("7").max_rent == 1_000_000


def test_start_describes_the_chat_own_search(bot):
    bot.handle_callback("7", "r:750000")
    assert "750.000 ARS" in bot.handle_command("/start", chat_id="7")


def test_stats_is_not_a_command_any_more(bot):
    assert "Unknown command" in bot.handle_command("/stats")
