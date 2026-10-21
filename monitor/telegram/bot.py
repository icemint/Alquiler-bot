"""Telegram command handling (/start, /stop, /status, /stats).

Runs in its own daemon thread with long polling. Anyone can talk to the bot:
/start subscribes the chat to new-listing notifications and /stop ends that.
/status and /stats only expose aggregate counts, never who is subscribed.
"""

from __future__ import annotations

import logging
import threading
from datetime import timedelta
from html import escape
from typing import Any, Dict, List, Optional

import requests

from monitor.config import Config
from monitor.database import Database
from monitor.logging_setup import log_context
from monitor.services.notification.formatter import source_label
from monitor.timeutil import format_local, from_iso, utcnow

logger = logging.getLogger("monitor.telegram")

TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"
OFFSET_KEY = "telegram_update_offset"
POLL_TIMEOUT_SECONDS = 25


class TelegramCommandBot(threading.Thread):
    def __init__(self, config: Config, database: Database) -> None:
        super().__init__(name="telegram-commands", daemon=True)
        self.config = config
        self.database = database
        self.session = requests.Session()
        self._stop = threading.Event()

    # ------------------------------------------------------------------ api

    def _call(
        self, method: str, payload: Dict[str, Any], timeout: float = 35.0
    ) -> Optional[Dict[str, Any]]:
        url = TELEGRAM_API.format(token=self.config.telegram.bot_token, method=method)
        try:
            response = self.session.post(url, json=payload, timeout=timeout)
            data = response.json()
        except (requests.RequestException, ValueError) as exc:
            log_context(
                logger, logging.WARNING, "telegram poll failed", method=method, error=str(exc)
            )
            return None
        if not data.get("ok"):
            log_context(
                logger,
                logging.WARNING,
                "telegram API error",
                method=method,
                description=data.get("description"),
            )
            return None
        return data

    def _send(self, chat_id: str, text: str) -> None:
        self._call(
            "sendMessage",
            {
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
        )

    # ------------------------------------------------------------------ commands

    def handle_command(
        self, command: str, chat_id: str = "", username: Optional[str] = None
    ) -> str:
        command = command.split("@")[0].strip().lower()
        if command == "/start":
            if chat_id:
                self.database.add_subscriber(chat_id, username)
                log_context(logger, logging.INFO, "subscriber added", chat_id=chat_id)
            return self.start_message() + "\n\n✅ You are subscribed. /stop to unsubscribe."
        if command == "/stop":
            if chat_id and self.database.remove_subscriber(chat_id):
                log_context(logger, logging.INFO, "subscriber removed", chat_id=chat_id)
                return "🔕 Unsubscribed. Send /start to subscribe again."
            return "You are not subscribed. Send /start to subscribe."
        if command == "/status":
            return self.status_message()
        if command == "/stats":
            return self.stats_message()
        if command == "/help":
            return self.start_message()
        return "Unknown command. Try /start, /stop, /status or /stats."

    def start_message(self) -> str:
        criteria = self.config.criteria
        return "\n".join(
            [
                "👋 <b>CABA rental monitor</b>",
                "",
                "I watch {0} and message you when a new matching rental appears.".format(
                    ", ".join(source_label(name) for name in self.config.enabled_sources())
                    or "no source"
                ),
                "",
                "<b>Current search</b>",
                "Neighborhoods: {0}".format(escape(", ".join(criteria.neighborhoods))),
                "Types: {0}".format(escape(", ".join(t.value for t in criteria.property_types))),
                f"Max rent: {criteria.max_rent:,.0f} {criteria.currency} (expensas excluded)".replace(
                    ",", "."
                ),
                f"Min area: {criteria.min_area_m2:g} m²",
                f"Checked every {self.config.check_interval_seconds / 60:g} minutes",
                "",
                "Commands: /status · /stats · /stop",
            ]
        )

    def status_message(self) -> str:
        last_cycle = self.database.last_successful_cycle()
        states = self.database.source_states()
        criteria = self.config.criteria

        working = [name for name, state in states.items() if state.get("last_status") == "ok"]
        lines = [
            "📡 <b>Status</b>",
            "",
            "Last successful check: {0}".format(
                format_local(from_iso(last_cycle["finished_at"])) if last_cycle else "never"
            ),
            f"Listings stored: {self.database.count_listings()}",
            f"Sources working: {len(working)}/{len(self.config.enabled_sources())}",
            "",
            "<b>Filters</b>",
            "Operation: Alquiler",
            "Types: {0}".format(escape(", ".join(t.value for t in criteria.property_types))),
            "Neighborhoods: {0}".format(escape(", ".join(criteria.neighborhoods))),
            f"Max price: {criteria.max_rent:,.0f} {criteria.currency} (expensas excluded)".replace(
                ",", "."
            ),
            f"Min area: {criteria.min_area_m2:g} m²",
        ]
        return "\n".join(lines)

    def stats_message(self) -> str:
        since = utcnow() - timedelta(days=1)
        states = self.database.source_states()
        per_source = self.database.counts_by_source()

        lines = [
            "📊 <b>Stats</b>",
            "",
            f"Listings monitored: {self.database.count_listings():,}".replace(",", "."),
            f"Matching stored: {self.database.count_listings(matched_only=True)}",
            f"New today: {self.database.count_new_since(since, matched_only=True)}",
            f"Notifications today: {self.database.count_notified_since(since)}",
            f"Cycles today: {self.database.cycles_since(since)}",
            f"Subscribers: {self.database.count_subscribers()}",
            "",
        ]
        for name in self.config.enabled_sources():
            state = states.get(name, {})
            status = state.get("last_status")
            if status == "ok":
                marker = "OK"
            elif status is None:
                marker = "not checked yet"
            else:
                marker = "{0} ({1})".format(status, (state.get("last_error") or "")[:80])
            lines.append(f"{source_label(name)}: {marker} · {per_source.get(name, 0)} stored")
        return "\n".join(lines)

    # ------------------------------------------------------------------ loop

    def _updates(self) -> List[Dict[str, Any]]:
        offset = self.database.get_state(OFFSET_KEY)
        payload: Dict[str, Any] = {
            "timeout": POLL_TIMEOUT_SECONDS,
            "allowed_updates": ["message"],
        }
        if offset:
            payload["offset"] = int(offset)
        data = self._call("getUpdates", payload, timeout=POLL_TIMEOUT_SECONDS + 10)
        return data.get("result", []) if data else []

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        if not self.config.telegram.configured:
            logger.info("telegram commands disabled: no bot token configured")
            return
        logger.info("telegram command bot started")
        while not self._stop.is_set():
            try:
                updates = self._updates()
            except Exception:  # noqa: BLE001 - the poller must never die
                logger.exception("telegram polling error")
                self._stop.wait(10)
                continue

            for update in updates:
                self.database.set_state(OFFSET_KEY, str(update.get("update_id", 0) + 1))
                message = update.get("message") or {}
                text = (message.get("text") or "").strip()
                chat = message.get("chat") or {}
                chat_id = str(chat.get("id", ""))
                if not text.startswith("/") or not chat_id:
                    continue
                log_context(logger, logging.INFO, "telegram command", command=text.split()[0])
                reply = self.handle_command(text.split()[0], chat_id, chat.get("username"))
                self._send(chat_id, reply)

            if not updates:
                self._stop.wait(1)
        logger.info("telegram command bot stopped")
