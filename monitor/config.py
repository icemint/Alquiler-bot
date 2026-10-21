"""Configuration. Secrets come from the environment only; search criteria may
come from the environment or from a JSON config file (CONFIG_FILE)."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from monitor.models.listing import PropertyType
from monitor.neighborhoods import NeighborhoodResolver

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)

_FILE_CONFIG: Dict[str, Any] = {}


def _load_file_config() -> Dict[str, Any]:
    path = os.getenv("CONFIG_FILE")
    if not path:
        return {}
    if not os.path.exists(path):
        raise ConfigError(f"CONFIG_FILE={path} does not exist")
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ConfigError("CONFIG_FILE must contain a JSON object")
    return data


class ConfigError(RuntimeError):
    pass


def _raw(name: str) -> Optional[str]:
    """Environment wins over the config file."""
    value = os.getenv(name)
    if value is not None and value.strip() != "":
        return value.strip()
    file_value = _FILE_CONFIG.get(name)
    if file_value is None:
        return None
    if isinstance(file_value, (list, tuple)):
        return ",".join(str(item) for item in file_value)
    return str(file_value).strip() or None


def _str(name: str, default: str) -> str:
    value = _raw(name)
    return default if value is None else value


def _int(name: str, default: int) -> int:
    value = _raw(name)
    if value is None:
        return default
    try:
        return int(float(value))
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number, got {value!r}") from exc


def _float(name: str, default: float) -> float:
    value = _raw(name)
    if value is None:
        return default
    try:
        return float(value)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number, got {value!r}") from exc


def _bool(name: str, default: bool) -> bool:
    value = _raw(name)
    if value is None:
        return default
    return value.lower() in ("1", "true", "yes", "y", "on", "si")


def _list(name: str, default: List[str]) -> List[str]:
    value = _raw(name)
    if value is None:
        return list(default)
    return [item.strip() for item in value.split(",") if item.strip()]


# The last bedroom option means "this many or more".
BEDROOMS_OR_MORE = 4
FURNISHED_OPTIONS = ("any", "yes", "no")


@dataclass
class SearchCriteria:
    max_rent: float = 1_000_000.0
    min_area_m2: float = 50.0
    currency: str = "ARS"
    neighborhoods: List[str] = field(default_factory=lambda: ["Recoleta", "Barrio Norte"])
    property_types: List[PropertyType] = field(
        default_factory=lambda: [PropertyType.DEPARTAMENTO, PropertyType.CASA, PropertyType.PH]
    )
    # Expensas are explicitly NOT part of the price filter.
    include_expensas_in_price: bool = False
    # Accepted bedroom counts (0 = monoambiente, BEDROOMS_OR_MORE = that or
    # more). Empty means any.
    bedrooms: List[int] = field(default_factory=list)
    # "yes": only listings that say they are furnished; "no": drop those
    # that say so; "any": no filter.
    furnished: str = "any"

    def describe(self) -> str:
        # Only the amount takes Argentine thousands separators - applying the
        # replacement to the whole sentence would also eat the list commas.
        price = f"{self.max_rent:,.0f}".replace(",", ".")
        types = "/".join(t.value for t in self.property_types)
        hoods = ", ".join(self.neighborhoods)
        text = (
            f"alquiler | {types} | {hoods} | area >= {self.min_area_m2:g} m2 | "
            f"price <= {price} {self.currency} (expensas excluded)"
        )
        if self.bedrooms:
            text += " | bedrooms " + ",".join(str(b) for b in self.bedrooms)
        if self.furnished != "any":
            text += f" | furnished={self.furnished}"
        return text

    def to_dict(self) -> Dict[str, Any]:
        """The part a subscriber can change, as stored in the database."""
        return {
            "max_rent": self.max_rent,
            "min_area_m2": self.min_area_m2,
            "neighborhoods": list(self.neighborhoods),
            "property_types": [t.value for t in self.property_types],
            "bedrooms": list(self.bedrooms),
            "furnished": self.furnished,
        }

    def with_changes(self, data: Dict[str, Any]) -> SearchCriteria:
        """A copy with the stored fields applied; anything absent or invalid
        keeps this criteria's value, so an old row never breaks a search."""
        types: List[PropertyType] = []
        for value in data.get("property_types") or []:
            try:
                types.append(PropertyType(value))
            except ValueError:
                continue
        try:
            max_rent = float(data.get("max_rent") or self.max_rent)
            min_area = float(data.get("min_area_m2") or self.min_area_m2)
        except (TypeError, ValueError):
            max_rent, min_area = self.max_rent, self.min_area_m2
        # An empty list is a real choice ("any"), so only a missing key
        # keeps the current value.
        bedrooms = list(self.bedrooms)
        if isinstance(data.get("bedrooms"), list):
            bedrooms = sorted(
                {b for b in data["bedrooms"] if isinstance(b, int) and 0 <= b <= BEDROOMS_OR_MORE}
            )
        furnished = data.get("furnished")
        return SearchCriteria(
            max_rent=max_rent,
            min_area_m2=min_area,
            currency=self.currency,
            neighborhoods=list(data.get("neighborhoods") or self.neighborhoods),
            property_types=types or list(self.property_types),
            include_expensas_in_price=self.include_expensas_in_price,
            bedrooms=bedrooms,
            furnished=furnished if furnished in FURNISHED_OPTIONS else self.furnished,
        )

    @staticmethod
    def union(searches: List[SearchCriteria]) -> SearchCriteria:
        """The widest search covering every one given: what to ask the sites
        for, so each subscriber's own filter still sees all candidates."""
        first = searches[0]
        neighborhoods: List[str] = []
        types: List[PropertyType] = []
        for search in searches:
            neighborhoods.extend(n for n in search.neighborhoods if n not in neighborhoods)
            types.extend(t for t in search.property_types if t not in types)
        # One subscriber without a bedroom filter makes the union unfiltered.
        bedrooms: List[int] = []
        if all(search.bedrooms for search in searches):
            bedrooms = sorted({b for search in searches for b in search.bedrooms})
        furnished = {search.furnished for search in searches}
        return SearchCriteria(
            max_rent=max(s.max_rent for s in searches),
            min_area_m2=min(s.min_area_m2 for s in searches),
            currency=first.currency,
            neighborhoods=neighborhoods,
            property_types=types,
            include_expensas_in_price=first.include_expensas_in_price,
            bedrooms=bedrooms,
            furnished=furnished.pop() if len(furnished) == 1 else "any",
        )


