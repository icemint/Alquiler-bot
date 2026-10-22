"""Build monitor/data/locations.json: cities, their barrios, and how each site
names them.

Run it when a site changes its location taxonomy:

    .venv/bin/python scripts/build_locations.py

What comes from where:

* MercadoLibre: the "Barrios" / "Ciudades" facet of a city's search page
  lists every location it knows, with the URL path to search it. In CABA and
  AMBA its sub-zones (Palermo Soho, Belgrano R, ...) are folded into the
  barrio that contains them, so the bot's menu stays a readable list. Other
  cities take MercadoLibre's barrios as they are (BARRIOS is None), plus the
  hand-made ones in EXTRA_BARRIOS.
* CabaProp: /api/v1/utils/barrios-obj (CABA only), folded the same way.
* ArgenProp: its WAF does not tolerate crawling, so its slugs and ids below
  were collected by hand from single search pages (CABA 2026-10-21, other
  cities 2026-10-21/22, one page per place, minutes apart). Outside CABA a
  city or an AMBA partido is known by its partido id; barrios without an id
  are resolved from the name on the card, within the city.

Barrios a site has that do not fold into the list below are reported, not
guessed: add them to BARRIOS (as a barrio or an alias) and run again.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any, Dict, List, Optional

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from monitor.config import DEFAULT_USER_AGENT  # noqa: E402
from monitor.neighborhoods import normalize_text  # noqa: E402

OUTPUT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "monitor", "data", "locations.json"
)
MELI = "https://inmuebles.mercadolibre.com.ar/departamentos/alquiler/{path}/"
CABAPROP_BARRIOS = "https://cabaprop.com.ar/api/v1/utils/barrios-obj"

# city -> {canonical barrio: [aliases: other names sites use for it or for
# a sub-zone inside it]}
BARRIOS: Dict[str, Dict[str, List[str]]] = {
    "CABA": {
        "Agronomía": [],
        "Almagro": ["Almagro Norte", "Almagro Sur"],
        "Balvanera": ["Once", "Congreso", "Abasto", "Balvanera Congreso", "Balvanera Once"],
        "Barracas": [],
        # Not an official barrio, but how people search; it overlaps
        # Recoleta, so sites file it either way.
        "Barrio Norte": [
            "B Norte",
            "Bo Norte",
            "Barrio Norte La Isla",
            "La Isla",
            "Barrio Norte Parque Las Heras",
            "Parque Las Heras",
            "Barrio Norte Recoleta",
        ],
        "Belgrano": [
            "Belgrano Barrancas",
            "Barrancas de Belgrano",
            "Belgrano C",
            "Belgrano Chico",
            "Belgrano R",
            "Belgrano La Imprenta",
            "La Imprenta",
        ],
        "Boedo": [],
        "Caballito": [
            "Caballito Barrio Inglés",
            "Caballito Cid Campeador",
            "Cid Campeador",
            "Caballito Norte",
            "Caballito Parque Rivadavia",
            "Parque Rivadavia",
            "Caballito Primera Junta",
            "Primera Junta",
            "Caballito Sur",
            "Parque Centenario",
        ],
        "Chacarita": [],
        "Coghlan": [],
        "Colegiales": [],
        "Constitución": [],
        "Flores": ["Flores Norte", "Flores Sur"],
        "Floresta": ["Floresta Norte", "Floresta Sur"],
        "La Boca": ["Boca"],
        "La Paternal": ["Paternal"],
        "Liniers": [],
        "Mataderos": [],
        "Monserrat": ["Montserrat"],
        "Monte Castro": [],
        "Nueva Pompeya": ["Pompeya"],
        "Núñez": ["Nuñez Lomas", "Nuñez River"],
        "Palermo": [
            "Palermo Chico",
            "Palermo Hollywood",
            "Palermo Nuevo",
            "Palermo Soho",
            "Palermo Viejo",
            "Botánico",
            "Palermo Botánico",
            "Las Cañitas",
            "Palermo Las Cañitas",
            "Palermo Boulevard",
            "Palermo Pacífico",
            "Palermo Plaza Italia",
        ],
        "Parque Avellaneda": [],
        "Parque Chacabuco": [],
        "Parque Chas": [],
        "Parque Patricios": [],
        "Puerto Madero": [],
        "Recoleta": ["Barrio Recoleta", "Recoleta CABA"],
        "Retiro": ["Retiro Catalinas", "Catalinas", "Retiro Plaza San Martín"],
        "Saavedra": [],
        "San Cristóbal": [],
        "San Nicolás": [
            "Microcentro / Centro",
            "Microcentro",
            "Centro",
            "San Nicolás Tribunales",
            "Tribunales",
        ],
        "San Telmo": [],
        "Vélez Sarsfield": [],
        "Versalles": [],
        "Villa Crespo": [],
        "Villa del Parque": [],
        "Villa Devoto": ["Devoto"],
        "Villa General Mitre": ["Villa Gral. Mitre"],
        "Villa Lugano": ["Lugano"],
        "Villa Luro": [],
        "Villa Ortúzar": [],
        "Villa Pueyrredón": [],
        "Villa Real": [],
        "Villa Riachuelo": [],
        "Villa Santa Rita": ["Santa Rita"],
        "Villa Soldati": [],
        "Villa Urquiza": [],
    },
    # In AMBA the "barrios" are the partidos (municipalities) of Greater
    # Buenos Aires. La Plata is a city of its own.
    "AMBA": {
        "Almirante Brown": [],
        "Avellaneda": [],
        "Berazategui": [],
        "Escobar": [],
        "Esteban Echeverría": [],
        "Ezeiza": [],
        "Florencio Varela": [],
        "General Rodríguez": ["Gral. Rodríguez"],
        "General San Martín": ["San Martín", "Gral. San Martín"],
        "Hurlingham": [],
        "Ituzaingó": [],
        "José C. Paz": [],
        "La Matanza": [],
        "Lanús": [],
        "Lomas de Zamora": [],
        "Malvinas Argentinas": [],
        "Merlo": [],
        "Moreno": [],
        "Morón": ["Castelar"],
        "Pilar": ["Del Viso"],
        "Presidente Perón": [],
        "Quilmes": [],
        "San Fernando": [],
        "San Isidro": ["Beccar"],
        "San Miguel": [],
        "Tigre": [],
        "Tres de Febrero": ["Caseros"],
        "Vicente López": [],
    },
    # None: every barrio MercadoLibre lists for the city.
    "La Plata": None,
    "Rosario": None,
    "Córdoba": None,
    "Mendoza": {},
    "Mar del Plata": None,
    "Bariloche": None,
    "Salta": None,
    "Neuquén": None,
    "Tucumán": None,
}

# Barrios MercadoLibre does not list as such: city -> {barrio: search path}.
# Their cards name the city only, so their listings are recognised by the
# search that found them (see mercadolibre/parser.py).
EXTRA_BARRIOS: Dict[str, Dict[str, str]] = {
    # MercadoLibre's "La Plata" barrio: the city proper, inside the diagonals.
    "La Plata": {"Casco Urbano": "bsas-gba-sur/la-plata/la-plata"},
    # Gran Mendoza: the capital and the departments around it, each a city
    # of its own on MercadoLibre.
    "Mendoza": {
        "Ciudad de Mendoza": "mendoza/mendoza",
        "Godoy Cruz": "mendoza/godoy-cruz",
        "Guaymallén": "mendoza/guaymallen",
        "Las Heras": "mendoza/las-heras",
        "Luján de Cuyo": "mendoza/lujan-de-cuyo",
        "Maipú": "mendoza/maipu",
    },
    "Tucumán": {"Yerba Buena": "tucuman/yerba-buena"},
}

CITIES: Dict[str, Dict[str, Any]] = {
    "CABA": {
        "aliases": ["Capital Federal", "Ciudad Autónoma de Buenos Aires"],
        "mercadolibre": ["capital-federal"],
        "argenprop": {"slugs": ["capital-federal"], "ids": ["localidad:2102"]},
        "cabaprop": True,
    },
    "AMBA": {
        "aliases": [
            "Bs.As. G.B.A. Norte",
            "Bs.As. G.B.A. Sur",
            "Bs.As. G.B.A. Oeste",
            "GBA",
            "Gran Buenos Aires",
        ],
        "mercadolibre": ["bsas-gba-norte", "bsas-gba-sur", "bsas-gba-oeste"],
        # No search for all of AMBA: 28 partidos a cycle is more than the WAF
        # tolerates. Chosen partidos are searched one by one.
        "argenprop": {"slugs": [], "ids": []},
        "cabaprop": False,
    },
    "La Plata": {
        "aliases": [],
        "mercadolibre": ["bsas-gba-sur/la-plata"],
        "argenprop": {"slugs": ["partido-de-la-plata"], "ids": ["partido:68"]},
    },
    "Rosario": {
        "aliases": [],
        "mercadolibre": ["santa-fe/rosario"],
        "argenprop": {"slugs": ["rosario"], "ids": ["partido:460"]},
    },
    "Córdoba": {
        "aliases": [],
        "mercadolibre": ["cordoba/cordoba"],
        "argenprop": {"slugs": ["cordoba"], "ids": ["partido:193"]},
    },
    "Mendoza": {
        "aliases": [],
        "mercadolibre": list(EXTRA_BARRIOS["Mendoza"].values()),
        # The capital only; the other departments when chosen (see AMBA).
        "argenprop": {"slugs": ["mendoza"], "ids": []},
    },
    "Mar del Plata": {
        "aliases": [],
        "mercadolibre": ["bsas-costa-atlantica/mar-del-plata"],
        "argenprop": {"slugs": ["mar-del-plata"], "ids": ["partido:56"]},
    },
    "Bariloche": {
        "aliases": ["San Carlos de Bariloche"],
        "mercadolibre": ["rio-negro/bariloche"],
        "argenprop": {"slugs": ["bariloche"], "ids": ["partido:382"]},
    },
    "Salta": {
        "aliases": [],
        "mercadolibre": ["salta/salta"],
        "argenprop": {"slugs": ["salta"], "ids": ["partido:394"]},
    },
    "Neuquén": {
        "aliases": [],
        "mercadolibre": ["neuquen/neuquen"],
        "argenprop": {"slugs": ["neuquen"], "ids": ["partido:366"]},
    },
    "Tucumán": {
        "aliases": ["San Miguel de Tucumán"],
        "mercadolibre": ["tucuman/san-miguel-de-tucuman", "tucuman/yerba-buena"],
        "argenprop": {"slugs": ["san-miguel-de-tucuman"], "ids": ["partido:500"]},
    },
}
# MercadoLibre's catch-all, not a barrio.
NOT_A_BARRIO = {"otros barrios"}

# ArgenProp, collected by hand (see the module docstring).
ARGENPROP_SLUGS = {
    "CABA": {
        "Almagro": "almagro",
        "Balvanera": "balvanera",
        "Barrio Norte": "barrio-norte",
        "Belgrano": "belgrano",
        "Caballito": "caballito",
        "Colegiales": "colegiales",
        "Flores": "flores",
        "Monserrat": "monserrat",
        "Núñez": "nunez",
        "Palermo": "palermo",
        "Puerto Madero": "puerto-madero",
        "Recoleta": "recoleta",
        "Retiro": "retiro",
        "San Cristóbal": "san-cristobal",
        "San Nicolás": "san-nicolas",
        "San Telmo": "san-telmo",
        "Villa Crespo": "villa-crespo",
        "Villa del Parque": "villa-del-parque",
        "Villa Devoto": "villa-devoto",
        "Villa Urquiza": "villa-urquiza",
    },
    "AMBA": {
        "Almirante Brown": "partido-de-almirante-brown",
        "Avellaneda": "partido-de-avellaneda",
        "Berazategui": "partido-de-berazategui",
        "Escobar": "partido-de-escobar",
        "Esteban Echeverría": "partido-de-esteban-echeverria",
        "Ezeiza": "partido-de-ezeiza",
        "Florencio Varela": "partido-de-florencio-varela",
        "General Rodríguez": "partido-de-general-rodriguez",
        "General San Martín": "partido-de-general-san-martin",
        "Hurlingham": "partido-de-hurlingham",
        "Ituzaingó": "partido-de-ituzaingo",
        "José C. Paz": "partido-de-jose-c-paz",
        "La Matanza": "partido-de-la-matanza",
        "Lanús": "partido-de-lanus",
        "Lomas de Zamora": "partido-de-lomas-de-zamora",
        "Malvinas Argentinas": "partido-de-malvinas-argentinas",
        "Merlo": "partido-de-merlo",
        "Moreno": "partido-de-moreno",
        "Morón": "partido-de-moron",
        "Pilar": "partido-de-pilar",
        "Presidente Perón": "partido-de-presidente-peron",
        "Quilmes": "partido-de-quilmes",
        "San Fernando": "partido-de-san-fernando",
        "San Isidro": "partido-de-san-isidro",
        "San Miguel": "partido-de-san-miguel",
        "Tigre": "partido-de-tigre",
        "Tres de Febrero": "partido-de-tres-de-febrero",
        "Vicente López": "partido-de-vicente-lopez",
    },
    # No working slug found for Las Heras and Maipú (both redirect to the
    # whole country); they come from the capital's search, by card name.
    "Mendoza": {
        "Ciudad de Mendoza": "mendoza",
        "Godoy Cruz": "godoy-cruz",
        "Guaymallén": "guaymallen",
        "Luján de Cuyo": "lujan-de-cuyo",
    },
    "Tucumán": {"Yerba Buena": "yerba-buena"},
}
ARGENPROP_IDS = {
    "CABA": {
        "Barrio Norte": ["barrio:4"],
        "Caballito": ["barrio:8"],
        "Liniers": ["barrio:16"],
        "Monserrat": ["barrio:12641"],
        "Palermo": ["barrio:20"],
        "Recoleta": ["barrio:12618"],
        "Retiro": ["barrio:12645"],
    },
    "AMBA": {
        "Almirante Brown": ["partido:6"],
        "Avellaneda": ["partido:8"],
        "Berazategui": ["partido:15"],
        "Escobar": ["partido:39"],
        "Esteban Echeverría": ["partido:40"],
        "Ezeiza": ["partido:42"],
        "Florencio Varela": ["partido:43"],
        "General Rodríguez": ["partido:57"],
        "General San Martín": ["partido:58"],
        "Hurlingham": ["partido:63"],
        "Ituzaingó": ["partido:64"],
        "José C. Paz": ["partido:65"],
        "La Matanza": ["partido:67"],
        "Lanús": ["partido:69"],
        "Lomas de Zamora": ["partido:76"],
        "Malvinas Argentinas": ["partido:80"],
        "Merlo": ["partido:84"],
        "Moreno": ["partido:87"],
        "Morón": ["partido:88"],
        "Pilar": ["partido:97"],
        "Presidente Perón": ["partido:99"],
        "Quilmes": ["partido:102"],
        "San Fernando": ["partido:115"],
        "San Isidro": ["partido:116"],
        "San Miguel": ["partido:117"],
        "Tigre": ["partido:124"],
        "Tres de Febrero": ["partido:129"],
        "Vicente López": ["partido:131"],
    },
    "Mendoza": {
        "Ciudad de Mendoza": ["partido:326"],
        "Godoy Cruz": ["partido:328"],
        "Guaymallén": ["partido:329"],
        "Luján de Cuyo": ["partido:334"],
        "Maipú": ["partido:335"],
    },
    "Tucumán": {"Yerba Buena": ["partido:515"]},
}


def fold(city: str, name: str) -> Optional[str]:
    """The canonical barrio a site's location name belongs to."""
    wanted = normalize_text(name)
    for barrio, aliases in (BARRIOS[city] or {}).items():
        if wanted == normalize_text(barrio) or wanted in {normalize_text(a) for a in aliases}:
            return barrio
    return None


