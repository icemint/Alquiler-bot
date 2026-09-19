"""Notification delivery.

The scheduler talks to ``NotificationService``; the transport behind it is a
``Notifier`` implementation. Telegram is the only channel today, and nothing
outside this package knows that.
"""

from __future__ import annotations

import abc
import logging
import smtplib
import time
from email.message import EmailMessage
from email.utils import formataddr
from typing import List, Optional

import requests

from monitor.config import EmailConfig, TelegramConfig
from monitor.database import Database, StoredListing
from monitor.logging_setup import log_context
from monitor.services.notification.formatter import (
    build_group_message,
    build_listing_message,
    chunk_for_group,
)
from monitor.services.notification.message import NotificationMessage

logger = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"
MAX_MESSAGE_LENGTH = 4000


class Notifier(abc.ABC):
    """Transport-agnostic notification sink."""

    name = "notifier"

    @abc.abstractmethod
    def send(self, message: NotificationMessage) -> bool:
        """Deliver one message. Returns True when it was accepted."""

    @property
    def available(self) -> bool:
        return True


class LoggingNotifier(Notifier):
    """Used when no channel is configured: nothing is lost, it is logged."""

    name = "log"

    def send(self, message: NotificationMessage) -> bool:
        logger.info("notification (no channel configured):\n%s", message.text)
        return True


class CompositeNotifier(Notifier):
    """Fans one notification out to every configured channel.

    Succeeds when at least one channel accepted the message: a listing that
    reached your inbox must not be re-sent forever because a second channel is
    misconfigured. Failures are logged per channel.
    """

    name = "composite"

    def __init__(self, notifiers: List[Notifier]) -> None:
        self.notifiers = notifiers

    def send(self, message: NotificationMessage) -> bool:
        delivered = False
        for notifier in self.notifiers:
            try:
                if notifier.send(message):
                    delivered = True
                else:
                    log_context(logger, logging.WARNING, "channel failed", channel=notifier.name)
            except Exception as exc:  # noqa: BLE001 - one channel must not break the others
                log_context(
                    logger, logging.ERROR, "channel raised", channel=notifier.name, error=str(exc)
                )
        return delivered


class EmailNotifier(Notifier):
    """SMTP delivery, sending a plain-text part alongside the HTML one."""

    name = "email"

    def __init__(self, config: EmailConfig) -> None:
        self.config = config

    def _build(self, message: NotificationMessage) -> EmailMessage:
        mail = EmailMessage()
        mail["Subject"] = f"{self.config.subject_prefix}{message.subject}"
        mail["From"] = formataddr((self.config.from_name, self.config.from_address))
        mail["To"] = ", ".join(self.config.recipients)
        mail.set_content(message.text)
        mail.add_alternative(message.email_html, subtype="html")
        return mail

    def send(self, message: NotificationMessage) -> bool:
        mail = self._build(message)
        try:
            if self.config.use_ssl:
                server = smtplib.SMTP_SSL(
                    self.config.host, self.config.port, timeout=self.config.timeout
                )
            else:
                server = smtplib.SMTP(
                    self.config.host, self.config.port, timeout=self.config.timeout
                )
            with server:
                server.ehlo()
                if self.config.use_starttls and not self.config.use_ssl:
                    server.starttls()
                    server.ehlo()
                if self.config.username and self.config.password:
                    server.login(self.config.username, self.config.password)
                server.send_message(mail)
        except (smtplib.SMTPException, OSError) as exc:
            log_context(
                logger,
                logging.ERROR,
                "email delivery failed",
                host=self.config.host,
                error=str(exc),
            )
            return False
        log_context(
            logger,
            logging.INFO,
            "email sent",
            to=",".join(self.config.recipients),
            subject=message.subject,
        )
        return True


class TelegramNotifier(Notifier):
    name = "telegram"

    def __init__(
        self,
        bot_token: str,
        chat_id: str,
        timeout: float = 15.0,
        session: Optional[requests.Session] = None,
    ) -> None:
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.timeout = timeout
        self.session = session or requests.Session()

    def _call(self, method: str, payload: dict) -> Optional[dict]:
        url = TELEGRAM_API.format(token=self.bot_token, method=method)
        try:
            response = self.session.post(url, json=payload, timeout=self.timeout)
        except requests.RequestException as exc:
            log_context(
                logger, logging.ERROR, "telegram request failed", method=method, error=str(exc)
            )
            return None
        if response.status_code == 429:
            retry_after = 5
            try:
                retry_after = int(response.json().get("parameters", {}).get("retry_after", 5))
            except ValueError:
                pass
            log_context(logger, logging.WARNING, "telegram rate limited", retry_after=retry_after)
            time.sleep(min(retry_after, 30))
            return None
        try:
            data = response.json()
        except ValueError:
            log_context(
                logger, logging.ERROR, "telegram returned non-JSON", status=response.status_code
            )
            return None
        if not data.get("ok"):
            log_context(
                logger,
                logging.ERROR,
                "telegram API error",
                method=method,
                description=data.get("description"),
            )
            return None
        return data

    def send(self, message: NotificationMessage) -> bool:
        text = message.telegram_html
        if len(text) > MAX_MESSAGE_LENGTH:
            text = text[: MAX_MESSAGE_LENGTH - 20] + "\n… (truncated)"
        result = self._call(
            "sendMessage",
            {
                "chat_id": self.chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
        )
        return result is not None


class NotificationService:
    """Turns the pending queue into messages, exactly once per listing."""

    def __init__(self, database: Database, notifier: Notifier, config: TelegramConfig) -> None:
        self.database = database
        self.notifier = notifier
        self.config = config

    def notify_pending(self) -> int:
        pending: List[StoredListing] = self.database.pending_notifications(
            limit=self.config.max_notifications_per_cycle
        )
        if not pending:
            return 0

        keys = [item.dedupe_key for item in pending]
        # Count the attempt before sending, so a permanently failing message
        # is retried a bounded number of times instead of forever.
        self.database.record_notification_attempt(keys)

        if len(pending) > self.config.group_threshold:
            sent_count = 0
            for chunk in chunk_for_group(pending, MAX_MESSAGE_LENGTH):
                if not self.notifier.send(build_group_message(chunk)):
                    break
                self.database.mark_notified([item.dedupe_key for item in chunk])
                sent_count += len(chunk)
                time.sleep(0.5)
            return sent_count

        sent: List[str] = []
        for item in pending:
            detected_at = item.first_seen_at
            if self.notifier.send(build_listing_message(item.listing, detected_at)):
                sent.append(item.dedupe_key)
                time.sleep(0.5)  # stay well inside Telegram's rate limits
            else:
                break
        self.database.mark_notified(sent)
        return len(sent)


def build_notifier(telegram: TelegramConfig, email: Optional[EmailConfig] = None) -> Notifier:
    """Every configured channel is used; with none configured, messages are logged."""
    channels: List[Notifier] = []
    if telegram.enabled and telegram.configured:
        channels.append(TelegramNotifier(telegram.bot_token or "", telegram.chat_id or ""))
    if email is not None and email.enabled and email.configured:
        channels.append(EmailNotifier(email))
    if not channels:
        return LoggingNotifier()
    if len(channels) == 1:
        return channels[0]
    return CompositeNotifier(channels)
