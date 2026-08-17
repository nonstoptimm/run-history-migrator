"""Shared activity selection rules for inspect, convert, and upload."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Protocol


class SelectableActivity(Protocol):
    """Minimum activity fields required by the common selector.

    Attributes:
        session_id: Canonical adidas session UUID, used as a tie-breaker
            for deterministic ordering and for `session_id` matching.
        local_start_date: Local calendar date used for `DateRange`
            filtering.
        sport_type_id: Raw adidas sport type identifier, matched against
            `SPORT_TYPE_IDS`.
    """

    @property
    def session_id(self) -> str:
        """Return the canonical adidas session UUID."""

    @property
    def local_start_date(self) -> date:
        """Return the local calendar date used for filtering."""

    @property
    def sport_type_id(self) -> str:
        """Return the raw adidas sport type identifier."""


# Maps a user-facing `--sport` name to the raw adidas sport type IDs
# considered equivalent to it. Only "running" (adidas sport type "1") is
# currently supported.
SPORT_TYPE_IDS: dict[str, frozenset[str]] = {"running": frozenset({"1"})}


@dataclass(frozen=True)
class DateRange:
    """Inclusive optional local-calendar date bounds.

    Both bounds are inclusive: a date equal to `since` or `until` is
    considered contained. Either bound may be `None` to leave that side
    unbounded; leaving both `None` matches every date.

    Attributes:
        since: Earliest local calendar date to include, or `None` for no
            lower bound.
        until: Latest local calendar date to include, or `None` for no
            upper bound.
    """

    since: date | None = None
    until: date | None = None

    def __post_init__(self) -> None:
        """Reject an inverted range.

        Raises:
            ValueError: If both bounds are set and `since` is after
                `until`.
        """
        if self.since and self.until and self.since > self.until:
            raise ValueError("--since must be on or before --until")

    def contains(self, value: date) -> bool:
        """Return whether `value` falls within the inclusive bounds."""
        if self.since and value < self.since:
            return False
        return not self.until or value <= self.until


def validate_sport(sport: str) -> frozenset[str]:
    """Return supported adidas sport IDs or raise a user-facing error.

    Args:
        sport: User-facing sport name, for example `"running"`.

    Returns:
        The set of raw adidas `sport_type_id` values that map to `sport`.

    Raises:
        ValueError: If `sport` is not a supported sport name.
    """
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
    """Filter and deterministically order activities, applying limit last.

    Args:
        activities: Candidate activities to filter.
        date_range: Inclusive local-date bounds; see `DateRange`.
        sport: User-facing sport name passed to `validate_sport`.
        session_id: If set, keep only the activity with this exact
            canonical adidas session UUID (exact match, no partial or
            case-insensitive matching).
        limit: If set, keep only the first `limit` results after
            filtering and sorting.

    Returns:
        Matching activities sorted by `(local_start_date, session_id)`,
        with `limit` applied last so it never affects which activities
        are considered eligible, only how many are returned.

    Raises:
        ValueError: If `sport` is not a supported sport name.
    """
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
