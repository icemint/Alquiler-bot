"""One notification, rendered for every channel that might carry it.

Telegram accepts only a small HTML subset and ignores subjects; email needs a
subject plus a plain-text alternative next to real HTML. Rendering all of them
once keeps the channels from each having their own idea of the content.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class NotificationMessage:
    subject: str
    text: str
    telegram_html: str
    email_html: str
