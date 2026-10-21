"""Location resolution must use structured fields, never free text."""

from monitor.neighborhoods import (
    Location,
    NeighborhoodResolver,
    barrio_key,
    normalize_text,
    split_key,
)


def test_normalize_text_strips_accents_and_punctuation():
    assert normalize_text("Núñez-Belgrano") == "nunez belgrano"


def test_resolves_canonical_names_and_aliases(resolver):
    assert resolver.from_label("Recoleta") == Location("CABA", "Recoleta")
    assert resolver.from_label("barrio norte") == Location("CABA", "Barrio Norte")
    assert resolver.from_label("BARRIO-NORTE") == Location("CABA", "Barrio Norte")


def test_site_sub_zones_fold_into_their_barrio(resolver):
    assert resolver.from_label("Palermo Soho") == Location("CABA", "Palermo")
    assert resolver.from_label("Parque Las Heras") == Location("CABA", "Barrio Norte")
    assert resolver.from_label("Belgrano R") == Location("CABA", "Belgrano")


def test_unknown_label_is_none(resolver):
    assert resolver.from_label("Narnia") is None
    assert resolver.from_label(None) is None


def test_does_not_match_a_barrio_inside_an_address(resolver):
    # The whole point: an address mentioning the zone is not a classification.
    assert resolver.from_label("Av. Santa Fe 1234, a metros de Barrio Norte") is None


def test_a_label_can_be_limited_to_one_city(resolver):
    assert resolver.from_label("Recoleta", "AMBA") is None
    assert resolver.from_label("Tigre", "AMBA") == Location("AMBA", "Tigre")


def test_resolves_search_paths_per_source(resolver):
    assert resolver.from_slug("mercadolibre", "capital-federal/barrio-norte") == Location(
        "CABA", "Barrio Norte"
    )
    assert resolver.from_slug("argenprop", "recoleta") == Location("CABA", "Recoleta")
    assert resolver.from_slug("mercadolibre", "bsas-gba-norte") == Location("AMBA")
    assert resolver.from_slug("mercadolibre", "nowhere") is None


def test_location_path_takes_the_city_then_its_barrio(resolver):
    assert resolver.from_location_path(
        "mercadolibre", ["Ayacucho Al 800", "Recoleta", "Capital Federal"]
    ) == Location("CABA", "Recoleta")
    assert resolver.from_location_path(
        "mercadolibre",
        ["Av. Del Libertador 15000", "Acassuso", "San Isidro", "Bs.As. G.B.A. Norte"],
    ) == Location("AMBA", "San Isidro")


def test_a_known_city_with_an_unknown_barrio_keeps_the_city(resolver):
    assert resolver.from_location_path(
        "mercadolibre", ["Calle 1", "Somewhere New", "Capital Federal"]
    ) == Location("CABA")


def test_the_street_does_not_win_over_the_barrio(resolver):
    # "Belgrano" is a CABA barrio, but here it is the street.
    assert resolver.from_location_path(
        "mercadolibre", ["Belgrano", "Monserrat", "Capital Federal"]
    ) == Location("CABA", "Monserrat")


def test_site_internal_ids_resolve_to_a_location(resolver):
    assert resolver.from_id("argenprop", "barrio:12618") == Location("CABA", "Recoleta")
    assert resolver.from_id("argenprop", "barrio:4") == Location("CABA", "Barrio Norte")
    assert resolver.from_id("argenprop", "localidad:2102") == Location("CABA")
    assert resolver.from_id("cabaprop", "12") == Location("CABA", "Barrio Norte")
    assert resolver.from_id("argenprop", "barrio:99999") is None
    assert resolver.from_id("argenprop", None) is None


def test_the_most_specific_id_wins(resolver):
    found = resolver.from_ids("argenprop", ["barrio:4", "localidad:2102"])
    assert found == Location("CABA", "Barrio Norte")
    assert resolver.from_ids("argenprop", [None, "localidad:2102"]) == Location("CABA")


def test_keys_qualify_barrios_by_city():
    assert barrio_key("CABA", "Recoleta") == "CABA/Recoleta"
    assert split_key("AMBA/Tigre") == Location("AMBA", "Tigre")
    # Searches stored before cities existed hold bare CABA names.
    assert split_key("Recoleta") == Location("CABA", "Recoleta")


def test_catalog_lists_cities_and_their_barrios(resolver):
    assert resolver.cities()[:2] == ["CABA", "AMBA"]
    assert "Villa Crespo" in resolver.neighborhoods("CABA")
    assert "Vicente López" in resolver.neighborhoods("AMBA")
    assert resolver.resolve_key("barrio norte") == "CABA/Barrio Norte"


def test_custom_mapping_can_be_supplied():
    custom = {
        "Rosario": {
            "aliases": ["rosario santa fe"],
            "barrios": {"Centro": {"aliases": ["microcentro"]}},
        }
    }
    custom_resolver = NeighborhoodResolver(custom)
    assert custom_resolver.from_label("Microcentro") == Location("Rosario", "Centro")
    assert custom_resolver.city_from_label("Rosario Santa Fe") == "Rosario"
    assert custom_resolver.from_label("Barrio Norte") is None


def test_the_most_specific_city_wins(resolver):
    # MercadoLibre files La Plata under "G.B.A. Sur", which is AMBA.
    assert resolver.from_location_path(
        "mercadolibre", ["Calle 46 Al 525", "La Plata", "Bs.As. G.B.A. Sur"]
    ) == Location("La Plata")
