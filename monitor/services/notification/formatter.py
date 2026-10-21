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
from monitor.services.notification.message import NotificationMessage
from monitor.timeutil import format_local, utcnow

SOURCE_LABELS = {
    "zonaprop": "ZonaProp",
    "argenprop": "ArgenProp",
    "mercadolibre": "MercadoLibre",
    "cabaprop": "CabaProp",
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


def _plain_listing(listing: Listing, detected_at: Optional[datetime] = None) -> str:
    """Plain-text rendering, used for email and for the log fallback."""
    lines = [
        "New apartment",
        "",
        f"Source: {source_label(listing.source)}",
        "",
        f"Type: {listing.property_type.value}",
        f"Neighborhood: {listing.neighborhood or '-'}",
        f"Price: {format_money(listing.price, listing.currency)}",
        f"Area: {format_area(listing.area_m2)}",
    ]
    if listing.rooms:
        lines.append(f"Ambientes: {listing.rooms}")
    if listing.expensas:
        lines.append(
            f"Expensas: {format_money(listing.expensas, listing.expensas_currency or 'ARS')}"
        )
    if listing.address:
        lines.append(f"Address: {listing.address}")
    lines += ["", "Link:", listing.url, "", f"Detected: {format_local(detected_at or utcnow())}"]
    return "\n".join(lines)


def _email_row(label: str, value: str) -> str:
    return (
        '<tr><td style="padding:4px 12px 4px 0;color:#666;white-space:nowrap">'
        f"{escape(label)}</td>"
        f'<td style="padding:4px 0;color:#111"><b>{value}</b></td></tr>'
    )


def _email_listing_card(listing: Listing) -> str:
    rows = [
        _email_row("Type", escape(listing.property_type.value)),
        _email_row("Neighborhood", escape(listing.neighborhood or "-")),
        _email_row("Price", escape(format_money(listing.price, listing.currency))),
        _email_row("Area", escape(format_area(listing.area_m2))),
    ]
    if listing.rooms:
        rows.append(_email_row("Ambientes", str(listing.rooms)))
    if listing.expensas:
        rows.append(
            _email_row(
                "Expensas",
                escape(format_money(listing.expensas, listing.expensas_currency or "ARS")),
            )
        )
    if listing.address:
        rows.append(_email_row("Address", escape(listing.address)))
    rows.append(_email_row("Source", escape(source_label(listing.source))))
    return (
        '<div style="border:1px solid #e3e3e3;border-radius:8px;padding:16px;margin:0 0 16px 0">'
        f'<div style="font-size:16px;font-weight:600;margin-bottom:10px">'
        f'{escape(listing.title or listing.property_type.value)}</div>'
        f'<table style="border-collapse:collapse;font-size:14px">{"".join(rows)}</table>'
        f'<p style="margin:14px 0 0 0"><a href="{escape(listing.url)}" '
        'style="display:inline-block;padding:9px 16px;background:#1a73e8;color:#fff;'
        'border-radius:6px;text-decoration:none;font-size:14px">Open listing</a></p>'
        "</div>"
    )


def _email_document(heading: str, detected_at: datetime, body: str) -> str:
    return (
        "<div style=\"font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Arial,sans-serif;"
        'max-width:640px;margin:0 auto;padding:8px">'
        f'<h2 style="font-size:18px;margin:0 0 4px 0">🏠 {escape(heading)}</h2>'
        f'<p style="color:#666;font-size:13px;margin:0 0 18px 0">Detected: '
        f"{escape(format_local(detected_at))}</p>"
        f"{body}"
        '<p style="color:#999;font-size:12px;margin-top:20px">Sent by your CABA rental monitor.</p>'
        "</div>"
    )


def build_listing_message(
    listing: Listing, detected_at: Optional[datetime] = None
) -> NotificationMessage:
    when = detected_at or utcnow()
    price = format_money(listing.price, listing.currency)
    subject = "{type} in {hood} - {price}".format(
        type=listing.property_type.value, hood=listing.neighborhood or "CABA", price=price
    )
    return NotificationMessage(
        subject=subject,
        text=_plain_listing(listing, when),
        telegram_html=format_listing(listing, when),
        email_html=_email_document("New apartment", when, _email_listing_card(listing)),
    )


def build_group_message(
    items: Sequence[StoredListing], detected_at: Optional[datetime] = None
) -> NotificationMessage:
    when = detected_at or utcnow()
    heading = f"{len(items)} new apartments"
    body = "".join(_email_listing_card(item.listing) for item in items)
    text_blocks = [_plain_listing(item.listing, when) for item in items]
    return NotificationMessage(
        subject=heading,
        text=("\n\n" + "-" * 40 + "\n\n").join(text_blocks),
        telegram_html=format_group(items, when),
        email_html=_email_document(heading, when, body),
    )
