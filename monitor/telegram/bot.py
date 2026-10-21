"""Telegram command handling (/start, /settings, /stop, /status).

Runs in its own daemon thread with long polling. Anyone can talk to the bot:
/start subscribes the chat to new-listing notifications and /stop ends that.
/settings edits the chat's own search with inline buttons; prices and areas
can also be typed. /status shows when the sites were last checked and the
chat's own filters, never who is subscribed.
"""

from __future__ import annotations

import logging
import re
import threading
from dataclasses import dataclass
from html import escape
from typing import Any, Dict, List, Optional

import requests

from monitor.config import BEDROOMS_OR_MORE, Config, SearchCriteria
from monitor.database import Database
from monitor.logging_setup import log_context
from monitor.models.listing import PropertyType
from monitor.neighborhoods import NeighborhoodResolver, barrio_key, normalize_text, split_key
from monitor.services.notification.formatter import source_label
from monitor.services.subscriptions import criteria_for, save_criteria
from monitor.timeutil import format_local, from_iso

logger = logging.getLogger("monitor.telegram")

TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"
OFFSET_KEY = "telegram_update_offset"
POLL_TIMEOUT_SECONDS = 25
# Set while the bot waits for typed input: "awaiting_input:<chat_id>" ->
# "rent", "area" or "search:<city index>" (a barrio name).
AWAITING_KEY = "awaiting_input:{chat_id}"
# The last barrio search a chat typed, so toggling a result can redraw it.
SEARCH_KEY = "barrio_search:{chat_id}"
BARRIOS_PER_PAGE = 24

RENT_PRESETS = [500_000, 750_000, 1_000_000, 1_500_000, 2_000_000]
AREA_PRESETS = [30, 40, 50, 60, 80, 100]
RENT_RANGE = (10_000, 100_000_000)
AREA_RANGE = (10, 2_000)
SELECTABLE_TYPES = [PropertyType.DEPARTAMENTO, PropertyType.CASA, PropertyType.PH]
COMMANDS = [
    ("start", "Subscribe and see your search"),
    ("settings", "Change your search"),
    ("status", "Last check and your filters"),
    ("stop", "Unsubscribe"),
]

Keyboard = List[List[Dict[str, str]]]


@dataclass
class Reply:
    text: str
    keyboard: Optional[Keyboard] = None
    # Shown briefly on top of the chat when answering a button press.
    toast: Optional[str] = None


FURNISHED_LABELS = {"any": "Any", "yes": "Furnished only", "no": "Unfurnished only"}


def bedrooms_label(value: int) -> str:
    if value == 0:
        return "Mono"
    return f"{value}+" if value == BEDROOMS_OR_MORE else str(value)


def describe_bedrooms(criteria: SearchCriteria) -> str:
    return ", ".join(bedrooms_label(b) for b in criteria.bedrooms) or "any"


def _amount(value: float) -> str:
    return f"{value:,.0f}".replace(",", ".")


def _button(text: str, data: str) -> Dict[str, str]:
    return {"text": text, "callback_data": data}


def _rows(buttons: List[Dict[str, str]], per_row: int) -> Keyboard:
    return [buttons[i : i + per_row] for i in range(0, len(buttons), per_row)]


def parse_number(text: str) -> Optional[float]:
    """A typed price or area: 1.200.000, 1 200 000 ARS and $950000 all work.

    People type different thousands separators, and decimals are never needed.
    """
    # Only the first number counts, so the "2" of a typed "65 m2" is ignored.
    match = re.search(r"\d[\d.,\s]*", text)
    return float(re.sub(r"[^0-9]", "", match.group())) if match else None


