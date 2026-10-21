"""HTTP plumbing and the scraper base class.

The scrapers know nothing about SQLite or Telegram: they return
``ScrapeResult`` objects and never raise into the scheduler.
"""

from __future__ import annotations

import abc
import logging
import random
import threading
import time
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit

import requests

from monitor.config import Config, SourceConfig
from monitor.logging_setup import log_context
from monitor.models.listing import Listing
from monitor.models.result import ScrapeResult, SourceStatus
from monitor.neighborhoods import NeighborhoodResolver

logger = logging.getLogger(__name__)


class ScraperError(RuntimeError):
    """Any failure that should be reported but must not stop the service."""


class FetchError(ScraperError):
    """The page could not be retrieved (network error, 5xx, bad status)."""


class BlockedError(ScraperError):
    """The site actively refused us (anti-bot challenge or rate limiting).

    Kept distinct from FetchError because a blocked source must never be
    reported as "0 listings found" - that would look like "nothing matched".
    """


class HttpClient:
    """Requests session with timeouts, bounded retries and per-host pacing."""

    def __init__(
        self,
        user_agent: str,
        connect_timeout: float = 10.0,
        read_timeout: float = 20.0,
        max_retries: int = 3,
        default_delay: float = 1.0,
    ) -> None:
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": user_agent,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "es-AR,es;q=0.9,en;q=0.8",
                "Upgrade-Insecure-Requests": "1",
                "Sec-Fetch-Dest": "document",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "none",
            }
        )
        self.connect_timeout = connect_timeout
        self.read_timeout = read_timeout
        self.max_retries = max(1, max_retries)
        self.default_delay = default_delay
        self._last_request: Dict[str, float] = {}
        self._lock = threading.Lock()

    def _respect_delay(self, url: str, delay: float) -> None:
        """Keep at least `delay` seconds between requests to the same host."""
        host = urlsplit(url).netloc
        with self._lock:
            last = self._last_request.get(host)
            now = time.monotonic()
            if last is not None:
                wait = delay - (now - last)
                if wait > 0:
                    time.sleep(wait)
            self._last_request[host] = time.monotonic()

    def get(
        self,
        url: str,
        delay: Optional[float] = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> str:
        return self._request("GET", url, delay, headers)

    def post_json(
        self,
        url: str,
        payload: Any,
        delay: Optional[float] = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> str:
        """POST a JSON body; same pacing, retries and block detection as get()."""
        return self._request("POST", url, delay, headers, payload)

    def _request(
        self,
        method: str,
        url: str,
        delay: Optional[float],
        headers: Optional[Dict[str, str]],
        payload: Any = None,
    ) -> str:
        last_error: Optional[Exception] = None
        for attempt in range(1, self.max_retries + 1):
            self._respect_delay(url, self.default_delay if delay is None else delay)
            try:
                if method == "POST":
                    response = self.session.post(
                        url,
                        timeout=(self.connect_timeout, self.read_timeout),
                        headers=headers,
                        json=payload,
                        allow_redirects=True,
                    )
                else:
                    response = self.session.get(
                        url,
                        timeout=(self.connect_timeout, self.read_timeout),
                        headers=headers,
                        allow_redirects=True,
                    )
            except requests.RequestException as exc:
                last_error = FetchError(f"request failed: {exc}")
            else:
                blocked = self._detect_block(response)
                if blocked:
                    # Retrying an anti-bot block immediately only deepens it.
                    raise BlockedError(blocked)
                if response.status_code >= 500 or response.status_code == 429:
                    last_error = FetchError(f"HTTP {response.status_code}")
                elif response.status_code >= 400:
                    raise FetchError(f"HTTP {response.status_code} for {url}")
                else:
                    return response.text

            if attempt < self.max_retries:
                backoff = min(30.0, (2**attempt)) + random.uniform(0, 1.0)
                log_context(
                    logger,
                    logging.WARNING,
                    "request failed, retrying",
                    url=url,
                    attempt=attempt,
                    backoff=round(backoff, 1),
                    error=str(last_error),
                )
                time.sleep(backoff)
        raise last_error if last_error else FetchError(f"unknown error for {url}")

    @staticmethod
    def _detect_block(response: requests.Response) -> Optional[str]:
        """Recognize anti-bot responses so they are reported, not parsed."""
        body = response.text or ""
        if response.status_code == 403 and (
            "cf-mitigated" in {k.lower() for k in response.headers}
            or "just a moment" in body[:2000].lower()
        ):
            return "Cloudflare challenge (HTTP 403)"
        if response.status_code == 202 and len(body.strip()) == 0:
            # AWS WAF answers 202 with an empty body when throttling.
            return "WAF throttling (HTTP 202, empty body)"
        if response.status_code == 429:
            return "rate limited (HTTP 429)"
        if "captcha" in body[:2000].lower() and response.status_code != 200:
            return f"captcha challenge (HTTP {response.status_code})"
        return None


class BaseScraper(abc.ABC):
    """Base class every source scraper implements.

    Subclasses implement :meth:`collect` and may raise; :meth:`scrape`
    converts failures into a ``ScrapeResult`` so one broken source can never
    take down the monitoring cycle.
    """

    name: str = "base"

    def __init__(
        self,
        config: Config,
        source_config: SourceConfig,
        resolver: NeighborhoodResolver,
        http: HttpClient,
    ) -> None:
        self.config = config
        self.source_config = source_config
        self.resolver = resolver
        self.http = http
        self.logger = logging.getLogger(f"monitor.scrapers.{self.name}")
        self._requests_made = 0

    def fetch(self, url: str) -> str:
        self._requests_made += 1
        self.logger.debug("fetching %s", url)
        return self.http.get(url, delay=self.source_config.request_delay_seconds)

    @abc.abstractmethod
    def collect(self) -> List[Listing]:
        """Fetch and normalize listings for the configured search criteria."""

    def scrape(self) -> ScrapeResult:
        self._requests_made = 0
        try:
            listings = self.collect()
        except BlockedError as exc:
            log_context(
                self.logger, logging.WARNING, "source blocked", source=self.name, reason=str(exc)
            )
            return ScrapeResult(
                source=self.name,
                status=SourceStatus.BLOCKED,
                error=str(exc),
                requests_made=self._requests_made,
                partial=True,
            )
        except ScraperError as exc:
            log_context(
                self.logger, logging.ERROR, "scraper error", source=self.name, error=str(exc)
            )
            return ScrapeResult(
                source=self.name,
                status=SourceStatus.ERROR,
                error=str(exc),
                requests_made=self._requests_made,
                partial=True,
            )
        except Exception as exc:  # noqa: BLE001 - last line of defence per source
            self.logger.exception("unexpected error in %s scraper", self.name)
            return ScrapeResult(
                source=self.name,
                status=SourceStatus.ERROR,
                error=f"{type(exc).__name__}: {exc}",
                requests_made=self._requests_made,
                partial=True,
            )
        return ScrapeResult(
            source=self.name,
            status=SourceStatus.OK,
            listings=listings,
            requests_made=self._requests_made,
        )
