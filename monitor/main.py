"""Entry point: wires configuration, database, scrapers and Telegram together."""

from __future__ import annotations

import argparse
import logging
import signal
import sys
from types import FrameType
from typing import Optional

from monitor.config import ConfigError, load_config
from monitor.database import Database
from monitor.logging_setup import setup_logging
from monitor.scheduler import MonitorService
from monitor.telegram import TelegramCommandBot

logger = logging.getLogger("monitor")


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description="CABA rental listing monitor")
    parser.add_argument(
        "--once", action="store_true", help="run a single monitoring cycle and exit"
    )
    parser.add_argument(
        "--no-telegram-commands", action="store_true", help="do not poll for bot commands"
    )
    args = parser.parse_args(argv)

    try:
        config = load_config()
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    setup_logging(config.log_level, config.log_format)

    database = Database(config.database_path)
    if config.telegram.chat_id:
        # The owner's chat is subscribed once; a later /stop from it is respected.
        database.add_subscriber(config.telegram.chat_id, reactivate=False)

    channels = []
    if config.telegram.enabled and config.telegram.configured:
        channels.append(f"telegram -> {database.count_subscribers()} subscribers")
    if config.email.enabled and config.email.configured:
        channels.append("email -> {0}".format(", ".join(config.email.recipients)))
    if channels:
        logger.info("notification channels: %s", "; ".join(channels))
    else:
        logger.warning(
            "no notification channel configured (TELEGRAM_BOT_TOKEN or SMTP_HOST/EMAIL_TO): "
            "notifications will be written to the log instead"
        )
    service = MonitorService(config, database)

    if not service.scrapers:
        logger.error("no sources enabled (check the SOURCES environment variable)")
        return 2

    bot: Optional[TelegramCommandBot] = None
    if config.telegram.commands_enabled and not args.no_telegram_commands:
        bot = TelegramCommandBot(config, database)
        bot.start()

    def handle_signal(signum: int, _frame: Optional[FrameType]) -> None:
        logger.info("received signal %s, shutting down", signum)
        service.stop()
        if bot:
            bot.stop()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    try:
        if args.once:
            service.run_cycle()
        else:
            service.run_forever()
    finally:
        if bot:
            bot.stop()
        database.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