class TelegramCommandBot(threading.Thread):
    def __init__(self, config: Config, database: Database) -> None:
        super().__init__(name="telegram-commands", daemon=True)
        self.config = config
        self.database = database
        self.resolver = NeighborhoodResolver.from_file(config.neighborhood_map_file)
        self.cities = self.resolver.cities()
        self.session = requests.Session()
        self._stop = threading.Event()

    # ------------------------------------------------------------------ api

    def _call(
        self, method: str, payload: Dict[str, Any], timeout: float = 35.0
    ) -> Optional[Dict[str, Any]]:
        url = TELEGRAM_API.format(token=self.config.telegram.bot_token, method=method)
        try:
            response = self.session.post(url, json=payload, timeout=timeout)
            data = response.json()
        except (requests.RequestException, ValueError) as exc:
            log_context(
                logger, logging.WARNING, "telegram poll failed", method=method, error=str(exc)
            )
            return None
        if not data.get("ok"):
            log_context(
                logger,
                logging.WARNING,
                "telegram API error",
                method=method,
                description=data.get("description"),
            )
            return None
        return data

    def _send(self, chat_id: str, text: str, keyboard: Optional[Keyboard] = None) -> None:
        payload: Dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if keyboard:
            payload["reply_markup"] = {"inline_keyboard": keyboard}
        self._call("sendMessage", payload)

    def _edit(self, chat_id: str, message_id: int, reply: Reply) -> None:
        payload: Dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": reply.text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
            "reply_markup": {"inline_keyboard": reply.keyboard or []},
        }
        self._call("editMessageText", payload)

    # ------------------------------------------------------------------ commands

    def handle_command(
        self, command: str, chat_id: str = "", username: Optional[str] = None
    ) -> str:
        command = command.split("@")[0].strip().lower()
        if chat_id:
            self._clear_awaiting(chat_id)
        if command == "/start":
            if chat_id:
                self.database.add_subscriber(chat_id, username)
                log_context(logger, logging.INFO, "subscriber added", chat_id=chat_id)
            return self.start_message(chat_id) + "\n\n✅ You are subscribed. /stop to unsubscribe."
        if command == "/settings":
            return self.settings_reply(chat_id).text
        if command == "/stop":
            if chat_id and self.database.remove_subscriber(chat_id):
                log_context(logger, logging.INFO, "subscriber removed", chat_id=chat_id)
                return "🔕 Unsubscribed. Send /start to subscribe again."
            return "You are not subscribed. Send /start to subscribe."
        if command == "/status":
            return self.status_message(chat_id)
        if command == "/help":
            return self.start_message(chat_id)
        return "Unknown command. Try /start, /settings, /status or /stop."

    def reply_to_command(
        self, command: str, chat_id: str = "", username: Optional[str] = None
    ) -> Reply:
        if command.split("@")[0].strip().lower() == "/settings":
            self._clear_awaiting(chat_id)
            return self.settings_reply(chat_id)
        return Reply(self.handle_command(command, chat_id, username))

    def _criteria(self, chat_id: str) -> SearchCriteria:
        return criteria_for(self.database, chat_id, self.config.criteria)

    def start_message(self, chat_id: str = "") -> str:
        criteria = self._criteria(chat_id)
        return "\n".join(
            [
                "👋 <b>CABA rental monitor</b>",
                "",
                "I watch {0} and message you when a new matching rental appears.".format(
                    ", ".join(source_label(name) for name in self.config.enabled_sources())
                    or "no source"
                ),
                "",
                "<b>Your search</b>",
                f"Location: {escape(criteria.describe_location())}",
                "Types: {0}".format(escape(", ".join(t.value for t in criteria.property_types))),
                f"Max rent: {criteria.max_rent:,.0f} {criteria.currency} (expensas excluded)".replace(
                    ",", "."
                ),
                f"Min area: {criteria.min_area_m2:g} m²",
                f"Bedrooms: {describe_bedrooms(criteria)}",
                f"Furnished: {FURNISHED_LABELS[criteria.furnished].lower()}",
                f"Checked every {self.config.check_interval_seconds / 60:g} minutes",
                "",
                "Commands: /settings · /status · /stop",
            ]
        )

    def status_message(self, chat_id: str = "") -> str:
        last_cycle = self.database.last_successful_cycle()
        criteria = self._criteria(chat_id)

        lines = [
            "📡 <b>Status</b>",
            "",
            "Last successful check: {0}".format(
                format_local(from_iso(last_cycle["finished_at"])) if last_cycle else "never"
            ),
            "",
            "<b>Filters</b>",
            "Operation: Alquiler",
            "Types: {0}".format(escape(", ".join(t.value for t in criteria.property_types))),
            f"Location: {escape(criteria.describe_location())}",
            f"Max price: {criteria.max_rent:,.0f} {criteria.currency} (expensas excluded)".replace(
                ",", "."
            ),
            f"Min area: {criteria.min_area_m2:g} m²",
            f"Bedrooms: {describe_bedrooms(criteria)}",
            f"Furnished: {FURNISHED_LABELS[criteria.furnished].lower()}",
        ]
        return "\n".join(lines)

    # ------------------------------------------------------------------ settings

    def _clear_awaiting(self, chat_id: str) -> None:
        if chat_id and self.database.get_state(AWAITING_KEY.format(chat_id=chat_id)):
            self.database.set_state(AWAITING_KEY.format(chat_id=chat_id), "")

    def settings_reply(self, chat_id: str, note: str = "") -> Reply:
        criteria = self._criteria(chat_id)
        lines = [
            "⚙️ <b>Your search</b>",
            "",
            f"Location: {escape(criteria.describe_location())}",
            "Types: {0}".format(escape(", ".join(t.value for t in criteria.property_types))),
            f"Max rent: {_amount(criteria.max_rent)} {criteria.currency} (expensas excluded)",
            f"Min area: {criteria.min_area_m2:g} m²",
            f"Bedrooms: {describe_bedrooms(criteria)}",
            f"Furnished: {FURNISHED_LABELS[criteria.furnished].lower()}",
            "USD listings: {0}".format(
                f"shown, at the {self.config.usd_rate_source} rate"
                if criteria.include_usd
                else "hidden"
            ),
            "",
            "Tap what you want to change.",
        ]
        if note:
            lines[0:0] = [note, ""]
        keyboard = [
            [_button("📍 Location", "s:loc"), _button("🏠 Types", "s:types")],
            [_button("💰 Max rent", "s:rent"), _button("📐 Min area", "s:area")],
            [_button("🛏 Bedrooms", "s:beds"), _button("🛋 Furnished", "s:furn")],
            [_button("💵 USD listings: " + ("on" if criteria.include_usd else "off"), "u:toggle")],
            [_button("✅ Done", "s:done")],
        ]
        return Reply("\n".join(lines), keyboard)

    def _cities_reply(self, chat_id: str) -> Reply:
        criteria = self._criteria(chat_id)
        buttons = [
            _button(("✅ " if city in criteria.cities else "▫️ ") + city, f"c:{index}")
            for index, city in enumerate(self.cities)
        ]
        keyboard = _rows(buttons, 3)
        for index, city in enumerate(self.cities):
            if city in criteria.cities:
                chosen = len(criteria.barrios_in(city))
                label = f"{chosen} barrios" if chosen else "all barrios"
                keyboard.append([_button(f"📍 {city}: {label} ›", f"bl:{index}:0")])
        keyboard.append([_button("⬅️ Back", "s:menu")])
        text = (
            "📍 <b>Location</b>\n\nTap a city to add or remove it, then pick its barrios. "
            "A city with no barrio picked is searched as a whole."
        )
        return Reply(text, keyboard)

    def _barrio_button(self, criteria: SearchCriteria, city_index: int, name: str, back: str):
        city = self.cities[city_index]
        index = self.resolver.neighborhoods(city).index(name)
        mark = "✅ " if name in criteria.barrios_in(city) else "▫️ "
        return _button(mark + name, f"bt:{city_index}:{index}:{back}")

    def _barrios_reply(self, chat_id: str, city_index: int, page: int = 0) -> Reply:
        criteria = self._criteria(chat_id)
        city = self.cities[city_index]
        names = self.resolver.neighborhoods(city)
        pages = max(1, -(-len(names) // BARRIOS_PER_PAGE))
        page = min(max(page, 0), pages - 1)
        shown = names[page * BARRIOS_PER_PAGE : (page + 1) * BARRIOS_PER_PAGE]
        whole = not criteria.barrios_in(city)
        keyboard = [[_button(("✅ " if whole else "▫️ ") + f"All of {city}", f"ba:{city_index}")]]
        keyboard += _rows(
            [self._barrio_button(criteria, city_index, name, str(page)) for name in shown], 2
        )
        navigation = []
        if page > 0:
            navigation.append(_button("◀️", f"bl:{city_index}:{page - 1}"))
        navigation.append(_button("🔎 Search", f"bs:{city_index}"))
        if page < pages - 1:
            navigation.append(_button("▶️", f"bl:{city_index}:{page + 1}"))
        keyboard.append(navigation)
        keyboard.append([_button("⬅️ Cities", "s:loc")])
        text = (
            f"📍 <b>{escape(city)}</b> (page {page + 1}/{pages})\n\nTap barrios to add or remove."
        )
        return Reply(text, keyboard)

    def _search_reply(self, chat_id: str, city_index: int, query: str) -> Reply:
        criteria = self._criteria(chat_id)
        city = self.cities[city_index]
        wanted = normalize_text(query)
        matches = [
            name for name in self.resolver.neighborhoods(city) if wanted in normalize_text(name)
        ][:BARRIOS_PER_PAGE]
        keyboard = _rows(
            [self._barrio_button(criteria, city_index, name, "s") for name in matches], 2
        )
        keyboard.append([_button("⬅️ All barrios", f"bl:{city_index}:0")])
        text = (
            f"🔎 <b>{escape(city)}</b>: “{escape(query)}”\n\nTap to add or remove."
            if matches
            else f"🔎 Nothing in {escape(city)} matches “{escape(query)}”."
        )
        return Reply(text, keyboard)

    def _toggle_city(self, chat_id: str, city_index: int) -> Reply:
        criteria = self._criteria(chat_id)
        city = self.cities[city_index]
        cities = list(criteria.cities)
        neighborhoods = list(criteria.neighborhoods)
        if city in cities:
            if len(cities) == 1:
                reply = self._cities_reply(chat_id)
                reply.toast = "Keep at least one city"
                return reply
            cities.remove(city)
            neighborhoods = [key for key in neighborhoods if split_key(key).city != city]
        else:
            cities.append(city)
        self._update(chat_id, cities=cities, neighborhoods=neighborhoods)
        return self._cities_reply(chat_id)

    def _toggle_barrio(self, chat_id: str, city_index: int, barrio_index: int) -> Optional[str]:
        """Returns a toast when the change needs explaining."""
        criteria = self._criteria(chat_id)
        city = self.cities[city_index]
        key = barrio_key(city, self.resolver.neighborhoods(city)[barrio_index])
        neighborhoods = list(criteria.neighborhoods)
        cities = list(criteria.cities)
        toast = None
        if key in neighborhoods:
            neighborhoods.remove(key)
            if not [k for k in neighborhoods if split_key(k).city == city]:
                toast = f"Now searching all of {city}"
        else:
            neighborhoods.append(key)
            if city not in cities:
                cities.append(city)
        self._update(chat_id, cities=cities, neighborhoods=neighborhoods)
        return toast

    def _types_reply(self, chat_id: str) -> Reply:
        chosen = self._criteria(chat_id).property_types
        buttons = [
            _button(("✅ " if ptype in chosen else "▫️ ") + ptype.value, f"t:{ptype.value}")
            for ptype in SELECTABLE_TYPES
        ]
        keyboard = [buttons, [_button("⬅️ Back", "s:menu")]]
        return Reply("🏠 <b>Property types</b>\n\nTap to add or remove.", keyboard)

    def _bedrooms_reply(self, chat_id: str) -> Reply:
        chosen = self._criteria(chat_id).bedrooms
        buttons = [
            _button(("✅ " if value in chosen else "▫️ ") + bedrooms_label(value), f"b:{value}")
            for value in range(BEDROOMS_OR_MORE + 1)
        ]
        keyboard = [
            buttons,
            [_button(("✅ " if not chosen else "▫️ ") + "Any", "b:any")],
            [_button("⬅️ Back", "s:menu")],
        ]
        text = (
            "🛏 <b>Bedrooms</b> (dormitorios)\n\nTap the counts you want; Mono is a "
            "monoambiente. Where a site lists only ambientes, bedrooms are ambientes - 1. "
            "Ads that do not say are still sent."
        )
        return Reply(text, keyboard)

    def _furnished_reply(self, chat_id: str) -> Reply:
        current = self._criteria(chat_id).furnished
        keyboard = [
            [
                _button(("✅ " if value == current else "▫️ ") + label, f"f:{value}")
                for value, label in FURNISHED_LABELS.items()
            ],
            [_button("⬅️ Back", "s:menu")],
        ]
        text = (
            "🛋 <b>Furnished (amoblado)</b>\n\nHides ads that say the opposite. "
            "Most ads do not say; those are still sent."
        )
        return Reply(text, keyboard)

    def _rent_reply(self, chat_id: str) -> Reply:
        criteria = self._criteria(chat_id)
        self.database.set_state(AWAITING_KEY.format(chat_id=chat_id), "rent")
        buttons = [_button(_amount(value), f"r:{value}") for value in RENT_PRESETS]
        keyboard = _rows(buttons, 3) + [[_button("⬅️ Back", "s:menu")]]
        text = (
            f"💰 <b>Max rent</b> ({criteria.currency})\n\nNow: {_amount(criteria.max_rent)}. Pick one or type a number, "
            "e.g. 1200000. Expensas are not counted."
        )
        return Reply(text, keyboard)

    def _area_reply(self, chat_id: str) -> Reply:
        criteria = self._criteria(chat_id)
        self.database.set_state(AWAITING_KEY.format(chat_id=chat_id), "area")
        buttons = [_button(f"{value} m²", f"a:{value}") for value in AREA_PRESETS]
        keyboard = _rows(buttons, 3) + [[_button("⬅️ Back", "s:menu")]]
        text = f"📐 <b>Min area</b>\n\nNow: {criteria.min_area_m2:g} m². Pick one or type a number, e.g. 65."
        return Reply(text, keyboard)

    def _update(self, chat_id: str, **changes: Any) -> SearchCriteria:
        current = self._criteria(chat_id).to_dict()
        current.update(changes)
        updated = self.config.criteria.with_changes(current)
        save_criteria(self.database, chat_id, updated)
        log_context(logger, logging.INFO, "search updated", chat_id=chat_id, **changes)
        return updated

    def handle_callback(self, chat_id: str, data: str) -> Reply:
        """A button press in the /settings menu."""
        kind, _, value = data.partition(":")
        keeps_prompt = (kind == "s" and value in ("rent", "area")) or (
            kind == "bt" and value.endswith(":s")
        )
        if not keeps_prompt:
            self._clear_awaiting(chat_id)

        if kind == "s":
            if value == "loc":
                return self._cities_reply(chat_id)
            if value == "types":
                return self._types_reply(chat_id)
            if value == "rent":
                return self._rent_reply(chat_id)
            if value == "area":
                return self._area_reply(chat_id)
            if value == "beds":
                return self._bedrooms_reply(chat_id)
            if value == "furn":
                return self._furnished_reply(chat_id)
            if value == "done":
                reply = self.settings_reply(chat_id)
                text = reply.text.replace("Tap what you want to change.", "/settings to change it.")
                return Reply(text, toast="Saved")
            return self.settings_reply(chat_id)

        location_reply = self._location_callback(chat_id, kind, value)
        if location_reply:
            return location_reply

        if kind == "t" and value in [t.value for t in SELECTABLE_TYPES]:
            chosen_types = [t.value for t in self._criteria(chat_id).property_types]
            if value in chosen_types:
                if len(chosen_types) == 1:
                    reply = self._types_reply(chat_id)
                    reply.toast = "Keep at least one type"
                    return reply
                chosen_types.remove(value)
            else:
                chosen_types.append(value)
            self._update(chat_id, property_types=chosen_types)
            return self._types_reply(chat_id)

        if kind == "b" and (value == "any" or (value.isdigit() and int(value) <= BEDROOMS_OR_MORE)):
            beds = list(self._criteria(chat_id).bedrooms)
            if value == "any":
                beds = []
            elif int(value) in beds:
                beds.remove(int(value))
            else:
                beds.append(int(value))
            self._update(chat_id, bedrooms=beds)
            return self._bedrooms_reply(chat_id)

        if kind == "u" and value == "toggle":
            include = not self._criteria(chat_id).include_usd
            self._update(chat_id, include_usd=include)
            return self.settings_reply(
                chat_id, note="✅ USD listings " + ("shown" if include else "hidden")
            )

        if kind == "f" and value in FURNISHED_LABELS:
            self._update(chat_id, furnished=value)
            return self.settings_reply(chat_id, note="✅ Furnished updated")

        if kind == "r" and value.isdigit():
            self._update(chat_id, max_rent=float(value))
            return self.settings_reply(chat_id, note="✅ Max rent updated")

        if kind == "a" and value.isdigit():
            self._update(chat_id, min_area_m2=float(value))
            return self.settings_reply(chat_id, note="✅ Min area updated")

        return self.settings_reply(chat_id)

    def _city_index(self, value: str) -> Optional[int]:
        return int(value) if value.isdigit() and int(value) < len(self.cities) else None

    def _location_callback(self, chat_id: str, kind: str, value: str) -> Optional[Reply]:
        """Buttons of the city and barrio screens; None if not one of them."""
        parts = value.split(":")
        city_index = self._city_index(parts[0])
        if city_index is None or kind not in ("c", "bl", "ba", "bs", "bt"):
            return None
        if kind == "c":
            return self._toggle_city(chat_id, city_index)
        if kind == "bl":
            page = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
            return self._barrios_reply(chat_id, city_index, page)
        if kind == "ba":
            city = self.cities[city_index]
            criteria = self._criteria(chat_id)
            cities = criteria.cities if city in criteria.cities else [*criteria.cities, city]
            neighborhoods = [k for k in criteria.neighborhoods if split_key(k).city != city]
            self._update(chat_id, cities=cities, neighborhoods=neighborhoods)
            return self._barrios_reply(chat_id, city_index, 0)
        if kind == "bs":
            self.database.set_state(AWAITING_KEY.format(chat_id=chat_id), f"search:{city_index}")
            text = (
                f"🔎 Type part of a barrio name in {escape(self.cities[city_index])}, e.g. palermo."
            )
            return Reply(text, [[_button("⬅️ All barrios", f"bl:{city_index}:0")]])
        # "bt:<city>:<barrio>:<page or s>"
        names = self.resolver.neighborhoods(self.cities[city_index])
        if len(parts) < 3 or not parts[1].isdigit() or int(parts[1]) >= len(names):
            return self._barrios_reply(chat_id, city_index, 0)
        toast = self._toggle_barrio(chat_id, city_index, int(parts[1]))
        if parts[2] == "s":
            query = self.database.get_state(SEARCH_KEY.format(chat_id=chat_id)) or ""
            reply = self._search_reply(chat_id, city_index, query)
        else:
            page = int(parts[2]) if parts[2].isdigit() else 0
            reply = self._barrios_reply(chat_id, city_index, page)
        reply.toast = toast
        return reply

    def handle_text(self, chat_id: str, text: str) -> Optional[Reply]:
        """Plain text: a typed price or area if the menu asked for one."""
        key = AWAITING_KEY.format(chat_id=chat_id)
        awaiting = self.database.get_state(key)
        if not awaiting:
            return Reply("Send /settings to change your search, or /help.")
        if awaiting.startswith("search:"):
            city_index = self._city_index(awaiting.split(":", 1)[1])
            if city_index is None:
                self.database.set_state(key, "")
                return Reply("Send /settings to change your search, or /help.")
            # Stay in search mode: the next text is another search.
            self.database.set_state(SEARCH_KEY.format(chat_id=chat_id), text.strip())
            return self._search_reply(chat_id, city_index, text.strip())
        number = parse_number(text)
        low, high = RENT_RANGE if awaiting == "rent" else AREA_RANGE
        if number is None or not low <= number <= high:
            example = "1200000" if awaiting == "rent" else "65"
            return Reply(
                f"Please send a number between {_amount(low)} and {_amount(high)}, e.g. {example}."
            )
        self.database.set_state(key, "")
        if awaiting == "rent":
            self._update(chat_id, max_rent=number)
            return self.settings_reply(chat_id, note="✅ Max rent updated")
        self._update(chat_id, min_area_m2=number)
        return self.settings_reply(chat_id, note="✅ Min area updated")

    # ------------------------------------------------------------------ loop

    def _updates(self) -> List[Dict[str, Any]]:
        offset = self.database.get_state(OFFSET_KEY)
        payload: Dict[str, Any] = {
            "timeout": POLL_TIMEOUT_SECONDS,
            "allowed_updates": ["message", "callback_query"],
        }
        if offset:
            payload["offset"] = int(offset)
        data = self._call("getUpdates", payload, timeout=POLL_TIMEOUT_SECONDS + 10)
        return data.get("result", []) if data else []

    def stop(self) -> None:
        self._stop.set()

    def _dispatch(self, update: Dict[str, Any]) -> None:
        callback = update.get("callback_query")
        if callback:
            message = callback.get("message") or {}
            chat_id = str((message.get("chat") or {}).get("id", ""))
            if not chat_id:
                return
            reply = self.handle_callback(chat_id, callback.get("data") or "")
            answer: Dict[str, Any] = {"callback_query_id": callback.get("id")}
            if reply.toast:
                answer["text"] = reply.toast
            self._call("answerCallbackQuery", answer)
            self._edit(chat_id, message.get("message_id"), reply)
            return

        message = update.get("message") or {}
        text = (message.get("text") or "").strip()
        chat = message.get("chat") or {}
        chat_id = str(chat.get("id", ""))
        if not text or not chat_id:
            return
        if text.startswith("/"):
            log_context(logger, logging.INFO, "telegram command", command=text.split()[0])
            reply = self.reply_to_command(text.split()[0], chat_id, chat.get("username"))
        else:
            reply = self.handle_text(chat_id, text)
        if reply:
            self._send(chat_id, reply.text, reply.keyboard)

    def run(self) -> None:
        if not self.config.telegram.configured:
            logger.info("telegram commands disabled: no bot token configured")
            return
        logger.info("telegram command bot started")
        self._call(
            "setMyCommands",
            {"commands": [{"command": name, "description": text} for name, text in COMMANDS]},
        )
        while not self._stop.is_set():
            try:
                updates = self._updates()
            except Exception:  # noqa: BLE001 - the poller must never die
                logger.exception("telegram polling error")
                self._stop.wait(10)
                continue

            for update in updates:
                self.database.set_state(OFFSET_KEY, str(update.get("update_id", 0) + 1))
                try:
                    self._dispatch(update)
                except Exception:  # noqa: BLE001 - one bad update must not stop the bot
                    logger.exception("telegram update failed")

            if not updates:
                self._stop.wait(1)
        logger.info("telegram command bot stopped")
