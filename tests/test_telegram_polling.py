"""The Telegram long-polling loop and the menu branches the reply tests skip.

Telegram is replaced by a fake session that records every API call and
answers getUpdates from a script.
"""

import requests
from test_telegram_bot import labels

from monitor.telegram.bot import OFFSET_KEY


class FakeTelegram:
    """Answers bot API calls; ``updates`` is a list of getUpdates batches."""

    def __init__(self, bot, updates=()):
        self.bot = bot
        self.updates = list(updates)
        self.calls = []

    def post(self, url, json=None, timeout=None):
        method = url.rsplit("/", 1)[1]
        self.calls.append((method, json))
        if method == "getUpdates":
            if not self.updates:
                self.bot.stop()
                return Answer({"ok": True, "result": []})
            batch = self.updates.pop(0)
            if isinstance(batch, Exception):
                raise batch
            return Answer({"ok": True, "result": batch})
        return Answer({"ok": True, "result": True})

    def sent(self, method):
        return [payload for name, payload in self.calls if name == method]


class Answer:
    def __init__(self, data):
        self.data = data

    def json(self):
        if isinstance(self.data, Exception):
            raise self.data
        return self.data


def message(text, chat_id=7, update_id=1):
    return {"update_id": update_id, "message": {"text": text, "chat": {"id": chat_id}}}


def callback(data, chat_id=7, update_id=1):
    return {
        "update_id": update_id,
        "callback_query": {
            "id": "cb",
            "data": data,
            "message": {"message_id": 99, "chat": {"id": chat_id}},
        },
    }


def run(bot, *batches):
    telegram = FakeTelegram(bot, batches)
    bot.session = telegram
    bot.run()
    return telegram


# ---------------------------------------------------------------- loop


def test_the_bot_registers_its_commands_and_answers_one(bot, database):
    telegram = run(bot, [message("/start", update_id=5)])
    commands = telegram.sent("setMyCommands")[0]["commands"]
    assert [c["command"] for c in commands][:2] == ["start", "settings"]
    [sent] = telegram.sent("sendMessage")
    assert sent["chat_id"] == "7" and "You are subscribed" in sent["text"]
    assert sent["parse_mode"] == "HTML"
    assert database.get_state(OFFSET_KEY) == "6"


def test_polling_resumes_after_the_saved_offset(bot, database):
    database.set_state(OFFSET_KEY, "41")
    telegram = run(bot)
    assert telegram.sent("getUpdates")[0]["offset"] == 41


def test_settings_comes_with_its_keyboard(bot):
    telegram = run(bot, [message("/settings")])
    [sent] = telegram.sent("sendMessage")
    assert sent["reply_markup"]["inline_keyboard"]


def test_plain_text_is_answered(bot):
    telegram = run(bot, [message("hola")])
    assert "/settings" in telegram.sent("sendMessage")[0]["text"]


def test_a_button_edits_the_menu_and_shows_its_toast(bot):
    city = bot.cities.index("CABA")
    telegram = run(bot, [callback(f"c:{city}")])
    [answer] = telegram.sent("answerCallbackQuery")
    assert answer == {"callback_query_id": "cb", "text": "Keep at least one city"}
    [edit] = telegram.sent("editMessageText")
    assert edit["message_id"] == 99 and edit["chat_id"] == "7"
    assert edit["reply_markup"]["inline_keyboard"]


def test_a_button_without_a_toast_answers_silently(bot):
    telegram = run(bot, [callback("s:types")])
    assert telegram.sent("answerCallbackQuery") == [{"callback_query_id": "cb"}]


def test_updates_without_a_chat_or_text_are_ignored(bot):
    empty_callback = {"update_id": 1, "callback_query": {"id": "cb", "data": "s:menu"}}
    telegram = run(bot, [empty_callback, message("   "), {"update_id": 2}])
    assert telegram.sent("sendMessage") == []
    assert telegram.sent("answerCallbackQuery") == []


