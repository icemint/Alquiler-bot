"""Notification formatting and delivery bookkeeping."""

from datetime import datetime, timezone

import pytest
from conftest import make_listing

from monitor.config import TelegramConfig
from monitor.services.deduplication import Deduplicator
from monitor.services.filtering import FilterDecision
from monitor.services.notification import format_group, format_listing
from monitor.services.notification.formatter import format_area, format_money, source_label
from monitor.services.notification.notifier import NotificationService, Notifier

ACCEPT = FilterDecision(True)
DETECTED_AT = datetime(2026, 9, 18, 22, 5, tzinfo=timezone.utc)


class FakeNotifier(Notifier):
    def __init__(self, succeed=True):
        self.sent = []
        self.succeed = succeed

    def send(self, message):
        self.sent.append(message)
        return self.succeed

    @property
    def messages(self):
        return [message.telegram_html for message in self.sent]


# ---------------------------------------------------------------- formatting


def test_money_and_area_use_argentine_formatting():
    assert format_money(1_000_000, "ARS") == "$ 1.000.000"
    assert format_money(1200, "USD") == "US$ 1.200"
    assert format_money(None) == "-"
    assert format_area(50) == "50 m²"
    assert format_area(38.5) == "38.5 m²"


def test_source_labels_are_human_readable():
    assert source_label("mercadolibre") == "MercadoLibre"
    assert source_label("argenprop") == "ArgenProp"
    assert source_label("zonaprop") == "ZonaProp"


def test_single_listing_message_contains_every_required_field():
    message = format_listing(make_listing(), DETECTED_AT)
    assert "🏠 <b>New apartment</b>" in message
    assert "Source: MercadoLibre" in message
    assert "Type: Departamento" in message
    assert "Neighborhood: Recoleta" in message
    assert "Price: $ 800.000" in message
    assert "Area: 55 m²" in message
    assert "Ambientes: 2" in message
    assert "Expensas: $ 150.000" in message
    assert "Address: Junin 1200" in message
    assert "https://departamento.mercadolibre.com.ar/MLA-1-depto-_JM" in message
    assert "Detected: 18/09/2026" in message


def test_optional_fields_are_omitted_when_absent():
    message = format_listing(make_listing(expensas=None, address=None, rooms=None), DETECTED_AT)
    assert "Expensas:" not in message
    assert "Address:" not in message
    assert "Ambientes:" not in message


def test_html_special_characters_are_escaped():
    message = format_listing(make_listing(address="Uriburu 1 & 2 <b>"), DETECTED_AT)
    assert "Uriburu 1 &amp; 2 &lt;b&gt;" in message


def test_group_message_lists_every_listing(database):
    Deduplicator(database).process(
        [
            (make_listing(external_id="A"), ACCEPT),
            (make_listing(external_id="B", neighborhood="Barrio Norte"), ACCEPT),
        ]
    )
    message = format_group(database.pending_notifications(), DETECTED_AT)
    assert "🏠 <b>2 new apartments</b>" in message
    assert "Recoleta" in message and "Barrio Norte" in message
    assert message.count("mercadolibre.com.ar") == 2


# ---------------------------------------------------------------- delivery


@pytest.fixture
def telegram_config():
    return TelegramConfig(bot_token="t", chat_id="1", group_threshold=3)


def test_each_new_listing_is_sent_once(database, telegram_config):
    Deduplicator(database).process([(make_listing(external_id=str(i)), ACCEPT) for i in range(2)])
    notifier = FakeNotifier()
    service = NotificationService(database, notifier, telegram_config)

    assert service.notify_pending() == 2
    assert len(notifier.messages) == 2
    # Nothing is pending afterwards, so a second cycle sends nothing.
    assert service.notify_pending() == 0
    assert len(notifier.messages) == 2


def test_many_listings_are_grouped_into_one_message(database, telegram_config):
    Deduplicator(database).process([(make_listing(external_id=str(i)), ACCEPT) for i in range(5)])
    notifier = FakeNotifier()
    service = NotificationService(database, notifier, telegram_config)

    assert service.notify_pending() == 5
    assert len(notifier.messages) == 1
    assert "5 new apartments" in notifier.messages[0]


def test_failed_delivery_keeps_the_listing_pending(database, telegram_config):
    Deduplicator(database).process([(make_listing(), ACCEPT)])
    service = NotificationService(database, FakeNotifier(succeed=False), telegram_config)

    assert service.notify_pending() == 0
    assert len(database.pending_notifications()) == 1  # will be retried


