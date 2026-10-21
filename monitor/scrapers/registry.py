"""Builds the enabled scrapers from configuration."""

from __future__ import annotations

from typing import Dict, List

from monitor.config import Config
from monitor.neighborhoods import NeighborhoodResolver
from monitor.scrapers.base import BaseScraper, HttpClient


def build_scrapers(config: Config, resolver: NeighborhoodResolver) -> List[BaseScraper]:
    from monitor.scrapers.argenprop import ArgenPropScraper
    from monitor.scrapers.cabaprop import CabaPropScraper
    from monitor.scrapers.mercadolibre import MercadoLibreScraper
    from monitor.scrapers.zonaprop import ZonaPropScraper

    classes: Dict[str, type] = {
        "mercadolibre": MercadoLibreScraper,
        "argenprop": ArgenPropScraper,
        "cabaprop": CabaPropScraper,
        "zonaprop": ZonaPropScraper,
    }

    http = HttpClient(
        user_agent=config.user_agent,
        connect_timeout=config.http_connect_timeout_seconds,
        read_timeout=config.http_timeout_seconds,
        max_retries=config.http_max_retries,
    )

    scrapers: List[BaseScraper] = []
    for name, source_config in config.sources.items():
        if not source_config.enabled:
            continue
        scraper_cls = classes.get(name)
        if scraper_cls is None:
            continue
        scrapers.append(scraper_cls(config, source_config, resolver, http))
    return scrapers
