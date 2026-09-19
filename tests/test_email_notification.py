"""Email channel: SMTP conversation, message shape, and multi-channel fan-out."""

import smtplib

import pytest
from conftest import make_listing

from monitor.config import EmailConfig, TelegramConfig, load_config
from monitor.services.notification import (
    CompositeNotifier,
    EmailNotifier,
    Notifier,
    build_listing_message,
    build_notifier,
)


class FakeSMTP:
    """Records the SMTP conversation instead of opening a socket."""

    instances = []

    def __init__(self, host, port, timeout=None):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.calls = []
        self.sent = []
        self.quit_called = False
        FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.quit_called = True
        return False

    def ehlo(self):
        self.calls.append("ehlo")

    def starttls(self):
        self.calls.append("starttls")

    def login(self, username, password):
        self.calls.append(("login", username, password))

    def send_message(self, mail):
        self.sent.append(mail)


@pytest.fixture(autouse=True)
def clear_instances():
    FakeSMTP.instances = []
    yield
    FakeSMTP.instances = []


@pytest.fixture
def email_config():
    return EmailConfig(
        host="smtp.gmail.com",
        port=587,
        username="nastya@example.com",
        password="app-password",
        from_address="nastya@example.com",
        recipients=["nastya@example.com"],
    )


@pytest.fixture
def message():
    return build_listing_message(make_listing())


def test_starttls_session_logs_in_and_sends(monkeypatch, email_config, message):
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    assert EmailNotifier(email_config).send(message) is True

    session = FakeSMTP.instances[0]
    assert (session.host, session.port) == ("smtp.gmail.com", 587)
    assert "starttls" in session.calls
    assert ("login", "nastya@example.com", "app-password") in session.calls
    assert session.quit_called is True
    assert len(session.sent) == 1


def test_implicit_tls_skips_starttls(monkeypatch, email_config, message):
    monkeypatch.setattr(smtplib, "SMTP_SSL", FakeSMTP)
    email_config.use_ssl = True
    email_config.port = 465
    assert EmailNotifier(email_config).send(message) is True
    assert "starttls" not in FakeSMTP.instances[0].calls


def test_message_carries_subject_recipients_and_both_parts(monkeypatch, email_config, message):
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    email_config.recipients = ["nastya@example.com", "second@example.com"]
    EmailNotifier(email_config).send(message)

    mail = FakeSMTP.instances[0].sent[0]
    assert mail["Subject"] == "[Alquiler] Departamento in Recoleta - $ 800.000"
    assert mail["To"] == "nastya@example.com, second@example.com"
    assert "CABA Rental Monitor" in mail["From"]

    body = mail.get_body(preferencelist=("plain",)).get_content()
    html = mail.get_body(preferencelist=("html",)).get_content()
    assert "Price: $ 800.000" in body
    assert "https://departamento.mercadolibre.com.ar/MLA-1-depto-_JM" in body
    assert "<a href=" in html and "Open listing" in html
    assert "55 m²" in html and "Recoleta" in html


def test_smtp_failure_is_reported_not_raised(monkeypatch, email_config, message):
    def explode(*_args, **_kwargs):
        raise smtplib.SMTPAuthenticationError(535, b"bad credentials")

    monkeypatch.setattr(smtplib, "SMTP", explode)
    assert EmailNotifier(email_config).send(message) is False


def test_connection_error_is_reported_not_raised(monkeypatch, email_config, message):
    def explode(*_args, **_kwargs):
        raise OSError("connection refused")

    monkeypatch.setattr(smtplib, "SMTP", explode)
    assert EmailNotifier(email_config).send(message) is False


# ---------------------------------------------------------------- fan-out


class Recorder(Notifier):
    def __init__(self, name, succeed=True):
        self.name = name
        self.succeed = succeed
        self.count = 0

    def send(self, message):
        self.count += 1
        return self.succeed


def test_every_channel_receives_the_notification(message):
    telegram, email = Recorder("telegram"), Recorder("email")
    assert CompositeNotifier([telegram, email]).send(message) is True
    assert (telegram.count, email.count) == (1, 1)


def test_one_healthy_channel_is_enough_to_count_as_delivered(message):
    working, broken = Recorder("email"), Recorder("telegram", succeed=False)
    assert CompositeNotifier([broken, working]).send(message) is True
    assert working.count == 1


def test_all_channels_failing_means_not_delivered(message):
    composite = CompositeNotifier([Recorder("a", succeed=False), Recorder("b", succeed=False)])
    assert composite.send(message) is False


def test_a_raising_channel_does_not_stop_the_others(message):
    class Exploding(Notifier):
        name = "exploding"

        def send(self, message):
            raise RuntimeError("boom")

    working = Recorder("email")
    assert CompositeNotifier([Exploding(), working]).send(message) is True
    assert working.count == 1


# ---------------------------------------------------------------- wiring


def test_email_only_configuration_builds_an_email_notifier(email_config):
    notifier = build_notifier(TelegramConfig(), email_config)
    assert isinstance(notifier, EmailNotifier)


def test_both_channels_configured_builds_a_composite(email_config):
    notifier = build_notifier(TelegramConfig(bot_token="t", chat_id="1"), email_config)
    assert isinstance(notifier, CompositeNotifier)
    assert {channel.name for channel in notifier.notifiers} == {"telegram", "email"}


def test_no_channel_configured_falls_back_to_logging():
    assert build_notifier(TelegramConfig(), EmailConfig()).name == "log"


def test_disabled_email_is_not_used(email_config):
    email_config.enabled = False
    assert build_notifier(TelegramConfig(), email_config).name == "log"


def test_email_settings_come_from_the_environment(monkeypatch):
    for key in ("SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASSWORD", "EMAIL_TO", "EMAIL_FROM"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("SMTP_USER", "nastya@example.com")
    monkeypatch.setenv("SMTP_PASSWORD", "app-password")
    monkeypatch.setenv("EMAIL_TO", "nastya@example.com")

    config = load_config()
    assert config.email.configured is True
    assert config.email.host == "smtp.gmail.com"
    assert config.email.port == 587
    # Gmail refuses a From that is not the authenticated user, so it defaults to it.
    assert config.email.from_address == "nastya@example.com"
    assert config.email.recipients == ["nastya@example.com"]


def test_port_465_switches_to_implicit_tls(monkeypatch):
    monkeypatch.setenv("SMTP_PORT", "465")
    monkeypatch.setenv("SMTP_USER", "a@b.com")
    monkeypatch.setenv("EMAIL_TO", "a@b.com")
    monkeypatch.delenv("SMTP_SSL", raising=False)
    assert load_config().email.use_ssl is True


def test_email_is_not_configured_without_recipients(monkeypatch):
    for key in ("EMAIL_TO", "SMTP_USER", "EMAIL_FROM"):
        monkeypatch.delenv(key, raising=False)
    assert load_config().email.configured is False
