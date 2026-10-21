"""SQLite persistence.

The database is the only thing that makes restarts safe: a listing that has
already been notified carries a ``notified_at`` timestamp, so restarting the
service never re-sends anything.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from monitor.models.listing import Listing, Operation, PropertyType
from monitor.timeutil import from_iso, to_iso, utcnow

logger = logging.getLogger(__name__)

SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "schema.sql")


@dataclass
class StoredListing:
    """A listing as it came back out of the database."""

    id: int
    dedupe_key: str
    listing: Listing
    first_seen_at: Optional[datetime]
    last_seen_at: Optional[datetime]
    notified_at: Optional[datetime]
    notify_attempts: int


def _row_to_stored(row: sqlite3.Row) -> StoredListing:
    extra: Dict[str, Any] = {}
    if row["extra"]:
        try:
            extra = json.loads(row["extra"])
        except ValueError:
            extra = {}
    listing = Listing(
        source=row["source"],
        external_id=row["external_id"],
        url=row["url"],
        title=row["title"],
        property_type=PropertyType(row["property_type"])
        if row["property_type"]
        else PropertyType.OTHER,
        operation=Operation(row["operation"]) if row["operation"] else Operation.OTHER,
        neighborhood=row["neighborhood"],
        raw_neighborhood=row["raw_neighborhood"],
        address=row["address"],
        price=row["price"],
        currency=row["currency"],
        area_m2=row["area_m2"],
        rooms=row["rooms"],
        expensas=row["expensas"],
        expensas_currency=row["expensas_currency"],
        published_at=from_iso(row["published_at"]),
        price_is_approximate=bool(row["price_is_approximate"]),
        extra=extra,
    )
    return StoredListing(
        id=row["id"],
        dedupe_key=row["dedupe_key"],
        listing=listing,
        first_seen_at=from_iso(row["first_seen_at"]),
        last_seen_at=from_iso(row["last_seen_at"]),
        notified_at=from_iso(row["notified_at"]),
        notify_attempts=row["notify_attempts"],
    )


class Database:
    def __init__(self, path: str) -> None:
        self.path = path
        directory = os.path.dirname(os.path.abspath(path))
        if directory:
            os.makedirs(directory, exist_ok=True)
        # The Telegram command poller reads from another thread.
        self._conn = sqlite3.connect(path, check_same_thread=False, timeout=30)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._migrate()

    def _migrate(self) -> None:
        with open(SCHEMA_PATH, encoding="utf-8") as handle:
            schema = handle.read()
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.executescript(schema)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------------ listings

    def upsert_listing(
        self,
        listing: Listing,
        matched: bool,
        reject_reason: Optional[str] = None,
        seen_at: Optional[datetime] = None,
    ) -> Tuple[bool, StoredListing]:
        """Insert or refresh a listing.

        Returns ``(is_new, stored)``. ``is_new`` is True only the first time a
        dedupe key is ever seen, which is what drives notifications.
        """
        now = to_iso(seen_at or utcnow())
        key = listing.dedupe_key
        payload = {
            "dedupe_key": key,
            "source": listing.source,
            "external_id": listing.stored_external_id,
            "url": listing.url,
            "title": listing.title,
            "property_type": listing.property_type.value if listing.property_type else None,
            "operation": listing.operation.value if listing.operation else None,
            "neighborhood": listing.neighborhood,
            "raw_neighborhood": listing.raw_neighborhood,
            "address": listing.address,
            "price": listing.price,
            "currency": listing.currency,
            "area_m2": listing.area_m2,
            "rooms": listing.rooms,
            "expensas": listing.expensas,
            "expensas_currency": listing.expensas_currency,
            "published_at": to_iso(listing.published_at),
            "price_is_approximate": int(bool(listing.price_is_approximate)),
            "matched": int(bool(matched)),
            "reject_reason": reject_reason,
            "first_seen_at": now,
            "last_seen_at": now,
            "extra": json.dumps(listing.extra, ensure_ascii=False) if listing.extra else None,
        }
        columns = ", ".join(payload.keys())
        placeholders = ", ".join(f":{name}" for name in payload)
        with self._lock:
            cursor = self._conn.execute(
                f"INSERT OR IGNORE INTO listings ({columns}) VALUES ({placeholders})",
                payload,
            )
            is_new = cursor.rowcount == 1
            if not is_new:
                self._conn.execute(
                    """
                    UPDATE listings
                       SET last_seen_at = :last_seen_at,
                           url = :url,
                           title = COALESCE(:title, title),
                           price = :price,
                           currency = :currency,
                           area_m2 = COALESCE(:area_m2, area_m2),
                           rooms = COALESCE(:rooms, rooms),
                           expensas = :expensas,
                           expensas_currency = :expensas_currency,
                           neighborhood = COALESCE(:neighborhood, neighborhood),
                           price_is_approximate = :price_is_approximate,
                           matched = :matched,
                           reject_reason = :reject_reason
                     WHERE dedupe_key = :dedupe_key
                    """,
                    payload,
                )
            self._conn.commit()
            row = self._conn.execute(
                "SELECT * FROM listings WHERE dedupe_key = ?", (key,)
            ).fetchone()
        return is_new, _row_to_stored(row)

    def pending_notifications(self, limit: int = 50, max_attempts: int = 5) -> List[StoredListing]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM listings
                 WHERE matched = 1 AND notified_at IS NULL AND notify_attempts < ?
                 ORDER BY first_seen_at ASC, id ASC
                 LIMIT ?
                """,
                (max_attempts, limit),
            ).fetchall()
        return [_row_to_stored(row) for row in rows]

    def mark_notified(self, dedupe_keys: List[str], when: Optional[datetime] = None) -> None:
        if not dedupe_keys:
            return
        timestamp = to_iso(when or utcnow())
        with self._lock:
            self._conn.executemany(
                "UPDATE listings SET notified_at = ? WHERE dedupe_key = ?",
                [(timestamp, key) for key in dedupe_keys],
            )
            self._conn.commit()

    def record_notification_attempt(self, dedupe_keys: List[str]) -> None:
        if not dedupe_keys:
            return
        with self._lock:
            self._conn.executemany(
                "UPDATE listings SET notify_attempts = notify_attempts + 1 WHERE dedupe_key = ?",
                [(key,) for key in dedupe_keys],
            )
            self._conn.commit()

    def suppress_notifications(self, dedupe_keys: List[str]) -> int:
        """Mark listings as already notified without sending anything.

        Used to seed a fresh database on the first run so the user is not
        flooded with every listing that already exists.
        """
        if not dedupe_keys:
            return 0
        timestamp = to_iso(utcnow())
        with self._lock:
            cursor = self._conn.executemany(
                "UPDATE listings SET notified_at = ? WHERE dedupe_key = ? AND notified_at IS NULL",
                [(timestamp, key) for key in dedupe_keys],
            )
            self._conn.commit()
            return cursor.rowcount

    def count_listings(self, source: Optional[str] = None, matched_only: bool = False) -> int:
        query = "SELECT COUNT(*) AS n FROM listings WHERE 1=1"
        params: List[Any] = []
        if source:
            query += " AND source = ?"
            params.append(source)
        if matched_only:
            query += " AND matched = 1"
        with self._lock:
            row = self._conn.execute(query, params).fetchone()
        return int(row["n"])

    def has_notified_listings(self, source: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM listings WHERE source = ? AND notified_at IS NOT NULL LIMIT 1",
                (source,),
            ).fetchone()
        return row is not None

    def count_new_since(self, since: datetime, matched_only: bool = False) -> int:
        query = "SELECT COUNT(*) AS n FROM listings WHERE first_seen_at >= ?"
        if matched_only:
            query += " AND matched = 1"
        with self._lock:
            row = self._conn.execute(query, (to_iso(since),)).fetchone()
        return int(row["n"])

    def count_notified_since(self, since: datetime) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM listings WHERE notified_at >= ?", (to_iso(since),)
            ).fetchone()
        return int(row["n"])

    def counts_by_source(self) -> Dict[str, int]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT source, COUNT(*) AS n FROM listings GROUP BY source"
            ).fetchall()
        return {row["source"]: int(row["n"]) for row in rows}

    # ------------------------------------------------------------------ cycles

    def start_cycle(self, started_at: Optional[datetime] = None) -> int:
        with self._lock:
            cursor = self._conn.execute(
                "INSERT INTO cycles (started_at) VALUES (?)", (to_iso(started_at or utcnow()),)
            )
            self._conn.commit()
            return int(cursor.lastrowid)

    def finish_cycle(
        self,
        cycle_id: int,
        sources_total: int,
        sources_ok: int,
        found: int,
        matched: int,
        new_listings: int,
        notified: int,
        errors: Optional[str] = None,
    ) -> None:
        with self._lock:
            self._conn.execute(
                """
                UPDATE cycles
                   SET finished_at = ?, sources_total = ?, sources_ok = ?, found = ?,
                       matched = ?, new_listings = ?, notified = ?, errors = ?
                 WHERE id = ?
                """,
                (
                    to_iso(utcnow()),
                    sources_total,
                    sources_ok,
                    found,
                    matched,
                    new_listings,
                    notified,
                    errors,
                    cycle_id,
                ),
            )
            self._conn.commit()

    def last_successful_cycle(self) -> Optional[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM cycles WHERE finished_at IS NOT NULL AND sources_ok > 0 "
                "ORDER BY id DESC LIMIT 1"
            ).fetchone()

    def cycles_since(self, since: datetime) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM cycles WHERE started_at >= ?", (to_iso(since),)
            ).fetchone()
        return int(row["n"])

    # ------------------------------------------------------------------ source state

    def update_source_state(
        self,
        source: str,
        status: str,
        error: Optional[str],
        found: int,
        new_listings: int,
        block_for: Optional[timedelta] = None,
    ) -> None:
        now = utcnow()
        success = status == "ok"
        blocked_until = to_iso(now + block_for) if block_for else None
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO source_state (
                    source, last_attempt_at, last_success_at, last_status, last_error,
                    consecutive_failures, blocked_until, total_found, total_new)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source) DO UPDATE SET
                    last_attempt_at = excluded.last_attempt_at,
                    last_success_at = CASE WHEN ? THEN excluded.last_success_at
                                           ELSE source_state.last_success_at END,
                    last_status = excluded.last_status,
                    last_error = excluded.last_error,
                    consecutive_failures = CASE WHEN ? THEN 0
                                                ELSE source_state.consecutive_failures + 1 END,
                    blocked_until = excluded.blocked_until,
                    total_found = source_state.total_found + excluded.total_found,
                    total_new = source_state.total_new + excluded.total_new
                """,
                (
                    source,
                    to_iso(now),
                    to_iso(now) if success else None,
                    status,
                    error,
                    0 if success else 1,
                    blocked_until,
                    found,
                    new_listings,
                    success,
                    success,
                ),
            )
            self._conn.commit()

    def source_states(self) -> Dict[str, Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM source_state").fetchall()
        return {row["source"]: dict(row) for row in rows}

    def source_blocked_until(self, source: str) -> Optional[datetime]:
        with self._lock:
            row = self._conn.execute(
                "SELECT blocked_until FROM source_state WHERE source = ?", (source,)
            ).fetchone()
        return from_iso(row["blocked_until"]) if row else None

    # ------------------------------------------------------------------ app state

    def get_state(self, key: str, default: Optional[str] = None) -> Optional[str]:
        with self._lock:
            row = self._conn.execute("SELECT value FROM app_state WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def set_state(self, key: str, value: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO app_state (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
            self._conn.commit()

    # ------------------------------------------------------------------ subscribers

    def add_subscriber(
        self, chat_id: str, username: Optional[str] = None, reactivate: bool = True
    ) -> bool:
        """Subscribe a chat. Returns True when it was not already active.

        With ``reactivate=False`` a chat that unsubscribed stays unsubscribed;
        that is what the startup seeding from TELEGRAM_CHAT_ID wants.
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT active FROM subscribers WHERE chat_id = ?", (chat_id,)
            ).fetchone()
            if row is None:
                self._conn.execute(
                    "INSERT INTO subscribers (chat_id, username, subscribed_at, active) "
                    "VALUES (?, ?, ?, 1)",
                    (chat_id, username, to_iso(utcnow())),
                )
            elif row["active"] or not reactivate:
                return False
            else:
                self._conn.execute(
                    "UPDATE subscribers SET active = 1, username = COALESCE(?, username), "
                    "subscribed_at = ? WHERE chat_id = ?",
                    (username, to_iso(utcnow()), chat_id),
                )
            self._conn.commit()
            return True

    def remove_subscriber(self, chat_id: str) -> bool:
        """Unsubscribe a chat. Returns True when it was active."""
        with self._lock:
            cursor = self._conn.execute(
                "UPDATE subscribers SET active = 0 WHERE chat_id = ? AND active = 1", (chat_id,)
            )
            self._conn.commit()
            return cursor.rowcount > 0

    def active_subscribers(self) -> List[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT chat_id FROM subscribers WHERE active = 1 ORDER BY subscribed_at"
            ).fetchall()
        return [row["chat_id"] for row in rows]

    def count_subscribers(self) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM subscribers WHERE active = 1"
            ).fetchone()
        return int(row["n"])
