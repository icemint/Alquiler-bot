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
        self.messages = []
        self.succeed = succeed

    def send(self, text):
        self.messages.append(text)
        return self.succeed


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

        def send(self, text):
            self.messages.append(text)
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
