"""Older databases are upgraded in place on start."""

import sqlite3

from monitor.database import Database

OLD_LISTINGS = (
    "CREATE TABLE listings (id INTEGER PRIMARY KEY AUTOINCREMENT, "
    "dedupe_key TEXT NOT NULL UNIQUE, source TEXT NOT NULL, external_id TEXT NOT NULL, "
    "url TEXT NOT NULL, title TEXT, property_type TEXT, operation TEXT, neighborhood TEXT, "
    "raw_neighborhood TEXT, address TEXT, price REAL, currency TEXT, area_m2 REAL, "
    "rooms INTEGER, expensas REAL, expensas_currency TEXT, published_at TEXT, "
    "price_is_approximate INTEGER NOT NULL DEFAULT 0, matched INTEGER NOT NULL DEFAULT 0, "
    "reject_reason TEXT, first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL, "
    "notified_at TEXT, notify_attempts INTEGER NOT NULL DEFAULT 0, extra TEXT)"
)


def test_listings_stored_before_cities_existed_are_in_caba(tmp_path):
    path = str(tmp_path / "old.sqlite3")
    connection = sqlite3.connect(path)
    connection.execute(OLD_LISTINGS)
    connection.execute(
        "INSERT INTO listings (dedupe_key, source, external_id, url, neighborhood, "
        "property_type, operation, matched, first_seen_at, last_seen_at) VALUES "
        "('mercadolibre:1', 'mercadolibre', '1', 'https://x', 'Recoleta', 'Departamento', "
        "'Alquiler', 1, '2026-10-01T00:00:00+00:00', '2026-10-01T00:00:00+00:00')"
    )
    connection.commit()
    connection.close()

    database = Database(path)
    stored = database.pending_notifications()[0].listing
    assert (stored.city, stored.neighborhood) == ("CABA", "Recoleta")
    database.close()
    # A second start does not run the backfill again.
    Database(path).close()
