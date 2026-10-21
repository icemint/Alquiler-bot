"""Each subscriber's own search.

A subscriber who never opened /settings searches with the configured default
criteria. The sites are asked for the union of everyone's searches; each
subscriber's filter then picks their own listings out of that.
"""

from __future__ import annotations

from typing import Dict

from monitor.config import SearchCriteria
from monitor.database import Database


def criteria_for(database: Database, chat_id: str, default: SearchCriteria) -> SearchCriteria:
    stored = database.get_criteria(chat_id) if chat_id else None
    return default.with_changes(stored) if stored else default


def save_criteria(database: Database, chat_id: str, criteria: SearchCriteria) -> None:
    database.set_criteria(chat_id, criteria.to_dict())


def subscriber_criteria(database: Database, default: SearchCriteria) -> Dict[str, SearchCriteria]:
    """Active subscribers and their searches, in subscription order."""
    return {
        chat_id: criteria_for(database, chat_id, default)
        for chat_id in database.active_subscribers()
    }


def search_scope(database: Database, default: SearchCriteria) -> SearchCriteria:
    """What to ask the sites for: every active subscriber's search combined."""
    searches = list(subscriber_criteria(database, default).values())
    return SearchCriteria.union(searches) if searches else default
