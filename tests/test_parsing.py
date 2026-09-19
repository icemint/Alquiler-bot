"""Unit tests for the shared text parsing helpers."""

import pytest

from monitor.models.listing import Operation, PropertyType
from monitor.scrapers.parsing import (
    parse_area,
    parse_number,
    parse_operation,
    parse_price,
    parse_property_type,
    parse_rooms,
)


@pytest.mark.parametrize(
    "text,expected",
    [
        ("1.234.567", 1234567.0),
        ("680.000", 680000.0),
        ("38,5", 38.5),
        ("1.234.567,89", 1234567.89),
        ("480", 480.0),
    ],
)
def test_parse_number_uses_argentine_format(text, expected):
    assert parse_number(text) == expected


@pytest.mark.parametrize(
    "text,amount,currency",
    [
        ("$ 680.000", 680000.0, "ARS"),
        ("ARS 1.000.000", 1000000.0, "ARS"),
        ("U$S 1.200", 1200.0, "USD"),
        ("USD 550", 550.0, "USD"),
        ("us$ 480", 480.0, "USD"),
    ],
)
def test_parse_price_detects_currency(text, amount, currency):
    parsed_amount, parsed_currency, _ = parse_price(text)
    assert parsed_amount == amount
    assert parsed_currency == currency


def test_parse_price_flags_desde_as_approximate():
    amount, currency, approximate = parse_price("Desde $ 500.000")
    assert (amount, currency) == (500000.0, "ARS")
    assert approximate is True


def test_parse_price_refuses_mixed_currencies():
    # "Alquiler: $ 680.000 ó u$s 480" cannot be attributed to one currency.
    amount, currency, approximate = parse_price("Alquiler: $ 680.000 ó u$s 480.")
    assert amount is None
    assert currency is None
    assert approximate is True


def test_parse_price_handles_missing_and_unparseable():
    assert parse_price(None) == (None, None, False)
    assert parse_price("Consultar")[0] is None


@pytest.mark.parametrize(
    "text,expected",
    [
        ("38 m² cubie.", 38.0),
        ("72 m² totales", 72.0),
        ("1.200 m2", 1200.0),
        ("38,5 m²", 38.5),
        ("1 baño", None),
        ("", None),
    ],
)
def test_parse_area(text, expected):
    assert parse_area(text) == expected


@pytest.mark.parametrize(
    "text,expected", [("2 ambientes", 2), ("1 amb.", 1), ("Monoambiente", 1), ("1 baño", None)]
)
def test_parse_rooms(text, expected):
    assert parse_rooms(text) == expected


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Departamento en alquiler", PropertyType.DEPARTAMENTO),
        ("PH en alquiler", PropertyType.PH),
        ("Casa en alquiler", PropertyType.CASA),
        ("Local comercial", PropertyType.OTHER),
        ("Depto 2 ambientes", PropertyType.DEPARTAMENTO),
    ],
)
def test_parse_property_type(text, expected):
    assert parse_property_type(text) == expected


def test_parse_property_type_does_not_match_ph_inside_words():
    assert parse_property_type("Oficina en Philips 100") is PropertyType.OTHER


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Departamento en alquiler", Operation.ALQUILER),
        ("Casa en alquiler temporal", Operation.ALQUILER_TEMPORAL),
        ("Departamento en venta", Operation.VENTA),
        ("", Operation.OTHER),
    ],
)
def test_parse_operation(text, expected):
    assert parse_operation(text) == expected
