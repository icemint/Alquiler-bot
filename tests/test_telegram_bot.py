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
    assert {"s:loc", "s:types", "s:rent", "s:area", "s:done"} <= set(data)


def labels(reply):
    return [button["text"] for row in reply.keyboard for button in row]


def barrio_button(bot, city, barrio, back="0"):
    city_index = bot.cities.index(city)
    barrio_index = bot.resolver.neighborhoods(city).index(barrio)
    return f"bt:{city_index}:{barrio_index}:{back}"


def test_location_lists_cities_and_their_barrio_choice(bot):
    reply = bot.handle_callback("7", "s:loc")
    assert {"✅ CABA", "▫️ AMBA", "📍 CABA: 2 barrios ›"} <= set(labels(reply))


def test_toggling_a_barrio_saves_it_for_that_chat_only(bot):
    reply = bot.handle_callback("7", barrio_button(bot, "CABA", "Barrio Norte"))
    assert "▫️ Barrio Norte" in labels(reply)
    assert bot._criteria("7").neighborhoods == ["CABA/Recoleta"]
    assert bot._criteria("8").neighborhoods == ["CABA/Recoleta", "CABA/Barrio Norte"]


def test_removing_the_last_barrio_searches_the_whole_city(bot):
    bot.handle_callback("7", barrio_button(bot, "CABA", "Barrio Norte"))
    reply = bot.handle_callback("7", barrio_button(bot, "CABA", "Recoleta"))
    assert "all of CABA" in reply.toast
    assert bot._criteria("7").neighborhoods == []
    assert bot._criteria("7").cities == ["CABA"]
    assert "✅ All of CABA" in labels(reply)


def test_all_of_a_city_clears_its_barrios(bot):
    bot.handle_callback("7", "ba:{0}".format(bot.cities.index("CABA")))
    assert bot._criteria("7").barrios_in("CABA") == []


def test_adding_a_city_searches_it_whole(bot):
    bot.handle_callback("7", "c:{0}".format(bot.cities.index("AMBA")))
    criteria = bot._criteria("7")
    assert criteria.cities == ["CABA", "AMBA"]
    assert criteria.barrios_in("AMBA") == []
    assert "AMBA: all barrios" in criteria.describe_location()


def test_removing_a_city_drops_its_barrios_but_not_the_last_city(bot):
    bot.handle_callback("7", "c:{0}".format(bot.cities.index("AMBA")))
    bot.handle_callback("7", barrio_button(bot, "AMBA", "Tigre"))
    bot.handle_callback("7", "c:{0}".format(bot.cities.index("AMBA")))
    assert bot._criteria("7").cities == ["CABA"]
    assert all(key.startswith("CABA/") for key in bot._criteria("7").neighborhoods)

    reply = bot.handle_callback("7", "c:{0}".format(bot.cities.index("CABA")))
    assert reply.toast == "Keep at least one city"


def test_barrio_list_is_paged(bot):
    city = bot.cities.index("CABA")
    first = bot.handle_callback("7", f"bl:{city}:0")
    second = bot.handle_callback("7", f"bl:{city}:1")
    assert "page 1/" in first.text and "page 2/" in second.text
    assert "▶️" in labels(first) and "◀️" in labels(second)
    assert set(labels(first)) != set(labels(second))


def test_typed_search_finds_barrios_and_toggles_from_the_results(bot):
    city = bot.cities.index("CABA")
    bot.handle_callback("7", f"bs:{city}")
    reply = bot.handle_text("7", "villa c")
    assert labels(reply)[:1] == ["▫️ Villa Crespo"]

    reply = bot.handle_callback("7", barrio_button(bot, "CABA", "Villa Crespo", "s"))
    assert "✅ Villa Crespo" in labels(reply)
    assert "CABA/Villa Crespo" in bot._criteria("7").neighborhoods
    # Still in search mode: another name searches again.
    assert "Palermo" in " ".join(labels(bot.handle_text("7", "paler")))


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
