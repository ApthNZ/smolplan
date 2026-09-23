"""Allocation engine.

Pure module: no web framework, no database. Everything is plain dicts and
integers. FTE is stored as integer hundredths throughout (0.50 FTE = 50) so
that arithmetic is exact.

Months are "YYYY-MM" strings on the outside, integer month indices on the
inside.
"""

from __future__ import annotations

import re
from collections import defaultdict

MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")

GREEN = "green"
RED = "red"
PAST = "past"
ARCHIVED = "archived"

# Shortfall / warning reasons
NO_SUPPLY_DATA = "no_supply_data"
OVER_ALLOCATED = "over_allocated"
RESERVE_EXCEEDS_SUPPLY = "reserve_exceeds_supply"

# Why a bar cannot move later (its end_limit)
CAPACITY = "capacity"
DEADLINE = "deadline"

MONTH_NAMES = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


# --- month helpers -----------------------------------------------------------


def month_index(month: str) -> int:
    if not MONTH_RE.match(month or ""):
        raise ValueError(f"bad month: {month!r}")
    year, mon = month.split("-")
    return int(year) * 12 + int(mon) - 1


def index_month(index: int) -> str:
    return f"{index // 12:04d}-{index % 12 + 1:02d}"


def month_add(month: str, n: int) -> str:
    return index_month(month_index(month) + n)


def month_span(start: str, end: str) -> list[str]:
    """Inclusive list of months from start to end."""
    a, b = month_index(start), month_index(end)
    return [index_month(i) for i in range(a, b + 1)]


def month_name(month: str) -> str:
    """"2026-09" as "Sep 2026", for any sentence a person reads.

    Spelled out from a fixed list rather than strftime("%b"), which follows the
    server's locale, so a message says the same thing as the page it lands on.
    """
    index = month_index(month)
    return f"{MONTH_NAMES[index % 12]} {index // 12}"


# --- allocation --------------------------------------------------------------


def allocate(
    teams,
    supply,
    reserves,
    initiatives,
    demand,
    current_month,
    horizon_months=24,
):
    """Allocate reserves and then initiatives in rank order.

    teams        list of team ids
    supply       {(team_id, month): fte_h}
    reserves     list of {reserve_id, name, team_id, month, fte_h}
    initiatives  list of {id, name, rank, start_month, archived,
                          duration, deadline_month}
    demand       list of {initiative_id, team_id, offset, fte_h}

    `duration` and `deadline_month` are optional: a missing duration is read
    off the demand (the last offset plus one), and a missing or empty
    deadline is none.

    Returns {"months": [...], "teams": [...], "cells": {...}, "initiatives": {...}}
    where cells is keyed by (team_id, month), and each initiative carries its
    status, its shortfalls, and the two edge readings described in _edges.
    """
    cur = month_index(current_month)
    teams = list(teams)

    demand_by_initiative = defaultdict(list)
    for line in demand:
        demand_by_initiative[line["initiative_id"]].append(line)

    live = sorted(
        (i for i in initiatives if not i.get("archived")),
        key=lambda i: i["rank"],
    )

    months = _window(supply, reserves, live, demand_by_initiative, cur, horizon_months)

    # R2/R9/R10: reserves come off the top, missing supply is zero, and
    # available capacity clamps at zero.
    cells = {}
    for team in teams:
        for m in months:
            month = index_month(m)
            raw = supply.get((team, month))
            reserved = 0
            consumers = []
            for line in reserves:
                if line["team_id"] == team and line["month"] == month:
                    reserved += line["fte_h"]
                    consumers.append(
                        {
                            "kind": "reserve",
                            "id": line["reserve_id"],
                            "name": line["name"],
                            "fte_h": line["fte_h"],
                        }
                    )
            have = raw or 0
            warnings = []
            if reserved > have:
                warnings.append(RESERVE_EXCEEDS_SUPPLY)
            cells[(team, month)] = {
                "supply": have,
                "has_supply_row": raw is not None,
                "reserved": reserved,
                "allocated": 0,
                "free": max(0, have - reserved),
                "warnings": warnings,
                "consumers": consumers,
            }

    results = {}
    for initiative in initiatives:
        if initiative.get("archived"):
            results[initiative["id"]] = _outcome(ARCHIVED)

    for initiative in live:
        lines = demand_by_initiative.get(initiative["id"], [])
        start = month_index(initiative["start_month"])
        needs = _needs(lines, start, cur)

        # R7: past means the stored end is behind the clock, not merely the
        # demand. An initiative can run on for months in which it needs
        # nobody, and until its end it is still live: green, on its deadline
        # if it has one, and drawn as a bar rather than as ended.
        if not needs and start + _duration(initiative, lines) - 1 < cur:
            results[initiative["id"]] = _outcome(PAST)
            continue

        shortfalls = []
        for (team, month), want in sorted(needs.items()):
            cell = cells.get((team, month))
            free = cell["free"] if cell else 0
            if want > free:
                # Who already holds this cell's capacity. Initiatives are
                # allocated in rank order, so at this point the cell's consumers
                # are exactly the reserves plus the higher-ranked work that got
                # in first — which is the answer to "why is this red". Copied,
                # because the list keeps growing as lower-ranked work lands.
                shortfalls.append(
                    {
                        "team_id": team,
                        "month": month,
                        "short": want - free,
                        "reason": (
                            OVER_ALLOCATED
                            if cell and cell["has_supply_row"]
                            else NO_SUPPLY_DATA
                        ),
                        "wanted": want,
                        "supply": cell["supply"] if cell else 0,
                        "taken_by": [dict(c) for c in cell["consumers"]] if cell else [],
                    }
                )

        status = RED if shortfalls else GREEN
        # Read before this initiative takes its own share, while the cells
        # hold exactly the reserves and the higher-ranked green work — the
        # same base fit_hints builds for the drag shading, without running
        # the allocation again for every bar.
        results[initiative["id"]] = _outcome(
            status, shortfalls, *_edges(initiative, lines, start, status, cells, cur)
        )

        # R4: all or nothing. A red initiative consumes zero everywhere.
        if shortfalls:
            continue

        for (team, month), want in needs.items():
            cell = cells[(team, month)]
            cell["allocated"] += want
            cell["free"] -= want
            cell["consumers"].append(
                {
                    "kind": "initiative",
                    "id": initiative["id"],
                    "name": initiative["name"],
                    "fte_h": want,
                }
            )

    return {
        "months": [index_month(m) for m in months],
        "teams": teams,
        "cells": cells,
        "initiatives": results,
    }


