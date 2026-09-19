"""Notification delivery.

The scheduler talks to ``NotificationService``; the transport behind it is a
``Notifier`` implementation. Telegram is the only channel today, and nothing
outside this package knows that.
"""

from __future__ import annotations

import abc
import logging
import time
from typing import List, Optional

import requests

from monitor.config import TelegramConfig
from monitor.database import Database, StoredListing
from monitor.logging_setup import log_context
from monitor.services.notification.formatter import (
    chunk_for_group,
    format_group,
    format_listing,
)

logger = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"
MAX_MESSAGE_LENGTH = 4000


class Notifier(abc.ABC):
    """Transport-agnostic notification sink."""

    @abc.abstractmethod
    def send(self, text: str) -> bool:
        """Deliver one message. Returns True when it was accepted."""

    @property
    def available(self) -> bool:
        return True


class LoggingNotifier(Notifier):
    """Used when Telegram is not configured: nothing is lost, it is logged."""

    def send(self, text: str) -> bool:
        logger.info("notification (no Telegram configured):\n%s", text)
        return True


class TelegramNotifier(Notifier):
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

    def send(self, text: str) -> bool:
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
                if not self.notifier.send(format_group(chunk)):
                    break
                self.database.mark_notified([item.dedupe_key for item in chunk])
                sent_count += len(chunk)
                time.sleep(0.5)
            return sent_count

        sent: List[str] = []
        for item in pending:
            detected_at = item.first_seen_at
            if self.notifier.send(format_listing(item.listing, detected_at)):
                sent.append(item.dedupe_key)
                time.sleep(0.5)  # stay well inside Telegram's rate limits
            else:
                break
        self.database.mark_notified(sent)
        return len(sent)


def build_notifier(config: TelegramConfig) -> Notifier:
    if config.enabled and config.configured:
        return TelegramNotifier(config.bot_token or "", config.chat_id or "")
    return LoggingNotifier()