def meli_facet(path: str, facet_id: str) -> List[Dict[str, Any]]:
    response = requests.get(
        MELI.format(path=path), headers={"User-Agent": DEFAULT_USER_AGENT}, timeout=30
    )
    response.raise_for_status()
    page = response.text
    marker = f'"id":"{facet_id}","name":'
    start = page.find(marker)
    while start != -1:
        values = page.find('"values":[', start)
        if page[values + 10] == "{":
            depth, begin = 0, values + 9
            for end in range(begin, len(page)):
                if page[end] == "[":
                    depth += 1
                elif page[end] == "]":
                    depth -= 1
                    if depth == 0:
                        return json.loads(page[begin : end + 1])
        start = page.find(marker, start + 1)
    raise RuntimeError(f"no {facet_id} facet on {path}")


def meli_path(url: str) -> str:
    return url.split("/alquiler/", 1)[1].split("#", 1)[0].strip("/")


def build() -> Dict[str, Any]:
    cities: Dict[str, Any] = {}
    unknown: List[str] = []
    # A barrio must never be named like a city: the city is read first.
    city_names = {
        normalize_text(name) for city, spec in CITIES.items() for name in [city, *spec["aliases"]]
    }

    for city, barrios in BARRIOS.items():
        spec = CITIES[city]
        entries: Dict[str, Dict[str, Any]] = {
            barrio: {"aliases": list(aliases), "slugs": {}, "ids": {}}
            for barrio, aliases in (barrios or {}).items()
        }

        facet = "city" if city == "AMBA" else "neighborhood"
        zones = spec["mercadolibre"] if barrios is None or barrios else []
        for zone in zones:
            for value in meli_facet(zone, facet):
                name = normalize_text(value["name"])
                if name in city_names or name in NOT_A_BARRIO:
                    continue  # a city of its own (La Plata sits in GBA Sur)
                barrio = fold(city, value["name"])
                if barrio is None and barrios is None:
                    barrio = value["name"]
                    entries.setdefault(barrio, {"aliases": [], "slugs": {}, "ids": {}})
                if barrio is None:
                    unknown.append(f"mercadolibre {city}: {value['name']}")
                    continue
                paths = entries[barrio]["slugs"].setdefault("mercadolibre", [])
                path = meli_path(value["url"])
                if path not in paths:
                    paths.append(path)
                if normalize_text(value["name"]) != normalize_text(barrio):
                    aliases = entries[barrio]["aliases"]
                    if value["name"] not in aliases:
                        aliases.append(value["name"])

        for barrio, path in EXTRA_BARRIOS.get(city, {}).items():
            entries[barrio] = {"aliases": [], "slugs": {"mercadolibre": [path]}, "ids": {}}

        if spec.get("cabaprop"):
            response = requests.get(
                CABAPROP_BARRIOS, headers={"User-Agent": DEFAULT_USER_AGENT}, timeout=30
            )
            response.raise_for_status()
            for identifier, name in sorted(response.json().items(), key=lambda i: int(i[0])):
                barrio = fold(city, name)
                if barrio is None:
                    unknown.append(f"cabaprop {city}: {identifier} {name}")
                    continue
                entries[barrio]["ids"].setdefault("cabaprop", []).append(identifier)

        for barrio, slug in ARGENPROP_SLUGS.get(city, {}).items():
            entries[barrio]["slugs"]["argenprop"] = [slug]
        for barrio, ids in ARGENPROP_IDS.get(city, {}).items():
            entries[barrio]["ids"]["argenprop"] = list(ids)

        argenprop = spec.get("argenprop") or {}
        cities[city] = {
            "aliases": spec["aliases"],
            "slugs": {
                "mercadolibre": spec["mercadolibre"],
                "argenprop": argenprop.get("slugs", []),
            },
            "ids": {"argenprop": argenprop.get("ids", [])},
            "barrios": dict(sorted(entries.items(), key=lambda item: normalize_text(item[0]))),
        }

    for line in unknown:
        print("not folded:", line, file=sys.stderr)
    return {"cities": cities}


def main() -> int:
    data = build()
    with open(OUTPUT, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=1)
        handle.write("\n")
    for city, spec in data["cities"].items():
        missing = [b for b, e in spec["barrios"].items() if "mercadolibre" not in e["slugs"]]
        print(f"{city}: {len(spec['barrios'])} barrios; no MercadoLibre path for: {missing}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
