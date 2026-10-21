"""Log formatting and the UTC <-> Buenos Aires time helpers."""

import json
import logging
from datetime import datetime, timedelta, timezone

import pytest

from monitor import timeutil
from monitor.logging_setup import ContextFormatter, JsonFormatter, log_context, setup_logging


def record(message="cycle done", context=None, exc_info=None):
    entry = logging.LogRecord("monitor.test", logging.INFO, __file__, 1, message, (), exc_info)
    if context is not None:
        entry.context = context
    return entry


# ---------------------------------------------------------------- logging


def test_text_format_appends_the_context():
    formatter = ContextFormatter("%(message)s")
    assert formatter.format(record()) == "cycle done"
    line = formatter.format(record(context={"source": "argenprop", "reason": "min interval"}))
    assert line == 'cycle done | source=argenprop reason="min interval"'


def test_json_format_is_one_object_per_line():
    try:
        raise ValueError("boom")
    except ValueError:
        import sys

        info = sys.exc_info()
    payload = json.loads(JsonFormatter().format(record(context={"found": 3}, exc_info=info)))
    assert payload["message"] == "cycle done"
    assert payload["level"] == "INFO"
    assert payload["found"] == 3
    assert "ValueError: boom" in payload["exception"]


@pytest.fixture
def restore_root_logger():
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    yield root
    root.handlers, root.level = handlers, level


@pytest.mark.parametrize("fmt,formatter", [("json", JsonFormatter), ("text", ContextFormatter)])
def test_setup_logging_picks_the_format(restore_root_logger, fmt, formatter):
    setup_logging("debug", fmt)
    [handler] = restore_root_logger.handlers
    assert isinstance(handler.formatter, formatter)
    assert restore_root_logger.level == logging.DEBUG
    assert logging.getLogger("urllib3").level == logging.WARNING


def test_log_context_passes_fields_through(caplog):
    with caplog.at_level(logging.INFO):
        log_context(logging.getLogger("monitor.test"), logging.INFO, "hello", chat_id="7")
    assert caplog.records[0].context == {"chat_id": "7"}


# ---------------------------------------------------------------- time


def test_iso_round_trip_is_utc():
    local = datetime(2026, 10, 21, 12, 0, tzinfo=timezone(timedelta(hours=-3)))
    assert timeutil.to_iso(local) == "2026-10-21T15:00:00+00:00"
    assert timeutil.to_iso(datetime(2026, 10, 21, 15, 0)) == "2026-10-21T15:00:00+00:00"
    assert timeutil.to_iso(None) is None
    assert timeutil.from_iso("2026-10-21T15:00:00") == datetime(
        2026, 10, 21, 15, 0, tzinfo=timezone.utc
    )
    assert timeutil.from_iso("yesterday") is None
    assert timeutil.from_iso(None) is None
    assert timeutil.utcnow().tzinfo is timezone.utc


def test_times_are_shown_in_buenos_aires(monkeypatch):
    monkeypatch.setenv("TZ", "America/Argentina/Buenos_Aires")
    noon_utc = datetime(2026, 10, 21, 15, 0)
    assert timeutil.format_local(noon_utc) == "21/10/2026 12:00"
    assert timeutil.format_local(None) == "-"


def test_an_unknown_timezone_falls_back_to_argentina(monkeypatch):
    monkeypatch.setenv("TZ", "Mars/Olympus_Mons")
    assert timeutil.local_timezone() is timeutil._FALLBACK_TZ
    assert timeutil.format_local(datetime(2026, 10, 21, 15, 0), "%H:%M") == "12:00"
