"""Neighborhood resolution must use structured fields, never free text."""

from monitor.neighborhoods import NeighborhoodResolver, normalize_text


def test_normalize_text_strips_accents_and_punctuation():
    assert normalize_text("Núñez-Belgrano") == "nunez belgrano"


def test_resolves_canonical_and_aliases(resolver):
    assert resolver.from_label("Recoleta") == "Recoleta"
    assert resolver.from_label("barrio norte") == "Barrio Norte"
    assert resolver.from_label("BARRIO-NORTE") == "Barrio Norte"


def test_unknown_neighborhood_is_none(resolver):
    assert resolver.from_label("Palermo") is None
    assert resolver.from_label(None) is None


def test_does_not_match_neighborhood_inside_an_address(resolver):
    # The whole point: an address mentioning the zone is not a classification.
    assert resolver.from_label("Av. Santa Fe 1234, a metros de Barrio Norte") is None


def test_resolves_from_slug_per_source(resolver):
    assert resolver.from_slug("mercadolibre", "barrio-norte") == "Barrio Norte"
    assert resolver.from_slug("argenprop", "recoleta") == "Recoleta"
    assert resolver.from_slug("mercadolibre", "palermo") is None


def test_location_path_prefers_the_most_specific_part(resolver):
    assert (
        resolver.from_location_path("mercadolibre", ["Capital Federal", "Recoleta"]) == "Recoleta"
    )
    assert resolver.from_location_path("mercadolibre", ["Capital Federal", "Palermo"]) is None


def test_custom_mapping_can_be_supplied():
    custom = {
        "Recoleta": {"aliases": ["recoleta", "recoleta norte"], "slugs": {"zonaprop": ["recoleta"]}}
    }
    custom_resolver = NeighborhoodResolver(custom)
    assert custom_resolver.from_label("Recoleta Norte") == "Recoleta"
    assert custom_resolver.from_label("Barrio Norte") is None


def test_site_internal_ids_resolve_to_canonical_names(resolver):
    assert resolver.from_id("argenprop", "12618") == "Recoleta"
    assert resolver.from_id("argenprop", "4") == "Barrio Norte"
    assert resolver.from_id("argenprop", "99999") is None
    assert resolver.from_id("argenprop", None) is None
    assert resolver.from_id("mercadolibre", "4") is None
