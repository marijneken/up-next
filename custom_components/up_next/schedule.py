"""Auto-start planning: when to start a held show so you never wait for an episode.

Model: you watch one episode per evening. Each evening the highest-priority show in
the list that is active and has an unwatched episode out gets the slot. A show on
auto-start begins on the earliest day from which every evening it gets has an
episode out, right up to the season finale.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

NEVER = date.max  # "not playing in this window"
MAX_LOOKAHEAD = 400  # days


@dataclass
class Lane:
    """One show as the simulator sees it."""

    sid: str
    days: list[date]  # release day of each unwatched episode, in order
    start: date  # first evening it may be played (NEVER = held)


@dataclass
class Plan:
    """Result for a show on auto-start."""

    start: date | None  # None: can't plan yet (finale date unknown)
    finale: date | None  # the finale's release day
    season: int | None
    episodes: int = 0  # episodes to watch up to and including the finale
    watch_finale: date | None = None  # evening you'll actually watch it


def simulate(lanes: list[Lane], first: date, last: date) -> dict[str, list[date]]:
    """Which evening each lane's episodes get watched (priority = list order)."""
    pos = {lane.sid: 0 for lane in lanes}
    watched: dict[str, list[date]] = {lane.sid: [] for lane in lanes}
    day = first
    while day <= last:
        for lane in lanes:
            i = pos[lane.sid]
            if lane.start <= day and i < len(lane.days) and lane.days[i] <= day:
                watched[lane.sid].append(day)
                pos[lane.sid] = i + 1
                break
        day += timedelta(days=1)
    return watched


def _gaps(
    lanes: list[Lane], sid: str, count: int, first: date, last: date
) -> tuple[int, date | None, date | None]:
    """Simulate; count evenings `sid` was free to play but had nothing out yet.

    Returns (waits, evening it first gets played, evening its `count`-th episode
    gets watched).
    """
    pos = {lane.sid: 0 for lane in lanes}
    waits = 0
    began: date | None = None
    day = first
    while day <= last:
        for lane in lanes:
            i = pos[lane.sid]
            if lane.start > day or i >= len(lane.days):
                continue
            if lane.days[i] <= day:
                pos[lane.sid] = i + 1
                if lane.sid == sid:
                    began = began or day
                    if i + 1 == count:
                        return waits, began, day
                break
            if lane.sid == sid:
                waits += 1  # our evening, but the next episode isn't out yet
        day += timedelta(days=1)
    return waits, began, None


def latest_start(
    higher: list[Lane], days: list[date], finale_index: int, today: date
) -> tuple[date, date | None]:
    """Earliest start day from which you never have to wait for an episode.

    Every evening the show gets (higher shows first) there's an episode out, all the
    way to the finale. That lands the finale as close to its release as possible
    without a gap. Higher shows may hold it back past that day (a backlog), so the
    start reported is the first evening it actually gets played.
    Returns (start day, evening the finale gets watched).
    """
    if days[finale_index] <= today:
        return today, None  # whole season is out: just start
    sid = "\0target"
    target = days[: finale_index + 1]
    count = finale_index + 1
    horizon = days[finale_index] + timedelta(days=MAX_LOOKAHEAD)
    day = today
    while day <= days[finale_index]:
        waits, began, watched = _gaps([*higher, Lane(sid, target, day)], sid, count, today, horizon)
        if not waits:
            return began or day, watched
        day += timedelta(days=1)
    _, began, watched = _gaps([*higher, Lane(sid, target, day)], sid, count, today, horizon)
    return began or day, watched
