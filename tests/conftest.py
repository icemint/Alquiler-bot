import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

FIXTURE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


def load_fixture(name):
    with open(os.path.join(FIXTURE_DIR, name), encoding="utf-8") as handle:
        return handle.read()


@pytest.fixture
def fixture_loader():
    return load_fixture


@pytest.fixture
def resolver():
    from monitor.neighborhoods import NeighborhoodResolver

    return NeighborhoodResolver()


@pytest.fixture
def criteria():
    from monitor.config import SearchCriteria

    return SearchCriteria()


@pytest.fixture
def database(tmp_path):
    from monitor.database import Database

    db = Database(str(tmp_path / "test.sqlite3"))
    yield db
    db.close()


def make_listing(**overrides):
    """A listing that passes every filter unless a field is overridden."""
    from monitor.models.listing import Listing, Operation, PropertyType

    defaults = {
        "source": "mercadolibre",
        "external_id": "MLA1",
        "url": "https://departamento.mercadolibre.com.ar/MLA-1-depto-_JM",
        "title": "Depto Recoleta",
        "property_type": PropertyType.DEPARTAMENTO,
        "operation": Operation.ALQUILER,
        "neighborhood": "Recoleta",
        "address": "Junin 1200",
        "price": 800_000.0,
        "currency": "ARS",
        "area_m2": 55.0,
        "rooms": 2,
        "expensas": 150_000.0,
    }
    defaults.update(overrides)
    return Listing(**defaults)
