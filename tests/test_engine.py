"""Acceptance tests T1-T7 from the spec."""

import copy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from engine import allocate, fit_hints, month_span

CURRENT = "2027-01"
HORIZON = 24
SUPPLY_MONTHS = month_span("2027-01", "2028-12")


def fixture():
    """Section 10 fixture: SOC and GRC, initiatives A (1), B (2), C (3)."""
    teams = ["SOC", "GRC"]
    supply = {}
    reserves = []
    for month in SUPPLY_MONTHS:
        supply[("SOC", month)] = 200
        supply[("GRC", month)] = 150
        reserves.append(
            {"reserve_id": 1, "name": "BAU", "team_id": "SOC", "month": month, "fte_h": 100}
        )
        reserves.append(
            {"reserve_id": 1, "name": "BAU", "team_id": "GRC", "month": month, "fte_h": 50}
        )
        reserves.append(
            {
                "reserve_id": 2,
                "name": "Unplanned",
                "team_id": "GRC",
                "month": month,
                "fte_h": 25,
            }
        )

    initiatives = [
        {"id": "A", "name": "A", "rank": 1, "start_month": "2027-01", "archived": False},
        {"id": "B", "name": "B", "rank": 2, "start_month": "2027-02", "archived": False},
        {"id": "C", "name": "C", "rank": 3, "start_month": "2027-04", "archived": False},
    ]

    demand = []
    for offset in range(3):
        demand.append({"initiative_id": "A", "team_id": "SOC", "offset": offset, "fte_h": 100})
    for offset in range(6):
        demand.append({"initiative_id": "A", "team_id": "GRC", "offset": offset, "fte_h": 50})
    for offset in range(3):
        demand.append({"initiative_id": "B", "team_id": "GRC", "offset": offset, "fte_h": 50})
    for offset in range(3):
        demand.append({"initiative_id": "C", "team_id": "SOC", "offset": offset, "fte_h": 50})

    return teams, supply, reserves, initiatives, demand


def run(current=CURRENT, **overrides):
    teams, supply, reserves, initiatives, demand = fixture()
    args = {
        "teams": teams,
        "supply": supply,
        "reserves": reserves,
        "initiatives": initiatives,
        "demand": demand,
    }
    args.update(overrides)
    return allocate(current_month=current, horizon_months=HORIZON, **args)


def status(result, initiative_id):
    return result["initiatives"][initiative_id]["status"]


def shortfalls(result, initiative_id):
    return [
        (s["team_id"], s["month"], s["short"])
        for s in result["initiatives"][initiative_id]["shortfalls"]
    ]


# --- T1 ----------------------------------------------------------------------


def test_t1_baseline():
    result = run()
    assert status(result, "A") == "green"
    assert status(result, "B") == "red"
    assert shortfalls(result, "B") == [
        ("GRC", "2027-02", 25),
        ("GRC", "2027-03", 25),
        ("GRC", "2027-04", 25),
    ]
    assert status(result, "C") == "green"


def test_t1_red_initiative_consumes_nothing():
    result = run()
    # B is red, so GRC in Feb shows only the reserves and A.
    cell = result["cells"][("GRC", "2027-02")]
    assert cell["allocated"] == 50
    assert cell["free"] == 25
    assert [c["name"] for c in cell["consumers"]] == ["BAU", "Unplanned", "A"]


# --- T2 ----------------------------------------------------------------------


def test_t2_drag_to_fit():
    _, _, _, initiatives, _ = fixture()
    before = run(initiatives=initiatives)
    moved = copy.deepcopy(initiatives)
    moved[1]["start_month"] = "2027-07"
    after = run(initiatives=moved)

    assert status(before, "B") == "red"
    assert status(after, "B") == "green"
    newly_green = [
        i
        for i in after["initiatives"]
        if after["initiatives"][i]["status"] == "green"
        and before["initiatives"][i]["status"] == "red"
    ]
    assert newly_green == ["B"]


# --- T3 ----------------------------------------------------------------------


def test_t3_fit_hints():
    teams, supply, reserves, initiatives, demand = fixture()
    hints = fit_hints(
        teams, supply, reserves, initiatives, demand, CURRENT, "B", HORIZON
    )
    assert hints == month_span("2027-07", "2028-10")


def test_t3_hints_ignore_lower_ranked_work():
    # C ranks below B, so it never blocks B's hints even where they collide.
    teams, supply, reserves, initiatives, demand = fixture()
    hints = fit_hints(
        teams, supply, reserves, initiatives, demand, CURRENT, "B", HORIZON
    )
    assert "2027-07" in hints


# --- T4 ----------------------------------------------------------------------


