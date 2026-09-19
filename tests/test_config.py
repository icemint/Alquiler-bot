"""Configuration loading from environment variables and config file."""

import json

import pytest

from monitor.config import ConfigError, load_config
from monitor.models.listing import PropertyType

ENV_KEYS = [
    "CONFIG_FILE",
    "MAX_RENT",
    "MIN_AREA",
    "CHECK_INTERVAL",
    "CHECK_INTERVAL_SECONDS",
    "NEIGHBORHOODS",
    "PROPERTY_TYPES",
    "SOURCES",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
    "DATABASE_PATH",
    "NOTIFY_ON_FIRST_RUN",
    "CURRENCY",
    "NEIGHBORHOOD_MAP_FILE",
]


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for key in ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


def test_defaults_match_the_brief():
    config = load_config()
    assert config.criteria.max_rent == 1_000_000
    assert config.criteria.min_area_m2 == 50
    assert config.criteria.currency == "ARS"
    assert config.criteria.neighborhoods == ["Recoleta", "Barrio Norte"]
    assert config.criteria.property_types == [
        PropertyType.DEPARTAMENTO,
        PropertyType.CASA,
        PropertyType.PH,
    ]
    assert config.check_interval_seconds == 300
    assert config.criteria.include_expensas_in_price is False


def test_environment_overrides_defaults(monkeypatch):
    monkeypatch.setenv("MAX_RENT", "750000")
    monkeypatch.setenv("MIN_AREA", "65")
    monkeypatch.setenv("CHECK_INTERVAL", "10")
    monkeypatch.setenv("PROPERTY_TYPES", "Departamento,PH")
    monkeypatch.setenv("NEIGHBORHOODS", "Recoleta")

    config = load_config()
    assert config.criteria.max_rent == 750_000
    assert config.criteria.min_area_m2 == 65
    assert config.check_interval_seconds == 600
    assert config.criteria.property_types == [PropertyType.DEPARTAMENTO, PropertyType.PH]
    assert config.criteria.neighborhoods == ["Recoleta"]


def test_secrets_are_read_from_the_environment(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    config = load_config()
    assert config.telegram.configured is True


def test_missing_telegram_credentials_are_not_fatal():
    assert load_config().telegram.configured is False


def test_config_file_supplies_search_criteria(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps({"MAX_RENT": 850000, "MIN_AREA": 55, "NEIGHBORHOODS": ["Barrio Norte"]}),
        encoding="utf-8",
    )
    monkeypatch.setenv("CONFIG_FILE", str(path))

    config = load_config()
    assert config.criteria.max_rent == 850_000
    assert config.criteria.min_area_m2 == 55
    assert config.criteria.neighborhoods == ["Barrio Norte"]


def test_environment_wins_over_the_config_file(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"MAX_RENT": 850000}), encoding="utf-8")
    monkeypatch.setenv("CONFIG_FILE", str(path))
    monkeypatch.setenv("MAX_RENT", "600000")
    assert load_config().criteria.max_rent == 600_000


def test_unknown_neighborhood_is_rejected_with_guidance(monkeypatch):
    monkeypatch.setenv("NEIGHBORHOODS", "Villa Crespo")
    with pytest.raises(ConfigError) as error:
        load_config()
    assert "neighborhood map" in str(error.value).lower()


def test_unknown_property_type_is_rejected(monkeypatch):
    monkeypatch.setenv("PROPERTY_TYPES", "Cochera")
    with pytest.raises(ConfigError):
        load_config()


def test_invalid_number_is_rejected(monkeypatch):
    monkeypatch.setenv("MAX_RENT", "cien mil")
    with pytest.raises(ConfigError):
        load_config()


def test_sources_can_be_selected(monkeypatch):
    monkeypatch.setenv("SOURCES", "mercadolibre")
    config = load_config()
    assert config.enabled_sources() == ["mercadolibre"]


def test_zonaprop_is_not_enabled_by_default():
    assert "zonaprop" not in load_config().enabled_sources()


def test_custom_neighborhood_map_file(tmp_path, monkeypatch):
    path = tmp_path / "hoods.json"
    path.write_text(
        json.dumps(
            {
                "Palermo Hollywood": {
                    "aliases": ["palermo hollywood"],
                    "slugs": {"argenprop": ["palermo-hollywood"]},
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("NEIGHBORHOOD_MAP_FILE", str(path))
    monkeypatch.setenv("NEIGHBORHOODS", "Palermo Hollywood")
    config = load_config()
    assert config.criteria.neighborhoods == ["Palermo Hollywood"]
    assert config.resolver.from_slug("argenprop", "palermo-hollywood") == "Palermo Hollywood"


def test_criteria_description_is_human_readable():
    text = load_config().criteria.describe()
    assert "expensas excluded" in text
    assert "Recoleta" in text


def test_description_keeps_list_commas_and_formats_the_amount(monkeypatch):
    monkeypatch.setenv("NEIGHBORHOODS", "Recoleta,Barrio Norte")
    text = load_config().criteria.describe()
    assert "Recoleta, Barrio Norte" in text
    assert "1.000.000 ARS" in text
