"""Shared activity selection rules for inspect, convert, and upload."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Protocol


class SelectableActivity(Protocol):
    """Minimum activity fields required by the common selector."""

    session_id: str
    local_start_date: date
    sport_type_id: str


SPORT_TYPE_IDS: dict[str, frozenset[str]] = {"running": frozenset({"1"})}


@dataclass(frozen=True)
class DateRange:
    """Inclusive optional local-calendar date bounds."""

    since: date | None = None
    until: date | None = None

    def __post_init__(self) -> None:
        if self.since and self.until and self.since > self.until:
            raise ValueError("--since must be on or before --until")

    def contains(self, value: date) -> bool:
        if self.since and value < self.since:
            return False
        return not self.until or value <= self.until


def validate_sport(sport: str) -> frozenset[str]:
    """Return supported adidas sport IDs or raise a user-facing error."""
    try:
        return SPORT_TYPE_IDS[sport]
    except KeyError as exc:
        supported = ", ".join(sorted(SPORT_TYPE_IDS))
        raise ValueError(f"unsupported sport {sport!r}; supported: {supported}") from exc


def select_activities[T: SelectableActivity](
    activities: list[T],
    date_range: DateRange,
    sport: str,
    session_id: str | None = None,
    limit: int | None = None,
) -> list[T]:
    """Filter and deterministically order activities, applying limit last."""
    sport_ids = validate_sport(sport)
    selected = [
        activity
        for activity in activities
        if (not session_id or activity.session_id == session_id)
        and activity.sport_type_id in sport_ids
        and date_range.contains(activity.local_start_date)
    ]
    selected.sort(key=lambda item: (item.local_start_date, item.session_id))
    return selected if limit is None else selected[:limit]