@dataclass
class TelegramConfig:
    bot_token: Optional[str] = None
    chat_id: Optional[str] = None
    enabled: bool = True
    commands_enabled: bool = True
    # Above this many new listings in one cycle, send a single grouped digest
    # instead of one message per listing.
    group_threshold: int = 5
    max_notifications_per_cycle: int = 20

    @property
    def configured(self) -> bool:
        # Recipients are the chats that sent /start; chat_id only seeds the
        # owner's chat as the first subscriber.
        return bool(self.bot_token)


@dataclass
class EmailConfig:
    host: str = "smtp.gmail.com"
    port: int = 587
    username: Optional[str] = None
    password: Optional[str] = None
    from_address: Optional[str] = None
    from_name: str = "CABA Rental Monitor"
    recipients: List[str] = field(default_factory=list)
    use_starttls: bool = True
    use_ssl: bool = False
    timeout: float = 20.0
    enabled: bool = True
    subject_prefix: str = "[Alquiler] "

    @property
    def configured(self) -> bool:
        return bool(self.host and self.from_address and self.recipients)


@dataclass
class SourceConfig:
    name: str
    enabled: bool = True
    # Some sources must be polled less often than the global cycle because
    # they rate-limit aggressively (ArgenProp's WAF does).
    min_interval_seconds: int = 0
    max_pages: int = 2
    request_delay_seconds: float = 2.0
    options: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Config:
    criteria: SearchCriteria
    telegram: TelegramConfig
    sources: Dict[str, SourceConfig]
    email: EmailConfig = field(default_factory=EmailConfig)
    database_path: str = "data/listings.sqlite3"
    check_interval_seconds: int = 300
    http_timeout_seconds: float = 20.0
    http_connect_timeout_seconds: float = 10.0
    http_max_retries: int = 3
    user_agent: str = DEFAULT_USER_AGENT
    log_level: str = "INFO"
    log_format: str = "text"
    # On a fresh database the first cycle records everything as already seen
    # instead of firing dozens of notifications at once.
    notify_on_first_run: bool = False
    neighborhood_map_file: Optional[str] = None

    @property
    def resolver(self) -> NeighborhoodResolver:
        return NeighborhoodResolver.from_file(self.neighborhood_map_file)

    def enabled_sources(self) -> List[str]:
        return [name for name, cfg in self.sources.items() if cfg.enabled]


def _parse_property_types(values: List[str]) -> List[PropertyType]:
    from monitor.scrapers.parsing import parse_property_type

    parsed: List[PropertyType] = []
    for value in values:
        ptype = parse_property_type(value)
        if ptype is PropertyType.OTHER:
            raise ConfigError(f"Unknown property type {value!r}; expected Departamento, Casa or PH")
        if ptype not in parsed:
            parsed.append(ptype)
    if not parsed:
        raise ConfigError("PROPERTY_TYPES must not be empty")
    return parsed


