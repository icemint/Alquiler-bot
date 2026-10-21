"""ZonaProp scraper - disabled by default, and here is why.

Every ZonaProp URL, including the sitemaps advertised in its own robots.txt,
answers ``HTTP 403`` with ``cf-mitigated: challenge`` and Cloudflare's
"Just a moment..." interstitial. There is no public API and no RSS feed. The
only ways past it are:

1. a headless browser that executes the Cloudflare challenge - that is an
   anti-bot bypass, which this project deliberately does not do;
2. a commercial scraping provider that deals with the challenge on its side
   (``ZONAPROP_STRATEGY=provider`` plus ``ZONAPROP_PROVIDER_URL``/
   ``ZONAPROP_API_KEY``);
3. ZonaProp's own saved-search email alerts, ingested from a mailbox.

So the default strategy is ``disabled``: the source reports itself as disabled
once per cycle, makes no requests, and never affects the other sources.
``ZONAPROP_STRATEGY=http`` exists to re-check whether the challenge is still
there; it is expected to report BLOCKED.

The parser is intentionally unimplemented: no page could be retrieved, and
writing a parser against markup nobody has seen would be a guess dressed up
as working code.
"""

from __future__ import annotations

import os
from typing import List

from monitor.models.listing import Listing, PropertyType
from monitor.models.result import ScrapeResult, SourceStatus
from monitor.neighborhoods import split_key
from monitor.scrapers.base import BaseScraper, FetchError

BASE_URL = "https://www.zonaprop.com.ar"
TYPE_SLUGS = {
    PropertyType.DEPARTAMENTO: "departamentos",
    PropertyType.CASA: "casas",
    PropertyType.PH: "ph",
}

DISABLED_REASON = (
    "disabled: ZonaProp serves a Cloudflare challenge (HTTP 403) to every "
    "HTTP client; set ZONAPROP_STRATEGY=provider to use a scraping provider"
)


class ZonaPropScraper(BaseScraper):
    name = "zonaprop"

    @property
    def strategy(self) -> str:
        return str(self.source_config.options.get("strategy", "disabled")).lower()

    def build_url(self, property_type: PropertyType, neighborhood_slug: str) -> str:
        """Search URL ZonaProp would answer if it were reachable."""
        return "{base}/{type_slug}-alquiler-{hood}-desde-{area}-m2-menos-{price}-pesos.html".format(
            base=BASE_URL,
            type_slug=TYPE_SLUGS.get(property_type, "departamentos"),
            hood=neighborhood_slug,
            area=int(self.criteria.min_area_m2),
            price=int(self.criteria.max_rent),
        )

    def scrape(self) -> ScrapeResult:
        if self.strategy == "disabled":
            return ScrapeResult(
                source=self.name,
                status=SourceStatus.DISABLED,
                error=DISABLED_REASON,
                partial=True,
            )
        return super().scrape()

    def collect(self) -> List[Listing]:
        if self.strategy == "provider":
            provider_url = os.getenv("ZONAPROP_PROVIDER_URL")
            api_key = os.getenv("ZONAPROP_API_KEY")
            if not provider_url or not api_key:
                raise FetchError(
                    "ZONAPROP_STRATEGY=provider requires ZONAPROP_PROVIDER_URL and "
                    "ZONAPROP_API_KEY; see README"
                )
            raise FetchError(
                "no scraping provider adapter is bundled: implement "
                "monitor/scrapers/zonaprop/provider.py for your provider's API"
            )

        # strategy == "http": a direct fetch, which is expected to be blocked.
        slug = "recoleta"
        for key in self.criteria.neighborhoods:
            location = split_key(key)
            slugs = self.resolver.slugs(self.name, location.city, location.neighborhood)
            if slugs:
                slug = slugs[0]
                break
        self.fetch(self.build_url(self.criteria.property_types[0], slug))
        raise FetchError(
            "ZonaProp returned a page but no parser exists for its markup; "
            "see monitor/scrapers/zonaprop/scraper.py"
        )
