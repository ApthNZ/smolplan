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
    initiatives  list of {id, name, rank, start_month, archived}
    demand       list of {initiative_id, team_id, offset, fte_h}

    Returns {"months": [...], "teams": [...], "cells": {...}, "initiatives": {...}}
    where cells is keyed by (team_id, month).
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
            results[initiative["id"]] = {"status": ARCHIVED, "shortfalls": []}

    for initiative in live:
        lines = demand_by_initiative.get(initiative["id"], [])
        start = month_index(initiative["start_month"])

        # R7: only current and future months are evaluated.
        needs = defaultdict(int)
        for line in lines:
            when = start + line["offset"]
            if when >= cur:
                needs[(line["team_id"], index_month(when))] += line["fte_h"]

        if not needs:
            status = PAST if lines else GREEN
            results[initiative["id"]] = {"status": status, "shortfalls": []}
            continue

        shortfalls = []
        for (team, month), want in sorted(needs.items()):
            cell = cells.get((team, month))
            free = cell["free"] if cell else 0
            if want > free:
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
                    }
                )

        # R4: all or nothing. A red initiative consumes zero everywhere.
        if shortfalls:
            results[initiative["id"]] = {"status": RED, "shortfalls": shortfalls}
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
        results[initiative["id"]] = {"status": GREEN, "shortfalls": []}

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

    cur = month_index(current_month)
    hints = []
    for start in range(cur, cur + horizon_months):
        need = defaultdict(int)
        for line in lines:
            when = start + line["offset"]
            if when >= cur:
                need[(line["team_id"], index_month(when))] += line["fte_h"]
        cells = base["cells"]
        if all(
            want <= (cells[key]["free"] if key in cells else 0)
            for key, want in need.items()
        ):
            hints.append(index_month(start))
    return hints