def test_delivery_is_not_retried_forever(database, telegram_config):
    Deduplicator(database).process([(make_listing(), ACCEPT)])
    service = NotificationService(database, FakeNotifier(succeed=False), telegram_config)
    for _ in range(6):
        service.notify_pending()
    assert database.pending_notifications() == []


def test_nothing_is_sent_when_there_is_nothing_new(database, telegram_config):
    notifier = FakeNotifier()
    assert NotificationService(database, notifier, telegram_config).notify_pending() == 0
    assert notifier.messages == []


def test_a_long_digest_is_split_instead_of_truncated(database, telegram_config):
    from monitor.services.notification.notifier import MAX_MESSAGE_LENGTH

    Deduplicator(database).process(
        [
            (
                make_listing(
                    external_id=str(i),
                    url=f"https://departamento.mercadolibre.com.ar/MLA-{i}-un-titulo-bastante-largo-_JM",
                    address=f"Una Direccion Bastante Larga Al {i}00",
                ),
                ACCEPT,
            )
            for i in range(40)
        ]
    )
    notifier = FakeNotifier()
    config = TelegramConfig(
        bot_token="t", chat_id="1", group_threshold=3, max_notifications_per_cycle=40
    )
    sent = NotificationService(database, notifier, config).notify_pending()

    assert sent == 40
    assert len(notifier.messages) > 1
    assert all(len(message) <= MAX_MESSAGE_LENGTH for message in notifier.messages)
    assert "truncated" not in " ".join(notifier.messages)
    assert database.pending_notifications() == []


def test_a_failed_chunk_leaves_the_rest_pending(database):
    class FailAfterFirst(Notifier):
        def __init__(self):
            self.messages = []

        def send(self, message):
            self.messages.append(message)
            return len(self.messages) == 1

    Deduplicator(database).process(
        [
            (
                make_listing(
                    external_id=str(i),
                    url=f"https://departamento.mercadolibre.com.ar/MLA-{i}-un-titulo-bastante-largo-_JM",
                    address=f"Una Direccion Bastante Larga Al {i}00",
                ),
                ACCEPT,
            )
            for i in range(40)
        ]
    )
    config = TelegramConfig(
        bot_token="t", chat_id="1", group_threshold=3, max_notifications_per_cycle=40
    )
    sent = NotificationService(database, FailAfterFirst(), config).notify_pending()

    assert 0 < sent < 40
    assert len(database.pending_notifications()) == 40 - sent


# ---------------------------------------------------------------- telegram fan-out


class FakeResponse:
    def __init__(self, status_code, data):
        self.status_code = status_code
        self._data = data

    def json(self):
        return self._data


class FakeSession:
    """Answers sendMessage per chat id; unknown chats succeed."""

    def __init__(self, responses=None):
        self.responses = responses or {}
        self.chats = []

    def post(self, url, json, timeout):
        self.chats.append(json["chat_id"])
        return self.responses.get(json["chat_id"], FakeResponse(200, {"ok": True}))


def telegram_notifier(database, session):
    from monitor.services.notification.notifier import TelegramNotifier

    return TelegramNotifier("t", database, session=session)


def single_message():
    from monitor.services.notification.formatter import build_listing_message

    return build_listing_message(make_listing(), DETECTED_AT)


def test_telegram_sends_to_every_active_subscriber(database):
    for chat_id in ("1", "2", "3"):
        database.add_subscriber(chat_id)
    database.remove_subscriber("3")
    session = FakeSession()

    assert telegram_notifier(database, session).send(single_message()) is True
    assert sorted(session.chats) == ["1", "2"]


def test_a_blocked_bot_removes_the_subscriber(database):
    database.add_subscriber("1")
    database.add_subscriber("2")
    blocked = FakeResponse(
        403, {"ok": False, "description": "Forbidden: bot was blocked by the user"}
    )
    session = FakeSession({"2": blocked})

    assert telegram_notifier(database, session).send(single_message()) is True
    assert database.active_subscribers() == ["1"]


def test_delivery_fails_when_no_subscriber_received_it(database):
    database.add_subscriber("1")
    session = FakeSession({"1": FakeResponse(500, {"ok": False, "description": "boom"})})
    assert telegram_notifier(database, session).send(single_message()) is False


def test_no_subscribers_does_not_block_the_queue(database):
    session = FakeSession()
    assert telegram_notifier(database, session).send(single_message()) is True
    assert session.chats == []
