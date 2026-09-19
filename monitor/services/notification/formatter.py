"""Message formatting.

Kept free of any Telegram API details so the same text can go to another
channel later: the notifier decides how to deliver, the formatter decides what
to say.
"""

from __future__ import annotations

from datetime import datetime
from html import escape
from typing import List, Optional, Sequence

from monitor.database import StoredListing
from monitor.models.listing import Listing
from monitor.timeutil import format_local, utcnow

SOURCE_LABELS = {
    "zonaprop": "ZonaProp",
    "argenprop": "ArgenProp",
    "mercadolibre": "MercadoLibre",
}


def source_label(source: str) -> str:
    return SOURCE_LABELS.get(source, source.title())


def format_money(amount: Optional[float], currency: Optional[str] = "ARS") -> str:
    """Argentine formatting: $ 1.234.567 / US$ 1.200."""
    if amount is None:
        return "-"
    symbol = "US$" if (currency or "").upper() == "USD" else "$"
    formatted = f"{float(amount):,.0f}".replace(",", ".")
    return f"{symbol} {formatted}"


def format_area(area: Optional[float]) -> str:
    if area is None:
        return "-"
    if float(area).is_integer():
        return f"{area:.0f} m²"
    return f"{area:.1f} m²"


def format_listing(listing: Listing, detected_at: Optional[datetime] = None) -> str:
    """Single-listing notification, HTML formatted for Telegram."""
    lines = [
        "🏠 <b>New apartment</b>",
        "",
        f"Source: {escape(source_label(listing.source))}",
        "",
        f"Type: {escape(listing.property_type.value)}",
        "Neighborhood: {0}".format(escape(listing.neighborhood or "-")),
        f"Price: {format_money(listing.price, listing.currency)}",
        f"Area: {format_area(listing.area_m2)}",
    ]
    if listing.rooms:
        lines.append(f"Ambientes: {listing.rooms}")
    if listing.expensas:
        lines.append(
            "Expensas: {0}".format(
                format_money(listing.expensas, listing.expensas_currency or "ARS")
            )
        )
    if listing.address:
        lines.append(f"Address: {escape(listing.address)}")
    lines.extend(
        [
            "",
            "Link:",
            escape(listing.url),
            "",
            f"Detected: {format_local(detected_at or utcnow())}",
        ]
    )
    return "\n".join(lines)


def chunk_for_group(
    items: Sequence[StoredListing], max_length: int = 4000
) -> List[List[StoredListing]]:
    """Split a digest into batches that each fit in one Telegram message.

    Truncating a digest would silently drop listings the user was told about,
    so the batch shrinks until the rendered message fits instead.
    """
    chunks: List[List[StoredListing]] = []
    current: List[StoredListing] = []
    for item in items:
        candidate = current + [item]
        if current and len(format_group(candidate)) > max_length:
            chunks.append(current)
            current = [item]
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


def format_group(items: Sequence[StoredListing], detected_at: Optional[datetime] = None) -> str:
    """Digest used when a single cycle turns up many new listings."""
    when = format_local(detected_at or utcnow())
    lines: List[str] = [
        f"🏠 <b>{len(items)} new apartments</b>",
        f"Detected: {when}",
        "",
    ]
    for index, item in enumerate(items, start=1):
        listing = item.listing
        details = [
            format_money(listing.price, listing.currency),
            format_area(listing.area_m2),
        ]
        if listing.rooms:
            details.append(f"{listing.rooms} amb.")
        if listing.expensas:
            details.append(
                "expensas {0}".format(
                    format_money(listing.expensas, listing.expensas_currency or "ARS")
                )
            )
        lines.append(
            "{index}. <b>{type} · {hood}</b> — {details}".format(
                index=index,
                type=escape(listing.property_type.value),
                hood=escape(listing.neighborhood or "-"),
                details=escape(" · ".join(details)),
            )
        )
        if listing.address:
            lines.append(f"   {escape(listing.address)}")
        lines.append(f"   {escape(source_label(listing.source))} · {escape(listing.url)}")
        lines.append("")
    return "\n".join(lines).strip()