def test_one_bad_update_does_not_stop_the_bot(bot, monkeypatch):
    real = bot.handle_text

    def explode_once(chat_id, text):
        if text == "boom":
            raise RuntimeError("bad update")
        return real(chat_id, text)

    monkeypatch.setattr(bot, "handle_text", explode_once)
    telegram = run(bot, [message("boom", update_id=1), message("hola", update_id=2)])
    assert len(telegram.sent("sendMessage")) == 1


def test_a_polling_error_is_survived(bot, monkeypatch):
    calls = []
    monkeypatch.setattr(bot._stop, "wait", lambda seconds: calls.append(seconds))

    def updates():
        calls.append("poll")
        if len(calls) == 1:
            raise RuntimeError("network down")
        bot.stop()
        return []

    monkeypatch.setattr(bot, "_updates", updates)
    bot.session = FakeTelegram(bot)
    bot.run()
    assert calls == ["poll", 10, "poll", 1]


def test_api_failures_are_logged_not_raised(bot, caplog):
    class Failing:
        def __init__(self, outcome):
            self.outcome = outcome

        def post(self, *args, **kwargs):
            if isinstance(self.outcome, Exception):
                raise self.outcome
            return Answer(self.outcome)

    for outcome in (
        requests.ConnectionError("down"),
        ValueError("not JSON"),
        {"ok": False, "description": "Bad Request: chat not found"},
    ):
        bot.session = Failing(outcome)
        assert bot._call("sendMessage", {}) is None
    assert [record.getMessage() for record in caplog.records] == [
        "telegram poll failed",
        "telegram poll failed",
        "telegram API error",
    ]


def test_without_a_token_the_bot_does_not_poll(bot):
    bot.config.telegram.bot_token = None
    bot.session = None  # any call would fail
    bot.run()


# ---------------------------------------------------------------- commands and menus


def test_help_repeats_the_welcome(bot):
    assert "CABA rental monitor" in bot.handle_command("/help")


def test_every_settings_screen_opens(bot):
    assert "Location" in bot.handle_callback("7", "s:loc").text
    assert "Bedrooms" in bot.handle_callback("7", "s:beds").text
    assert "Furnished" in bot.handle_callback("7", "s:furn").text
    assert "Your search" in bot.handle_callback("7", "s:unknown").text


def test_done_closes_the_menu(bot):
    reply = bot.handle_callback("7", "s:done")
    assert reply.toast == "Saved"
    assert reply.keyboard is None
    assert "/settings to change it." in reply.text


def test_a_chosen_bedroom_count_can_be_unticked(bot):
    bot.handle_callback("7", "b:1")
    bot.handle_callback("7", "b:2")
    bot.handle_callback("7", "b:1")
    assert bot._criteria("7").bedrooms == [2]


def test_area_preset_button(bot):
    assert "Min area updated" in bot.handle_callback("7", "a:70").text
    assert bot._criteria("7").min_area_m2 == 70


def test_the_last_property_type_cannot_be_removed(bot):
    bot.handle_callback("7", "t:Casa")
    bot.handle_callback("7", "t:PH")
    reply = bot.handle_callback("7", "t:Departamento")
    assert reply.toast == "Keep at least one type"


def test_malformed_buttons_fall_back_to_a_menu(bot):
    for data in ("x:1", "r:lots", "b:9", "f:maybe", "c:999", "bt:0:9999:0", "bt:0"):
        reply = bot.handle_callback("7", data)
        assert reply.text, data
    assert bot._criteria("7") == bot._criteria("other chat")


def test_a_search_for_an_unknown_city_is_dropped(bot, database):
    database.set_state("awaiting_input:7", "search:999")
    assert "/settings" in bot.handle_text("7", "palermo").text
    assert "/settings" in bot.handle_text("7", "palermo").text


def test_a_search_with_no_match_says_so(bot):
    bot.handle_callback("7", "bs:{0}".format(bot.cities.index("CABA")))
    reply = bot.handle_text("7", "zzzz")
    assert "Nothing in CABA matches" in reply.text
    assert labels(reply) == ["⬅️ All barrios"]
