"""Structured logging.

Text mode produces human-readable lines with a key=value suffix; json mode
produces one JSON object per line for log shipping. Extra fields are passed
through the standard ``extra={"context": {...}}`` mechanism.
"""

from __future__ import annotations

import json
import logging
import sys
from typing import Any, Dict


class ContextFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)
        context: Dict[str, Any] = getattr(record, "context", {}) or {}
        if not context:
            return base
        rendered = " ".join(f"{k}={_render(v)}" for k, v in context.items())
        return f"{base} | {rendered}"


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: Dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        context = getattr(record, "context", {}) or {}
        payload.update(context)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def _render(value: Any) -> str:
    text = str(value)
    return f'"{text}"' if " " in text else text


def setup_logging(level: str = "INFO", fmt: str = "text") -> None:
    handler = logging.StreamHandler(sys.stdout)
    if fmt == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(
            ContextFormatter("[%(asctime)s] %(levelname)-7s %(name)s: %(message)s", "%H:%M:%S")
        )
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("requests").setLevel(logging.WARNING)


def log_context(logger: logging.Logger, level: int, message: str, **context: Any) -> None:
    logger.log(level, message, extra={"context": context})
