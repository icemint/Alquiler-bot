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
from functools import partial
from typing import Callable, Dict, List, Optional, Set

import requests

from monitor.config import EmailConfig, SearchCriteria, TelegramConfig
from monitor.database import Database, StoredListing
from monitor.logging_setup import log_context
from monitor.services.filtering import ListingFilter
from monitor.services.notification.formatter import (
    build_group_message,
    build_listing_message,
    chunk_for_group,
)
from monitor.services.notification.message import NotificationMessage
from monitor.services.subscriptions import subscriber_criteria

logger = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"
MAX_MESSAGE_LENGTH = 4000
# Returned by TelegramNotifier._call when the chat blocked the bot: not a
# delivery, but nothing to retry either.
_BLOCKED: dict = {"ok": False, "blocked": True}


class Notifier(abc.ABC):
    """Transport-agnostic notification sink."""

    name = "notifier"

    @abc.abstractmethod
    def send(self, message: NotificationMessage) -> bool:
        """Deliver one message. Returns True when it was accepted."""

    @property
    def available(self) -> bool:
        return True


class RecipientNotifier(Notifier):
    """A channel that can address one subscriber at a time, so each one
    gets only the listings matching their own search."""

    @abc.abstractmethod
    def send_to(self, chat_id: str, message: NotificationMessage) -> bool:
        """Deliver to one chat. True when delivered, or when the chat is gone
        and retrying is pointless."""


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


class TelegramNotifier(RecipientNotifier):
    """Sends to the chats that sent /start."""

    name = "telegram"

    def __init__(
        self,
        bot_token: str,
        database: Database,
        timeout: float = 15.0,
        session: Optional[requests.Session] = None,
    ) -> None:
        self.bot_token = bot_token
        self.database = database
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
        if response.status_code == 403:
            # The user blocked the bot or deleted the chat: retrying is pointless.
            chat_id = str(payload.get("chat_id", ""))
            self.database.remove_subscriber(chat_id)
            log_context(
                logger,
                logging.WARNING,
                "subscriber removed: bot blocked",
                chat_id=chat_id,
                description=data.get("description"),
            )
            return _BLOCKED
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

    def send_to(self, chat_id: str, message: NotificationMessage) -> bool:
        text = message.telegram_html
        if len(text) > MAX_MESSAGE_LENGTH:
            text = text[: MAX_MESSAGE_LENGTH - 20] + "\n… (truncated)"
        result = self._call(
            "sendMessage",
            {
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
        )
        return result is not None

    def send(self, message: NotificationMessage) -> bool:
        """The same message to every active subscriber."""
        subscribers = self.database.active_subscribers()
        if not subscribers:
            # Nobody to tell: treat it as delivered so the queue does not grow.
            logger.info("no telegram subscribers, notification logged:\n%s", message.text)
            return True
        # Same rule as CompositeNotifier: one successful delivery is enough.
        delivered = False
        for chat_id in subscribers:
            delivered = self.send_to(chat_id, message) or delivered
        return delivered


class NotificationService:
    """Turns the pending queue into messages, exactly once per listing and chat.

    With a channel that can address single chats (Telegram), each subscriber
    gets the pending listings that match their own search. Any other channel
    gets every pending listing, as one broadcast.
    """

    def __init__(
        self,
        database: Database,
        notifier: Notifier,
        config: TelegramConfig,
        default_criteria: Optional[SearchCriteria] = None,
        usd_rate: Optional[Callable[[], Optional[float]]] = None,
    ) -> None:
        self.database = database
        self.notifier = notifier
        self.config = config
        self.default_criteria = default_criteria or SearchCriteria()
        self.usd_rate = usd_rate

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

        if isinstance(self.notifier, RecipientNotifier):
            return self._notify_subscribers(pending)

        sent = self._deliver(self.notifier.send, pending)
        self.database.mark_notified(sent)
        return len(sent)

    def _notify_subscribers(self, pending: List[StoredListing]) -> int:
        assert isinstance(self.notifier, RecipientNotifier)
        keys = [item.dedupe_key for item in pending]
        searches: Dict[str, SearchCriteria] = subscriber_criteria(
            self.database, self.default_criteria
        )
        if not searches:
            logger.info("no subscribers: %s listing(s) recorded without sending", len(keys))
            self.database.mark_notified(keys)
            return 0

        sent_total = 0
        # Listings some chat still has to receive stay pending for a retry;
        # the deliveries table keeps the others from getting them twice.
        unfinished: Set[str] = set()
        for chat_id, criteria in searches.items():
            listing_filter = ListingFilter(criteria, self.usd_rate)
            already = set(self.database.delivered_keys(chat_id, keys))
            mine = [
                item
                for item in pending
                if item.dedupe_key not in already and listing_filter.evaluate(item.listing).accepted
            ]
            if not mine:
                continue
            sent = self._deliver(partial(self.notifier.send_to, chat_id), mine)
            self.database.record_deliveries(chat_id, sent)
            sent_total += len(sent)
            unfinished.update(item.dedupe_key for item in mine[len(sent) :])
        self.database.mark_notified([key for key in keys if key not in unfinished])
        return sent_total

    def _deliver(self, send, items: List[StoredListing]) -> List[str]:
        """Send ``items`` in order, as a digest when there are many; stops at
        the first failure. Returns the keys that went out."""
        sent: List[str] = []
        # Dollar rents are shown in pesos too; the rate is only looked up
        # when there is one.
        rate = (
            self.usd_rate()
            if self.usd_rate and any(item.listing.currency == "USD" for item in items)
            else None
        )
        if len(items) > self.config.group_threshold:
            for chunk in chunk_for_group(items, MAX_MESSAGE_LENGTH, rate):
                if not send(build_group_message(chunk, usd_rate=rate)):
                    break
                sent.extend(item.dedupe_key for item in chunk)
                time.sleep(0.5)
            return sent

        for item in items:
            if not send(build_listing_message(item.listing, item.first_seen_at, rate)):
                break
            sent.append(item.dedupe_key)
            time.sleep(0.5)  # stay well inside Telegram's rate limits
        return sent


def build_notifier(
    telegram: TelegramConfig,
    email: Optional[EmailConfig] = None,
    database: Optional[Database] = None,
) -> Notifier:
    """Every configured channel is used; with none configured, messages are logged."""
    channels: List[Notifier] = []
    # Telegram recipients live in the database, so it is needed for that channel.
    if telegram.enabled and telegram.configured and database is not None:
        channels.append(TelegramNotifier(telegram.bot_token or "", database))
    if email is not None and email.enabled and email.configured:
        channels.append(EmailNotifier(email))
    if not channels:
        return LoggingNotifier()
    if len(channels) == 1:
        return channels[0]
    return CompositeNotifier(channels)