def test_t4_displacement_cascade():
    teams, supply, reserves, initiatives, demand = fixture()
    before = allocate(teams, supply, reserves, initiatives, demand, CURRENT, HORIZON)

    pushed = copy.deepcopy(initiatives)
    for initiative in pushed:
        initiative["rank"] += 1
    pushed.append(
        {"id": "D", "name": "D", "rank": 1, "start_month": "2027-02", "archived": False}
    )
    demand = demand + [
        {"initiative_id": "D", "team_id": "SOC", "offset": 0, "fte_h": 50},
        {"initiative_id": "D", "team_id": "SOC", "offset": 1, "fte_h": 50},
    ]
    after = allocate(teams, supply, reserves, pushed, demand, CURRENT, HORIZON)

    assert status(after, "D") == "green"
    assert status(after, "A") == "red"
    assert shortfalls(after, "A") == [
        ("SOC", "2027-02", 50),
        ("SOC", "2027-03", 50),
    ]
    assert status(after, "B") == "green"
    assert status(after, "C") == "green"

    transitions = {
        i: (before["initiatives"][i]["status"], after["initiatives"][i]["status"])
        for i in before["initiatives"]
        if before["initiatives"][i]["status"] != after["initiatives"][i]["status"]
    }
    assert transitions == {"A": ("green", "red"), "B": ("red", "green")}


# --- T5 ----------------------------------------------------------------------


def test_t5_reserves_exceed_supply():
    teams, supply, reserves, initiatives, demand = fixture()
    supply = dict(supply)
    supply[("GRC", "2027-05")] = 50
    result = allocate(teams, supply, reserves, initiatives, demand, CURRENT, HORIZON)

    cell = result["cells"][("GRC", "2027-05")]
    assert "reserve_exceeds_supply" in cell["warnings"]
    assert cell["free"] == 0

    assert status(result, "A") == "red"
    assert shortfalls(result, "A") == [("GRC", "2027-05", 50)]
    assert status(result, "B") == "green"
    assert status(result, "C") == "green"


# --- T6 ----------------------------------------------------------------------


def test_t6_past_months_are_not_evaluated():
    result = run(current="2027-04")
    assert status(result, "A") == "green"
    assert status(result, "B") == "red"
    assert shortfalls(result, "B") == [("GRC", "2027-04", 25)]
    assert status(result, "C") == "green"


def test_t6_no_cells_before_the_current_month():
    result = run(current="2027-04")
    assert ("GRC", "2027-03") not in result["cells"]
    assert result["months"][0] == "2027-04"


def test_t6_start_in_the_past_is_rejected():
    from app import validate_start_month

    with pytest.raises(ValueError):
        validate_start_month("2027-03", current_month="2027-04")
    assert validate_start_month("2027-04", current_month="2027-04") == "2027-04"


def test_t7_wholly_past_initiative_cannot_turn_red():
    # R6/R7: an initiative shortened to lie entirely in the past is inert.
    teams, supply, reserves, initiatives, demand = fixture()
    initiatives = copy.deepcopy(initiatives)
    initiatives[0]["start_month"] = "2026-01"
    result = allocate(teams, supply, reserves, initiatives, demand, CURRENT, HORIZON)
    assert status(result, "A") == "past"
    assert result["initiatives"]["A"]["shortfalls"] == []


# --- T7 ----------------------------------------------------------------------


def test_t7_precision():
    """0.10 + 0.20 against 0.30 available. Fails under float arithmetic."""
    teams = ["APP"]
    supply = {("APP", "2027-01"): 30}
    reserves = []
    initiatives = [
        {"id": "P", "name": "P", "rank": 1, "start_month": "2027-01", "archived": False},
        {"id": "Q", "name": "Q", "rank": 2, "start_month": "2027-01", "archived": False},
    ]
    demand = [
        {"initiative_id": "P", "team_id": "APP", "offset": 0, "fte_h": 10},
        {"initiative_id": "Q", "team_id": "APP", "offset": 0, "fte_h": 20},
    ]
    result = allocate(teams, supply, reserves, initiatives, demand, CURRENT, HORIZON)
    assert status(result, "P") == "green"
    assert status(result, "Q") == "green"
    assert result["cells"][("APP", "2027-01")]["free"] == 0


# --- R9: missing supply is distinct from over-allocation ---------------------


def test_missing_supply_is_labelled_separately():
    teams, supply, reserves, initiatives, demand = fixture()
    initiatives = copy.deepcopy(initiatives)
    initiatives[2]["start_month"] = "2028-12"  # C runs into 2029, past supply
    result = allocate(teams, supply, reserves, initiatives, demand, CURRENT, HORIZON)

    assert status(result, "C") == "red"
    reasons = {s["month"]: s["reason"] for s in result["initiatives"]["C"]["shortfalls"]}
    assert reasons == {
        "2029-01": "no_supply_data",
        "2029-02": "no_supply_data",
    }


def test_archived_initiatives_are_excluded():
    teams, supply, reserves, initiatives, demand = fixture()
    initiatives = copy.deepcopy(initiatives)
    initiatives[0]["archived"] = True
    result = allocate(teams, supply, reserves, initiatives, demand, CURRENT, HORIZON)

    assert status(result, "A") == "archived"
    assert status(result, "B") == "green"  # A's GRC draw is gone