def _window(supply, reserves, live, demand_by_initiative, cur, horizon_months):
    """Every month we might need a cell for, from the current month onwards.

    The horizon controls the display range; it does not limit evaluation, so
    demand running past the end of supply still gets a cell (and a
    "no supply data" shortfall).
    """
    indices = set(range(cur, cur + horizon_months))
    for _, month in supply:
        indices.add(month_index(month))
    for line in reserves:
        indices.add(month_index(line["month"]))
    for initiative in live:
        start = month_index(initiative["start_month"])
        for line in demand_by_initiative.get(initiative["id"], []):
            indices.add(start + line["offset"])
    return sorted(i for i in indices if i >= cur)


def _outcome(status, shortfalls=(), earliest_start=None, end_limit=None):
    return {
        "status": status,
        "shortfalls": list(shortfalls),
        "earliest_start": earliest_start,
        "end_limit": end_limit,
    }


def _duration(initiative, lines) -> int:
    """How many months the initiative runs, start inclusive.

    Stored with the initiative. Read off the demand only for a caller that
    predates the stored end, which is what the end used to be.
    """
    if initiative.get("duration"):
        return initiative["duration"]
    return max((line["offset"] for line in lines), default=0) + 1


def _last_start(initiative, lines) -> int | None:
    """The latest start that still ends by the deadline; None without a deadline."""
    deadline = initiative.get("deadline_month")
    if not deadline:
        return None
    return month_index(deadline) - _duration(initiative, lines) + 1


def _needs(lines, start, cur) -> dict:
    """What a demand profile asks of each (team, month), if it starts at `start`.

    R7: only current and future months are evaluated.
    """
    needs = defaultdict(int)
    for line in lines:
        when = start + line["offset"]
        if when >= cur:
            needs[(line["team_id"], index_month(when))] += line["fte_h"]
    return needs


def _fits(lines, start, cells, cur) -> bool:
    """Would this profile, started at `start`, be fully met by what `cells` has free?

    The one test behind the drag shading and both edge glows, so the three
    can never disagree about what "would be green" means. A month with no
    cell has nothing free (R9).
    """
    return all(
        want <= (cells[key]["free"] if key in cells else 0)
        for key, want in _needs(lines, start, cur).items()
    )


def _edges(initiative, lines, start, status, cells, cur):
    """(earliest_start, end_limit): which ends of the bar have room, or none.

    earliest_start is the earliest month from now on at which a green
    initiative that has not started yet would still be green. end_limit says
    why it cannot move later: "deadline" when it already ends on (or past)
    its deadline, which no amount of capacity changes, so a red initiative
    gets it too; otherwise "capacity" when starting one month later would
    turn it red.

    `cells` must hold exactly the reserves and the higher-ranked green work.
    Lower-ranked work is ignored, as it is for the drag shading: this
    initiative would pre-empt it, so moving there can turn that work red.
    """
    earliest_start = None
    end_limit = None

    last = _last_start(initiative, lines)
    if last is not None and start >= last:
        end_limit = DEADLINE

    if status != GREEN:
        return earliest_start, end_limit

    if end_limit is None and not _fits(lines, start + 1, cells, cur):
        end_limit = CAPACITY

    for candidate in range(cur, start):
        if _fits(lines, candidate, cells, cur):
            earliest_start = index_month(candidate)
            break

    return earliest_start, end_limit


def fit_hints(
    teams,
    supply,
    reserves,
    initiatives,
    demand,
    current_month,
    initiative_id,
    horizon_months=24,
):
    """Start months where this initiative would be green.

    Allocation is run over the initiatives ranked above it only: lower-ranked
    work is ignored because this one preempts it. What it would displace shows
    up as those initiatives turning red once the move is made.

    A start that would carry the end past the deadline is never offered, fit
    or not: the deadline is a hard limit, not a preference.
    """
    target = next((i for i in initiatives if i["id"] == initiative_id), None)
    if target is None or target.get("archived"):
        return []

    higher = [
        i
        for i in initiatives
        if not i.get("archived")
        and i["id"] != initiative_id
        and i["rank"] < target["rank"]
    ]
    base = allocate(
        teams, supply, reserves, higher, demand, current_month, horizon_months
    )
    lines = [d for d in demand if d["initiative_id"] == initiative_id]
    last = _last_start(target, lines)

    cur = month_index(current_month)
    return [
        index_month(start)
        for start in range(cur, cur + horizon_months)
        if (last is None or start <= last) and _fits(lines, start, base["cells"], cur)
    ]
