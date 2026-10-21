"""Filtering of normalized listings against the search criteria.

The guiding rule is the one from the brief: if the price or the area cannot
be determined reliably, the listing is not notified. Every rejection carries a
machine-readable reason so a cycle's log explains itself.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Callable, Optional

from monitor.config import BEDROOMS_OR_MORE, SearchCriteria
from monitor.models.listing import Listing, Operation


class RejectReason(str, Enum):
    OPERATION = "operation_not_alquiler"
    TEMPORARY_RENTAL = "temporary_rental"
    PROPERTY_TYPE = "property_type_not_allowed"
    NEIGHBORHOOD_UNKNOWN = "neighborhood_unknown"
    CITY_NOT_ALLOWED = "city_not_allowed"
    NEIGHBORHOOD_NOT_ALLOWED = "neighborhood_not_allowed"
    PRICE_MISSING = "price_missing"
    PRICE_UNRELIABLE = "price_unreliable"
    CURRENCY = "currency_not_allowed"
    PRICE_ABOVE_MAX = "price_above_max"
    AREA_MISSING = "area_missing"
    AREA_BELOW_MIN = "area_below_min"
    BEDROOMS_NOT_ALLOWED = "bedrooms_not_allowed"
    UNFURNISHED = "unfurnished"
    FURNISHED = "furnished"


@dataclass
class FilterDecision:
    accepted: bool
    reason: Optional[RejectReason] = None

    @property
    def reason_value(self) -> Optional[str]:
        return self.reason.value if self.reason else None


class ListingFilter:
    def __init__(
        self,
        criteria: SearchCriteria,
        usd_rate: Optional[Callable[[], Optional[float]]] = None,
    ) -> None:
        self.criteria = criteria
        # ARS per USD, asked only when a dollar listing comes up.
        self.usd_rate = usd_rate

    def _price_in_budget_currency(self, listing: Listing) -> Optional[float]:
        """The rent in the budget's currency, or None if it cannot be had."""
        if listing.currency == self.criteria.currency:
            return listing.price
        if (
            listing.currency == "USD"
            and self.criteria.currency == "ARS"
            and self.criteria.include_usd
            and self.usd_rate
        ):
            rate = self.usd_rate()
            if rate and listing.price is not None:
                return listing.price * rate
        return None

    def evaluate(self, listing: Listing) -> FilterDecision:
        criteria = self.criteria

        if listing.operation is Operation.ALQUILER_TEMPORAL:
            return FilterDecision(False, RejectReason.TEMPORARY_RENTAL)
        if listing.operation is not Operation.ALQUILER:
            return FilterDecision(False, RejectReason.OPERATION)

        if listing.property_type not in criteria.property_types:
            return FilterDecision(False, RejectReason.PROPERTY_TYPE)

        # Never fall back to substring matching on the address: city and
        # barrio must come from the site's structured fields.
        if not listing.city:
            return FilterDecision(False, RejectReason.NEIGHBORHOOD_UNKNOWN)
        if listing.city not in criteria.cities:
            return FilterDecision(False, RejectReason.CITY_NOT_ALLOWED)
        wanted = criteria.barrios_in(listing.city)
        if wanted:
            if not listing.neighborhood:
                return FilterDecision(False, RejectReason.NEIGHBORHOOD_UNKNOWN)
            if listing.neighborhood not in wanted:
                return FilterDecision(False, RejectReason.NEIGHBORHOOD_NOT_ALLOWED)

        if listing.price is None:
            return FilterDecision(False, RejectReason.PRICE_MISSING)
        if listing.price_is_approximate:
            # "desde" prices, promotional prices and ads covering several
            # units: the number shown is not this unit's rent.
            return FilterDecision(False, RejectReason.PRICE_UNRELIABLE)
        price = self._price_in_budget_currency(listing)
        if price is None:
            return FilterDecision(False, RejectReason.CURRENCY)
        if price <= 0:
            return FilterDecision(False, RejectReason.PRICE_UNRELIABLE)
        # Expensas are deliberately excluded from the price comparison.
        if price > criteria.max_rent:
            return FilterDecision(False, RejectReason.PRICE_ABOVE_MAX)

        if listing.area_m2 is None:
            return FilterDecision(False, RejectReason.AREA_MISSING)
        if listing.area_m2 < criteria.min_area_m2:
            return FilterDecision(False, RejectReason.AREA_BELOW_MIN)

        # Bedrooms and furnished only drop listings that state something
        # else. Many ads say nothing about them; those are notified and the
        # subscriber decides from the ad itself.
        if (
            criteria.bedrooms
            and listing.bedrooms is not None
            and min(listing.bedrooms, BEDROOMS_OR_MORE) not in criteria.bedrooms
        ):
            return FilterDecision(False, RejectReason.BEDROOMS_NOT_ALLOWED)
        if criteria.furnished == "yes" and listing.furnished is False:
            return FilterDecision(False, RejectReason.UNFURNISHED)
        if criteria.furnished == "no" and listing.furnished is True:
            return FilterDecision(False, RejectReason.FURNISHED)

        return FilterDecision(True)
