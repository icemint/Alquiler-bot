"""The monitoring loop.

One cycle = ask every enabled source once, filter, deduplicate, notify. A
source that fails is isolated: its exception never leaves ``scrape()``, it is
recorded in ``source_state`` and backed off, and the remaining sources still
complete the cycle.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Dict, List, Optional, Tuple

from monitor.config import Config
from monitor.database import Database
from monitor.logging_setup import log_context
from monitor.models.listing import Listing
from monitor.models.result import ScrapeResult, SourceStatus
from monitor.neighborhoods import NeighborhoodResolver
from monitor.scrapers import BaseScraper, build_scrapers
from monitor.services.deduplication import Deduplicator
from monitor.services.filtering import FilterDecision, ListingFilter
from monitor.services.notification import NotificationService
from monitor.services.notification.notifier import Notifier, build_notifier
from monitor.timeutil import format_local, utcnow

logger = logging.getLogger("monitor.scheduler")

# A blocked source waits this long times the number of consecutive failures,
# capped, before it is tried again.
BLOCK_BACKOFF_BASE = timedelta(minutes=15)
BLOCK_BACKOFF_MAX = timedelta(hours=2)
BOOTSTRAP_KEY = "bootstrap_completed_at"


@dataclass
class SourceReport:
    source: str
    status: SourceStatus
    found: int = 0
    matched: int = 0
    new: int = 0
    known: int = 0
    error: Optional[str] = None
    reject_reasons: Dict[str, int] = field(default_factory=dict)

    @property
    def label(self) -> str:
        from monitor.services.notification.formatter import source_label

        return source_label(self.source)


@dataclass
class CycleReport:
    sources: List[SourceReport] = field(default_factory=list)
    notifications_sent: int = 0
    suppressed_on_bootstrap: int = 0

    @property
    def found(self) -> int:
        return sum(report.found for report in self.sources)

    @property
    def matched(self) -> int:
        return sum(report.matched for report in self.sources)

    @property
    def new(self) -> int:
        return sum(report.new for report in self.sources)

    @property
    def sources_ok(self) -> int:
        return sum(1 for report in self.sources if report.status is SourceStatus.OK)


class MonitorService:
    def __init__(
        self,
        config: Config,
        database: Database,
        scrapers: Optional[List[BaseScraper]] = None,
        notifier: Optional[Notifier] = None,
    ) -> None:
        self.config = config
        self.database = database
        self.resolver = NeighborhoodResolver.from_file(config.neighborhood_map_file)
        self.scrapers = scrapers if scrapers is not None else build_scrapers(config, self.resolver)
        self.filter = ListingFilter(config.criteria)
        self.deduplicator = Deduplicator(database)
        self.notifier = notifier or build_notifier(config.telegram, config.email, database)
        self.notifications = NotificationService(database, self.notifier, config.telegram)
        self._stop = threading.Event()

    # ------------------------------------------------------------------ helpers

    def _should_skip(self, scraper: BaseScraper) -> Optional[str]:
        """Per-source pacing: WAF backoff and source-specific intervals."""
        now = utcnow()
        blocked_until = self.database.source_blocked_until(scraper.name)
        if blocked_until and blocked_until > now:
            return "backing off until {0}".format(format_local(blocked_until, "%H:%M"))

        min_interval = scraper.source_config.min_interval_seconds
        if min_interval:
            state = self.database.source_states().get(scraper.name)
            last_attempt = state.get("last_attempt_at") if state else None
            if last_attempt:
                from monitor.timeutil import from_iso

                parsed = from_iso(last_attempt)
                if parsed and (now - parsed).total_seconds() < min_interval:
                    remaining = int(min_interval - (now - parsed).total_seconds())
                    return f"min interval not elapsed ({remaining}s remaining)"
        return None

    def _backoff_for(self, source: str) -> timedelta:
        state = self.database.source_states().get(source, {})
        failures = int(state.get("consecutive_failures", 0)) + 1
        return min(BLOCK_BACKOFF_BASE * failures, BLOCK_BACKOFF_MAX)

    def _evaluate(
        self, listings: List[Listing]
    ) -> Tuple[List[Tuple[Listing, FilterDecision]], Dict[str, int]]:
        decisions: List[Tuple[Listing, FilterDecision]] = []
        reasons: Dict[str, int] = {}
        for listing in listings:
            decision = self.filter.evaluate(listing)
            decisions.append((listing, decision))
            if not decision.accepted and decision.reason_value:
                reasons[decision.reason_value] = reasons.get(decision.reason_value, 0) + 1
        return decisions, reasons

    # ------------------------------------------------------------------ cycle

    def run_cycle(self) -> CycleReport:
        cycle_id = self.database.start_cycle()
        report = CycleReport()
        errors: List[str] = []

        for scraper in self.scrapers:
            skip_reason = self._should_skip(scraper)
            if skip_reason:
                report.sources.append(
                    SourceReport(scraper.name, SourceStatus.SKIPPED, error=skip_reason)
                )
                log_context(
                    logger, logging.INFO, "source skipped", source=scraper.name, reason=skip_reason
                )
                continue

            result: ScrapeResult = scraper.scrape()
            source_report = self._handle_result(result)
            report.sources.append(source_report)

            if result.error:
                errors.append(f"{result.source}: {result.error}")

        # Suppressed listings are already marked as notified, so the other
        # sources' new listings can still go out in this cycle.
        report.suppressed_on_bootstrap = self._bootstrap_if_needed(report)
        report.notifications_sent = self.notifications.notify_pending()

        self.database.finish_cycle(
            cycle_id,
            sources_total=len(self.scrapers),
            sources_ok=report.sources_ok,
            found=report.found,
            matched=report.matched,
            new_listings=report.new,
            notified=report.notifications_sent,
            errors="; ".join(errors) if errors else None,
        )
        self._log_cycle(report)
        return report

    def _handle_result(self, result: ScrapeResult) -> SourceReport:
        if result.status is not SourceStatus.OK:
            block_for = (
                self._backoff_for(result.source)
                if result.status
                in (
                    SourceStatus.BLOCKED,
                    SourceStatus.ERROR,
                )
                else None
            )
            self.database.update_source_state(
                result.source,
                result.status.value,
                result.error,
                found=0,
                new_listings=0,
                block_for=block_for,
            )
            return SourceReport(source=result.source, status=result.status, error=result.error)

        decisions, reasons = self._evaluate(result.listings)
        outcome = self.deduplicator.process(decisions)
        self.database.update_source_state(
            result.source,
            result.status.value,
            None,
            found=result.found,
            new_listings=outcome.new_count,
        )
        return SourceReport(
            source=result.source,
            status=result.status,
            found=result.found,
            matched=outcome.matched,
            new=outcome.new_count,
            known=outcome.known,
            reject_reasons=reasons,
        )

    def _bootstrap_if_needed(self, report: CycleReport) -> int:
        """A source's first successful run: record what exists, stay quiet.

        Tracked per source, so enabling a new source on an existing database
        does not flood subscribers with everything it already lists. A source
        that has notified before (it predates per-source tracking) is only
        marked, never suppressed.
        """
        if self.config.notify_on_first_run:
            return 0
        total = 0
        for source in report.sources:
            if source.status is not SourceStatus.OK:
                continue
            key = f"{BOOTSTRAP_KEY}:{source.source}"
            if self.database.get_state(key):
                continue
            if not self.database.has_notified_listings(source.source):
                pending = [
                    item.dedupe_key
                    for item in self.database.pending_notifications(limit=10_000)
                    if item.listing.source == source.source
                ]
                suppressed = self.database.suppress_notifications(pending)
                total += suppressed
                if suppressed:
                    log_context(
                        logger,
                        logging.INFO,
                        "first run: existing listings recorded without notifying",
                        source=source.source,
                        suppressed=suppressed,
                    )
            self.database.set_state(key, format_local(utcnow(), "%Y-%m-%dT%H:%M:%S"))
        return total

    def _log_cycle(self, report: CycleReport) -> None:
        for source in report.sources:
            if source.status is SourceStatus.OK:
                logger.info(
                    "%s: %s listings found, %s matching, %s new",
                    source.label,
                    source.found,
                    source.matched,
                    source.new,
                    extra={
                        "context": {
                            "source": source.source,
                            "found": source.found,
                            "matching": source.matched,
                            "new": source.new,
                            "known": source.known,
                            "rejected": source.reject_reasons or "-",
                        }
                    },
                )
            else:
                logger.warning(
                    "%s: %s (%s)",
                    source.label,
                    source.status.value,
                    source.error or "no detail",
                    extra={"context": {"source": source.source, "status": source.status.value}},
                )
        logger.info(
            "Notifications: %s sent",
            report.notifications_sent,
            extra={"context": {"sent": report.notifications_sent}},
        )

    # ------------------------------------------------------------------ loop

    def stop(self) -> None:
        self._stop.set()

    @property
    def stopping(self) -> bool:
        return self._stop.is_set()

    def run_forever(self) -> None:
        interval = self.config.check_interval_seconds
        log_context(
            logger,
            logging.INFO,
            "monitor started",
            interval_seconds=interval,
            sources=",".join(s.name for s in self.scrapers) or "none",
            criteria=self.config.criteria.describe(),
        )
        while not self._stop.is_set():
            started = utcnow()
            try:
                self.run_cycle()
            except Exception:  # noqa: BLE001 - the loop must survive anything
                logger.exception("monitoring cycle failed")
            elapsed = (utcnow() - started).total_seconds()
            wait = max(1.0, interval - elapsed)
            if self._stop.wait(wait):
                break
        logger.info("monitor stopped")