def load_config() -> Config:
    global _FILE_CONFIG
    _FILE_CONFIG = _load_file_config()

    resolver = NeighborhoodResolver.from_file(_raw("NEIGHBORHOOD_MAP_FILE"))
    neighborhoods = _list("NEIGHBORHOODS", ["Recoleta", "Barrio Norte"])
    canonical: List[str] = []
    for name in neighborhoods:
        resolved = resolver.from_label(name)
        if not resolved:
            raise ConfigError(
                f"Neighborhood {name!r} is not in the neighborhood map. Add it to "
                "NEIGHBORHOOD_MAP_FILE with the per-source slugs first."
            )
        if resolved not in canonical:
            canonical.append(resolved)

    criteria = SearchCriteria(
        max_rent=_float("MAX_RENT", 1_000_000.0),
        min_area_m2=_float("MIN_AREA", 50.0),
        currency=_str("CURRENCY", "ARS").upper(),
        neighborhoods=canonical,
        property_types=_parse_property_types(
            _list("PROPERTY_TYPES", ["Departamento", "Casa", "PH"])
        ),
    )

    telegram = TelegramConfig(
        bot_token=_raw("TELEGRAM_BOT_TOKEN"),
        chat_id=_raw("TELEGRAM_CHAT_ID"),
        enabled=_bool("TELEGRAM_ENABLED", True),
        commands_enabled=_bool("TELEGRAM_COMMANDS_ENABLED", True),
        group_threshold=_int("TELEGRAM_GROUP_THRESHOLD", 5),
        max_notifications_per_cycle=_int("MAX_NOTIFICATIONS_PER_CYCLE", 20),
    )

    smtp_user = _raw("SMTP_USER")
    email_port = _int("SMTP_PORT", 587)
    email = EmailConfig(
        host=_str("SMTP_HOST", "smtp.gmail.com"),
        port=email_port,
        username=smtp_user,
        password=_raw("SMTP_PASSWORD"),
        # Gmail rejects a From that is not the authenticated account, so the
        # SMTP user is the natural default sender.
        from_address=_raw("EMAIL_FROM") or smtp_user,
        from_name=_str("EMAIL_FROM_NAME", "CABA Rental Monitor"),
        recipients=_list("EMAIL_TO", []),
        use_ssl=_bool("SMTP_SSL", email_port == 465),
        use_starttls=_bool("SMTP_STARTTLS", True),
        timeout=_float("SMTP_TIMEOUT", 20.0),
        enabled=_bool("EMAIL_ENABLED", True),
        subject_prefix=_str("EMAIL_SUBJECT_PREFIX", "[Alquiler] "),
    )

    interval_minutes = _float("CHECK_INTERVAL", 5.0)
    check_interval_seconds = _int("CHECK_INTERVAL_SECONDS", int(interval_minutes * 60))

    enabled_sources = [
        s.lower() for s in _list("SOURCES", ["mercadolibre", "argenprop", "cabaprop"])
    ]
    sources = {
        "mercadolibre": SourceConfig(
            name="mercadolibre",
            enabled="mercadolibre" in enabled_sources,
            max_pages=_int("MERCADOLIBRE_MAX_PAGES", 3),
            request_delay_seconds=_float("MERCADOLIBRE_REQUEST_DELAY", 2.0),
            options={"mode": _str("MERCADOLIBRE_MODE", "html")},
        ),
        "argenprop": SourceConfig(
            name="argenprop",
            enabled="argenprop" in enabled_sources,
            # ArgenProp's AWS WAF answers 202 with an empty body once it
            # dislikes the request rate, and the ban outlives short backoffs.
            min_interval_seconds=_int("ARGENPROP_MIN_INTERVAL_SECONDS", 600),
            max_pages=_int("ARGENPROP_MAX_PAGES", 1),
            request_delay_seconds=_float("ARGENPROP_REQUEST_DELAY", 20.0),
        ),
        "cabaprop": SourceConfig(
            name="cabaprop",
            enabled="cabaprop" in enabled_sources,
            # One JSON request per cycle covers every new listing (newest first).
            min_interval_seconds=_int("CABAPROP_MIN_INTERVAL_SECONDS", 0),
            max_pages=_int("CABAPROP_MAX_PAGES", 1),
            request_delay_seconds=_float("CABAPROP_REQUEST_DELAY", 5.0),
            options={"page_size": _int("CABAPROP_PAGE_SIZE", 30)},
        ),
        "zonaprop": SourceConfig(
            name="zonaprop",
            enabled="zonaprop" in enabled_sources,
            min_interval_seconds=_int("ZONAPROP_MIN_INTERVAL_SECONDS", 900),
            max_pages=_int("ZONAPROP_MAX_PAGES", 1),
            request_delay_seconds=_float("ZONAPROP_REQUEST_DELAY", 5.0),
            options={"strategy": _str("ZONAPROP_STRATEGY", "disabled")},
        ),
    }

    return Config(
        criteria=criteria,
        telegram=telegram,
        email=email,
        sources=sources,
        database_path=_str("DATABASE_PATH", "data/listings.sqlite3"),
        check_interval_seconds=check_interval_seconds,
        http_timeout_seconds=_float("HTTP_TIMEOUT", 20.0),
        http_connect_timeout_seconds=_float("HTTP_CONNECT_TIMEOUT", 10.0),
        http_max_retries=_int("HTTP_MAX_RETRIES", 3),
        user_agent=_str("USER_AGENT", DEFAULT_USER_AGENT),
        log_level=_str("LOG_LEVEL", "INFO").upper(),
        log_format=_str("LOG_FORMAT", "text").lower(),
        notify_on_first_run=_bool("NOTIFY_ON_FIRST_RUN", False),
        neighborhood_map_file=_raw("NEIGHBORHOOD_MAP_FILE"),
    )
