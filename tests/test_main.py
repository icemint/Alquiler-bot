"""The entry point: configuration errors, wiring and shutdown.

The monitor service and the bot are replaced by fakes: main() is tested for
what it wires together, not for what a cycle does.
"""

import logging
import signal

import pytest

from monitor import main as entry
from monitor.database import Database

ENV = [
    "CONFIG_FILE",
    "SOURCES",
    "NEIGHBORHOODS",
    "CITIES",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
    "TELEGRAM_COMMANDS_ENABLED",
    "SMTP_HOST",
    "SMTP_USER",
    "EMAIL_FROM",
    "EMAIL_TO",
    "EMAIL_ENABLED",
]


class FakeService:
    instances = []

    def __init__(self, config, database):
        self.scrapers = ["scraper"] if config.sources else []
        self.calls = []
        FakeService.instances.append(self)

    def run_cycle(self):
        self.calls.append("cycle")

    def run_forever(self):
        self.calls.append("forever")

    def stop(self):
        self.calls.append("stop")


class FakeBot:
    instances = []

    def __init__(self, config, database):
        self.calls = []
        FakeBot.instances.append(self)

    def start(self):
        self.calls.append("start")

    def stop(self):
        self.calls.append("stop")


@pytest.fixture(autouse=True)
def wiring(monkeypatch, tmp_path):
    for key in ENV:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "main.sqlite3"))
    monkeypatch.setenv("SOURCES", "mercadolibre")
    FakeService.instances.clear()
    FakeBot.instances.clear()
    monkeypatch.setattr(entry, "MonitorService", FakeService)
    monkeypatch.setattr(entry, "TelegramCommandBot", FakeBot)
    monkeypatch.setattr(entry, "setup_logging", lambda *args: None)
    handlers = {}
    monkeypatch.setattr(
        entry.signal, "signal", lambda sig, handler: handlers.update({sig: handler})
    )
    return handlers


def test_a_configuration_error_exits_with_2(monkeypatch, capsys):
    monkeypatch.setenv("NEIGHBORHOODS", "Narnia")
    assert entry.main([]) == 2
    assert "Configuration error" in capsys.readouterr().err
    assert FakeService.instances == []


def test_once_runs_a_single_cycle(caplog):
    with caplog.at_level(logging.WARNING, logger="monitor"):
        assert entry.main(["--once"]) == 0
    [service] = FakeService.instances
    assert service.calls == ["cycle"]
    assert "no notification channel configured" in caplog.text


def test_the_service_runs_until_stopped_with_the_bot_alongside(monkeypatch, wiring):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "token")
    assert entry.main([]) == 0
    [service], [bot] = FakeService.instances, FakeBot.instances
    assert service.calls == ["forever"]
    assert bot.calls == ["start", "stop"]  # stopped on the way out

    wiring[signal.SIGTERM](signal.SIGTERM, None)
    assert service.calls[-1] == "stop" and bot.calls[-1] == "stop"
    assert set(wiring) == {signal.SIGINT, signal.SIGTERM}


def test_the_command_bot_can_be_turned_off(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "token")
    assert entry.main(["--once", "--no-telegram-commands"]) == 0
    assert FakeBot.instances == []


def test_the_owner_chat_is_subscribed(monkeypatch, tmp_path, caplog):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    with caplog.at_level(logging.INFO, logger="monitor"):
        entry.main(["--once"])
    database = Database(str(tmp_path / "main.sqlite3"))
    assert database.active_subscribers() == ["42"]
    database.close()
    assert "telegram -> 1 subscribers" in caplog.text


def test_email_is_listed_as_a_channel(monkeypatch, caplog):
    monkeypatch.setenv("SMTP_USER", "me@example.com")
    monkeypatch.setenv("EMAIL_TO", "you@example.com")
    with caplog.at_level(logging.INFO, logger="monitor"):
        entry.main(["--once"])
    assert "email -> you@example.com" in caplog.text


def test_no_sources_exits_with_2(monkeypatch, caplog):
    monkeypatch.setattr(FakeService, "__init__", _no_scrapers)
    assert entry.main(["--once"]) == 2
    assert "no sources enabled" in caplog.text


def _no_scrapers(self, config, database):
    self.scrapers = []
    self.calls = []
