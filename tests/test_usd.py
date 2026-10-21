"""Listings priced in dollars, held against a budget in pesos."""

import pytest
import requests
from conftest import make_listing
from test_scheduler import FakeScraper
from test_subscriptions import FakeRecipientNotifier

from monitor.config import Config, SearchCriteria, SourceConfig, TelegramConfig, load_config
from monitor.scheduler import MonitorService
from monitor.services import exchange
from monitor.services.exchange import UsdRate
from monitor.services.filtering import ListingFilter, RejectReason
from monitor.services.notification.formatter import build_listing_message, format_price

RATE = 1545.0


def usd(price, **changes):
    return make_listing(price=price, currency="USD", **changes)


# ---------------------------------------------------------------- the rate


class RateApi:
    def __init__(self, *answers):
        self.answers = list(answers)
        self.urls = []

    def get(self, url, timeout):
        self.urls.append(url)
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


class RateAnswer:
    def __init__(self, data, status=200):
        self.data = data
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))

    def json(self):
        return self.data


@pytest.fixture
def real_fetch(monkeypatch):
    """Undo the conftest guard: these tests talk to a fake API."""
    monkeypatch.undo()


def test_the_rate_is_the_selling_price_fetched_once_an_hour(real_fetch, monkeypatch):
    api = RateApi(RateAnswer({"compra": 1495, "venta": 1545}), RateAnswer({"venta": 1600}))
    clock = [1000.0]
    monkeypatch.setattr(exchange.time, "time", lambda: clock[0])
    rate = UsdRate("oficial", session=api)

    assert rate.current() == 1545
    clock[0] += 1800
    assert rate.current() == 1545  # still fresh
    clock[0] += 1801
    assert rate.current() == 1600
    assert api.urls[0] == "https://dolarapi.com/v1/dolares/oficial"


def test_a_failed_refresh_keeps_the_last_rate(real_fetch, monkeypatch):
    api = RateApi(
        RateAnswer({"venta": 1545}), requests.ConnectionError("down"), RateAnswer({}, 500)
    )
    clock = [1000.0]
    monkeypatch.setattr(exchange.time, "time", lambda: clock[0])
    rate = UsdRate(session=api)
    assert rate.current() == 1545
    clock[0] += 4000
    assert rate.current() == 1545
    clock[0] += 4000
    assert rate.current() == 1545


@pytest.mark.parametrize(
    "answer", [RateAnswer({"compra": 1}), RateAnswer({"venta": "n/a"}), RateAnswer({"venta": 0})]
)
def test_an_odd_answer_gives_no_rate(real_fetch, answer):
    assert UsdRate(session=RateApi(answer)).current() is None


def test_a_fixed_rate_never_asks():
    assert UsdRate(fixed=1500, session=RateApi()).current() == 1500


# ---------------------------------------------------------------- the filter


def evaluate(listing, rate=RATE, **criteria):
    return ListingFilter(SearchCriteria(**criteria), lambda: rate).evaluate(listing)


def test_a_dollar_rent_within_the_budget_is_accepted():
    assert evaluate(usd(600)).accepted  # 927.000 ARS <= 1.000.000


def test_a_dollar_rent_above_the_budget_is_rejected():
    assert evaluate(usd(700)).reason is RejectReason.PRICE_ABOVE_MAX  # 1.081.500 ARS


def test_dollars_are_rejected_when_the_subscriber_hides_them():
    assert evaluate(usd(600), include_usd=False).reason is RejectReason.CURRENCY


def test_dollars_are_rejected_without_a_rate():
    assert evaluate(usd(600), rate=None).reason is RejectReason.CURRENCY
    assert ListingFilter(SearchCriteria()).evaluate(usd(600)).reason is RejectReason.CURRENCY


def test_other_currencies_are_still_rejected():
    assert evaluate(make_listing(currency="EUR")).reason is RejectReason.CURRENCY


def test_the_rate_is_only_asked_for_dollar_listings():
    asked = []
    ListingFilter(SearchCriteria(), lambda: asked.append(1) or RATE).evaluate(make_listing())
    assert asked == []


# ---------------------------------------------------------------- the setting


def test_the_setting_is_stored_and_widened_like_the_others():
    hidden = SearchCriteria().with_changes({"include_usd": False})
    assert hidden.include_usd is False
    assert hidden.to_dict()["include_usd"] is False
    assert hidden.with_changes({"include_usd": "yes"}).include_usd is False  # invalid: kept
    assert SearchCriteria.union([hidden, SearchCriteria()]).include_usd is True
    assert SearchCriteria.union([hidden, hidden]).include_usd is False
    assert "no USD" in hidden.describe()


def test_environment_sets_the_default_and_the_rate(monkeypatch):
    monkeypatch.setenv("INCLUDE_USD", "false")
    monkeypatch.setenv("USD_RATE_SOURCE", "Blue")
    monkeypatch.setenv("USD_ARS_RATE", "1500")
    config = load_config()
    assert config.criteria.include_usd is False
    assert (config.usd_rate_source, config.usd_ars_rate) == ("blue", 1500)


def test_by_default_dollars_are_shown_at_the_oficial_rate(monkeypatch):
    for key in ("INCLUDE_USD", "USD_RATE_SOURCE", "USD_ARS_RATE"):
        monkeypatch.delenv(key, raising=False)
    config = load_config()
    assert config.criteria.include_usd is True
    assert (config.usd_rate_source, config.usd_ars_rate) == ("oficial", None)


# ---------------------------------------------------------------- messages and menu


def test_a_dollar_rent_is_shown_in_pesos_too():
    assert format_price(usd(600), RATE) == "US$ 600 (≈ $ 927.000)"
    assert format_price(usd(600)) == "US$ 600"
    assert format_price(make_listing(price=800_000), RATE) == "$ 800.000"
    message = build_listing_message(usd(600), usd_rate=RATE)
    assert "≈ $ 927.000" in message.telegram_html
    assert "≈ $ 927.000" in message.text and "≈ $ 927.000" in message.email_html


def test_the_menu_toggles_dollar_listings(bot):
    assert "USD listings: shown, at the oficial rate" in bot.settings_reply("7").text
    reply = bot.handle_callback("7", "u:toggle")
    assert "USD listings hidden" in reply.text
    assert bot._criteria("7").include_usd is False
    assert "💵 USD listings: off" in [b["text"] for row in reply.keyboard for b in row]
    bot.handle_callback("7", "u:toggle")
    assert bot._criteria("7").include_usd is True


# ---------------------------------------------------------------- a whole cycle


def test_a_cycle_notifies_a_dollar_listing_with_its_peso_price(database):
    database.add_subscriber("1")
    config = Config(
        criteria=SearchCriteria(),
        telegram=TelegramConfig(bot_token="t", group_threshold=3),
        sources={"mercadolibre": SourceConfig(name="mercadolibre")},
        notify_on_first_run=True,
        usd_ars_rate=RATE,
    )
    scraper = FakeScraper("mercadolibre", [usd(600, external_id="U1"), usd(900, external_id="U2")])
    notifier = FakeRecipientNotifier()
    MonitorService(config, database, scrapers=[scraper], notifier=notifier).run_cycle()

    [message] = notifier.to("1")
    assert "US$ 600 (≈ $ 927.000)" in message
