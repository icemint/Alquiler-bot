"""Deduplication.

Two levels:

* within a cycle - the same advertisement can appear twice (two pages, or a
  listing indexed under two neighborhoods), deduplicated by ``dedupe_key``;
* across cycles and restarts - the database holds every key ever seen, so a
  listing is "new" exactly once in the lifetime of the database.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Tuple

from monitor.database import Database, StoredListing
from monitor.models.listing import Listing
from monitor.services.filtering import FilterDecision

logger = logging.getLogger(__name__)


@dataclass
class DedupeOutcome:
    new: List[StoredListing] = field(default_factory=list)
    known: int = 0
    duplicates_in_cycle: int = 0
    matched: int = 0
    # Previously stored listings that only now satisfy the criteria, e.g.
    # after a price drop. They are notified too, via the pending queue.
    reactivated: int = 0

    @property
    def new_count(self) -> int:
        return len(self.new)


class Deduplicator:
    def __init__(self, database: Database) -> None:
        self.database = database

    def process(self, decisions: List[Tuple[Listing, FilterDecision]]) -> DedupeOutcome:
        outcome = DedupeOutcome()
        seen_this_cycle: Dict[str, bool] = {}

        for listing, decision in decisions:
            key = listing.dedupe_key
            if key in seen_this_cycle:
                outcome.duplicates_in_cycle += 1
                continue
            seen_this_cycle[key] = True

            if decision.accepted:
                outcome.matched += 1

            is_new, stored = self.database.upsert_listing(
                listing, matched=decision.accepted, reject_reason=decision.reason_value
            )
            if is_new:
                if decision.accepted:
                    outcome.new.append(stored)
                    logger.debug("new matching listing %s", stored.dedupe_key)
            else:
                outcome.known += 1
                if decision.accepted and stored.notified_at is None:
                    # Became eligible later (e.g. the price dropped below the
                    # limit) and was never notified. The pending queue picks
                    # it up; it is not counted as a new discovery.
                    outcome.reactivated += 1
        return outcome
