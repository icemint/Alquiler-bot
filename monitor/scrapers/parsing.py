"""Text parsing helpers shared by the scrapers.

Argentine sites write numbers as 1.234.567,89 and mix currencies in the same
listing ("Alquiler: $ 680.000 o u$s 480"), so every helper here returns None
rather than a guess when the value is not unambiguous.
"""

from __future__ import annotations

import re
from typing import Optional, Tuple

from monitor.models.listing import Operation, PropertyType

# "desde", "a partir de", "consultar" -> the displayed number is not the
# actual rent of a single unit.
APPROXIMATE_PRICE_MARKERS = (
    "desde",
    "a partir de",
    "hasta",
    "consultar",
    "a convenir",
    "precio a consultar",
)

_USD_MARKERS = ("u$s", "usd", "u$d", "dolar", "dólar", "us$")
_ARS_MARKERS = ("ars", "$", "peso")

_NUMBER_RE = re.compile(r"\d{1,3}(?:\.\d{3})+(?:,\d+)?|\d+(?:,\d+)?|\d+(?:\.\d+)?")


def normalize_spaces(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    cleaned = re.sub(r"\s+", " ", value.replace("\xa0", " ")).strip()
    return cleaned or None


def parse_number(value: str) -> Optional[float]:
    """Parse an Argentine-formatted number. '1.234.567,89' -> 1234567.89."""
    if value is None:
        return None
    match = _NUMBER_RE.search(value.replace("\xa0", " "))
    if not match:
        return None
    token = match.group(0)
    if "." in token and "," in token:
        token = token.replace(".", "").replace(",", ".")
    elif "," in token:
        # A comma is a decimal separator in es-AR.
        token = token.replace(",", ".")
    elif token.count(".") >= 1:
        groups = token.split(".")
        # 1.234 / 1.234.567 are thousands separators; 38.5 is a decimal.
        if all(len(g) == 3 for g in groups[1:]) and len(groups[0]) <= 3:
            token = token.replace(".", "")
    try:
        return float(token)
    except ValueError:
        return None


def detect_currency(text: str) -> Optional[str]:
    lowered = (text or "").lower()
    if any(marker in lowered for marker in _USD_MARKERS):
        return "USD"
    if any(marker in lowered for marker in _ARS_MARKERS):
        return "ARS"
    return None


def parse_price(text: Optional[str]) -> Tuple[Optional[float], Optional[str], bool]:
    """Return (amount, currency, is_approximate).

    A text that mentions both currencies (e.g. "$ 680.000 o u$s 480") is
    ambiguous for a currency-sensitive filter, so it yields (None, None, ...)
    unless the amount clearly belongs to one of them.
    """
    if not text:
        return None, None, False
    cleaned = normalize_spaces(text) or ""
    lowered = cleaned.lower()
    approximate = any(marker in lowered for marker in APPROXIMATE_PRICE_MARKERS)

    has_usd = any(marker in lowered for marker in _USD_MARKERS)
    # Strip the dollar markers first, otherwise their own "$" looks like ARS.
    without_usd = lowered
    for marker in ("u$s", "u$d", "us$", "usd"):
        without_usd = without_usd.replace(marker, " ")
    has_ars = "$" in without_usd or "ars" in without_usd or "peso" in without_usd
    if has_usd and has_ars:
        # Both currencies quoted in the same field: not reliable.
        return None, None, True
    amount = parse_number(cleaned)
    if amount is None:
        return None, detect_currency(cleaned), approximate
    currency = "USD" if has_usd else ("ARS" if has_ars else None)
    return amount, currency, approximate


def parse_area(text: Optional[str]) -> Optional[float]:
    """Extract square meters from strings like '38 m² cubie.' or '120 m2'."""
    if not text:
        return None
    cleaned = (normalize_spaces(text) or "").lower().replace("mts", "m").replace("m2", "m²")
    match = re.search(r"(\d{1,3}(?:\.\d{3})*(?:,\d+)?|\d+(?:[.,]\d+)?)\s*m²", cleaned)
    if not match:
        return None
    value = parse_number(match.group(1))
    if value is None or value <= 0 or value > 100_000:
        return None
    return value


def parse_rooms(text: Optional[str]) -> Optional[int]:
    """Number of ambientes. 'Monoambiente' counts as 1."""
    if not text:
        return None
    lowered = (normalize_spaces(text) or "").lower()
    if "monoambiente" in lowered:
        return 1
    match = re.search(r"(\d+)\s*(?:amb|ambiente)", lowered)
    if match:
        return int(match.group(1))
    return None


def bedrooms_from_rooms(rooms: Optional[int]) -> Optional[int]:
    """Dormitorios when a site only gives ambientes: every ambiente but the
    living room, so a monoambiente has none. Only a fallback - a structured
    bedroom count always wins."""
    if not rooms:
        return None
    return max(rooms - 1, 0)


# "no amoblado" contains "amoblado", so the negative forms are checked first.
_UNFURNISHED_RE = re.compile(
    r"\b(?:sin\s+muebles|sin\s+amoblar|sin\s+amueblar|no\s+(?:est[aá]\s+)?amoblad[oa]"
    r"|no\s+(?:est[aá]\s+)?amueblad[oa]|desamoblad[oa]|unfurnished)\b"
)
# Semi-amoblado counts as furnished: there is furniture to live with.
_FURNISHED_RE = re.compile(r"\b(?:amoblad[oa]s?|amueblad[oa]s?|con\s+muebles|furnished)\b")


def parse_furnished(*texts: Optional[str]) -> Optional[bool]:
    """Whether the title or description says the place is (un)furnished.

    Sites rarely have a structured field for it, but an ad that says
    "amoblado" means it. Silence stays None rather than "unfurnished".
    """
    text = " ".join(t for t in texts if t).lower()
    if not text:
        return None
    if _UNFURNISHED_RE.search(text):
        return False
    if _FURNISHED_RE.search(text):
        return True
    return None


def parse_property_type(text: Optional[str]) -> PropertyType:
    if not text:
        return PropertyType.OTHER
    lowered = (normalize_spaces(text) or "").lower()
    # PH first: "ph" is a substring-prone token, so match it on word bounds.
    if re.search(r"\bph\b", lowered) or "propiedad horizontal" in lowered:
        return PropertyType.PH
    if "departamento" in lowered or "depto" in lowered or lowered == "dep":
        return PropertyType.DEPARTAMENTO
    if "casa" in lowered or "chalet" in lowered:
        return PropertyType.CASA
    return PropertyType.OTHER


def parse_operation(text: Optional[str]) -> Operation:
    if not text:
        return Operation.OTHER
    lowered = (normalize_spaces(text) or "").lower()
    if "temporal" in lowered or "temporario" in lowered:
        return Operation.ALQUILER_TEMPORAL
    if "alquiler" in lowered or "alquila" in lowered or "renta" in lowered:
        return Operation.ALQUILER
    if "venta" in lowered or "vende" in lowered:
        return Operation.VENTA
    return Operation.OTHER
