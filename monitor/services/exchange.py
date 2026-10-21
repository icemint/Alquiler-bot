"""The USD -> ARS rate, so a listing priced in dollars can be held against a
budget in pesos.

The rate comes from dolarapi.com (public, no key): the selling price of the
chosen quote, "oficial" by default. It is fetched at most once an hour, and
only when a dollar listing actually needs it. USD_ARS_RATE fixes the rate
instead. If no rate can be had, dollar listings are rejected as before - a
budget is never compared against an unconverted amount.
"""

from __future__ import annotations

import logging
import time
from typing import Optional

import requests

from monitor.logging_setup import log_context

logger = logging.getLogger(__name__)

RATE_URL = "https://dolarapi.com/v1/dolares/{source}"
REFRESH_SECONDS = 3600


class UsdRate:
    def __init__(
        self,
        source: str = "oficial",
        fixed: Optional[float] = None,
        session: Optional[requests.Session] = None,
        timeout: float = 10.0,
    ) -> None:
        self.source = source
        self.fixed = fixed
        self.session = session or requests.Session()
        self.timeout = timeout
        self._rate: Optional[float] = None
        self._fetched_at: Optional[float] = None

    def current(self) -> Optional[float]:
        """ARS per USD, or None when unknown."""
        if self.fixed:
            return self.fixed
        fresh = self._fetched_at is not None and time.time() - self._fetched_at < REFRESH_SECONDS
        if not fresh:
            # A failed refresh keeps the last rate: an hour-old rate is far
            # better than rejecting every dollar listing.
            self._fetched_at = time.time()
            self._rate = self._fetch() or self._rate
        return self._rate

    def _fetch(self) -> Optional[float]:
        try:
            response = self.session.get(RATE_URL.format(source=self.source), timeout=self.timeout)
            response.raise_for_status()
            rate = float(response.json()["venta"])
        except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
            log_context(
                logger, logging.WARNING, "USD rate unavailable", source=self.source, error=str(exc)
            )
            return None
        if rate <= 0:
            return None
        log_context(logger, logging.INFO, "USD rate", source=self.source, ars=rate)
        return rate
