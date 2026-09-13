"""CSV import tests.

The fixture database has teams SOC and GRC, initiatives A, B and C, and a
clock pinned to 2027-01.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from fastapi.testclient import TestClient

import db
import importer

HEADER = "InitiativeName,Reference,StartMonth,EndMonth,SOC,GRC"


@pytest.fixture()
def client(tmp_path):
    db.DB_PATH = str(tmp_path / "test.db")
    import app as app_module

    conn = db.connect()
    db.init_db(conn)
    db.seed_fixture(conn, anchor="2027-01")
    conn.close()

    with TestClient(app_module.app) as test_client:
        yield test_client


def post(client, csv):
    return client.post("/api/import", json={"csv": csv})


def by_name(state):
    return {i["name"]: i for i in state["initiatives"]}


def demand_of(initiative, team_id):
    return sorted((d["offset"], d["fte_h"]) for d in initiative["demand"] if d["team_id"] == team_id)


# --- the happy path ----------------------------------------------------------


def test_the_example_from_the_spec_imports(client):
    response = post(client, HEADER + "\n"
        "Project123,PRO-001,2026-09,2026-12,1,0.5\n"
        "ProjectABC,PRO-002,2026-10,2027-03,2,0.25\n")
    assert response.status_code == 200, response.json()

    state = response.json()
    assert state["import"]["created"] == ["Project123", "ProjectABC"]
    assert state["import"]["updated"] == []

    teams = {t["name"]: t["id"] for t in state["teams"]}
    one = by_name(state)["Project123"]
    assert one["start_month"] == "2026-09"
    assert one["reference"] == "PRO-001"
    # 2026-09 to 2026-12 inclusive is four months, every month at the same FTE.
    assert demand_of(one, teams["SOC"]) == [(0, 100), (1, 100), (2, 100), (3, 100)]
    assert demand_of(one, teams["GRC"]) == [(0, 50), (1, 50), (2, 50), (3, 50)]

    two = by_name(state)["ProjectABC"]
    assert two["length"] == 6  # 2026-10 to 2027-03
    assert demand_of(two, teams["GRC"])[0] == (0, 25)


def test_imported_rows_are_appended_below_existing_work(client):
    state = post(client, HEADER + "\nNew,PRO-9,2027-02,2027-03,1,\n").json()
    ranked = sorted(state["initiatives"], key=lambda i: i["rank"])
    assert [i["name"] for i in ranked] == ["A", "B", "C", "New"]


def test_a_blank_team_cell_means_no_demand(client):
    state = post(client, HEADER + "\nOnlySoc,PRO-9,2027-02,2027-03,1,\n").json()
    teams = {t["name"]: t["id"] for t in state["teams"]}
    only = by_name(state)["OnlySoc"]
    assert demand_of(only, teams["GRC"]) == []
    assert len(demand_of(only, teams["SOC"])) == 2


def test_column_order_and_case_do_not_matter(client):
    response = post(client, "reference, endmonth , InitiativeName,startmonth,  grc ,soc\n"
                            "PRO-9,2027-03,Reordered,2027-02,0.25,1\n")
    assert response.status_code == 200, response.json()
    teams = {t["name"]: t["id"] for t in response.json()["teams"]}
    row = by_name(response.json())["Reordered"]
    assert demand_of(row, teams["GRC"])[0] == (0, 25)
    assert demand_of(row, teams["SOC"])[0] == (0, 100)


def test_a_byte_order_mark_does_not_break_the_first_header(client):
    response = post(client, "﻿" + HEADER + "\nBom,PRO-9,2027-02,2027-03,1,\n")
    assert response.status_code == 200, response.json()


# --- past starts, which is the whole point of accepting them -----------------


def test_a_start_in_the_past_is_accepted(client):
    """The editor refuses this (R8); import must not, or a real export of
    work already under way would be unusable."""
    response = post(client, HEADER + "\nAlreadyGoing,PRO-9,2026-06,2027-06,1,\n")
    assert response.status_code == 200, response.json()

    row = by_name(response.json())["AlreadyGoing"]
    assert row["start_month"] == "2026-06"
    # R7: only 2027-01 onwards is evaluated, so it is judged on what remains.
    assert row["status"] in ("green", "red")


# --- re-import ---------------------------------------------------------------


def test_reimport_updates_in_place(client):
    first = post(client, HEADER + "\nProject123,PRO-001,2026-09,2026-12,1,0.5\n").json()
    assert first["import"]["created"] == ["Project123"]
    created_id = by_name(first)["Project123"]["id"]

    second = post(client, HEADER + "\nProject123 renamed,PRO-001,2027-02,2027-04,0.5,\n").json()
    assert second["import"] == {
        "created": [],
        "updated": ["Project123 renamed"],
        "changed": second["import"]["changed"],
    }

    names = by_name(second)
    assert "Project123" not in names
    row = names["Project123 renamed"]
    assert row["id"] == created_id  # same initiative, not a duplicate
    assert row["start_month"] == "2027-02"
    assert row["length"] == 3

    teams = {t["name"]: t["id"] for t in second["teams"]}
    assert demand_of(row, teams["GRC"]) == []  # the old GRC demand is gone
    assert len(demand_of(row, teams["SOC"])) == 3


def test_reimport_matches_reference_case_insensitively(client):
    post(client, HEADER + "\nOne,PRO-001,2027-02,2027-03,1,\n")
    second = post(client, HEADER + "\nOne again,pro-001,2027-02,2027-03,1,\n").json()
    assert second["import"]["updated"] == ["One again"]
    assert len([i for i in second["initiatives"] if i["reference"]]) == 1


def test_reimport_leaves_rank_and_archived_alone(client):
    state = post(client, HEADER + "\nKeep,PRO-9,2027-02,2027-03,1,\n").json()
    target = by_name(state)["Keep"]
    order = [target["id"]] + [i["id"] for i in state["initiatives"] if i["id"] != target["id"]]
    client.post("/api/initiatives/reorder", json={"ordered_ids": order})
    client.patch(f"/api/initiatives/{target['id']}", json={"archived": True})

    after = post(client, HEADER + "\nKeep,PRO-9,2027-02,2027-05,1,\n").json()
    row = by_name(after)["Keep"]
    assert row["rank"] == 1
    assert row["archived"] is True
    assert row["length"] == 4  # the demand did change


# --- failure, which must write nothing ---------------------------------------


def test_an_unknown_team_fails_the_whole_import(client):
    before = client.get("/api/state").json()
    response = post(client, "InitiativeName,Reference,StartMonth,EndMonth,SOC,PLATFORM\n"
                            "Good,PRO-1,2027-02,2027-03,1,1\n"
                            "AlsoGood,PRO-2,2027-02,2027-03,1,1\n")
    assert response.status_code == 400

    detail = response.json()["detail"]
    assert len(detail["errors"]) == 1
    assert "PLATFORM" in detail["errors"][0]
    assert "GRC, SOC" in detail["errors"][0]  # says what the valid teams are

    assert client.get("/api/state").json()["initiatives"] == before["initiatives"]


def test_every_problem_is_reported_at_once(client):
    response = post(client, HEADER + "\n"
        ",PRO-1,2027-13,2027-03,1,\n"
        "Backwards,PRO-2,2027-06,2027-02,1,\n"
        "Dup,PRO-2,2027-02,2027-03,x,\n")
    assert response.status_code == 400

    errors = response.json()["detail"]["errors"]
    joined = " | ".join(errors)
    assert "InitiativeName is empty" in joined
    assert "2027-13" in joined
    assert "before StartMonth" in joined
    assert "already appears" in joined
    assert "not a number" in joined
    assert len(errors) >= 5


def test_one_bad_row_writes_nothing_at_all(client):
    before = client.get("/api/state").json()
    response = post(client, HEADER + "\n"
        "Fine,PRO-1,2027-02,2027-03,1,\n"
        "Broken,PRO-2,2027-02,nonsense,1,\n")
    assert response.status_code == 400
    after = client.get("/api/state").json()
    assert [i["name"] for i in after["initiatives"]] == [i["name"] for i in before["initiatives"]]


@pytest.mark.parametrize(
    "csv,expected",
    [
        ("InitiativeName,StartMonth,EndMonth,SOC\nA,2027-02,2027-03,1\n", "Reference"),
        ("", "empty"),
        (HEADER + "\n", "no rows"),
        ("InitiativeName,Reference,StartMonth,EndMonth\nA,PRO-1,2027-02,2027-03\n", "No team columns"),
        (HEADER + "\nA,PRO-1,2027-02,2027-03,0.333,\n", "two decimal places"),
        (HEADER + "\nA,PRO-1,2027-02,2027-03,-1,\n", "negative"),
        (HEADER + "\nA,PRO-1,2027-02,2027-03,9999,\n", "above the maximum"),
        (HEADER + "\nA,PRO-1,2027-02,2027-03,,\n", "no team has any FTE"),
        (HEADER + "\nA,,2027-02,2027-03,1,\n", "Reference is empty"),
    ],
)
def test_rejected_files(client, csv, expected):
    response = post(client, csv)
    assert response.status_code in (400, 422), csv
    if response.status_code == 400:
        assert expected.lower() in " ".join(response.json()["detail"]["errors"]).lower()


def test_a_span_longer_than_the_offset_limit_is_rejected(client):
    response = post(client, HEADER + "\nLong,PRO-1,2027-01,2047-01,1,\n")
    assert response.status_code == 400
    assert "more than the limit" in " ".join(response.json()["detail"]["errors"])


# --- what the summary reports ------------------------------------------------


def test_a_new_row_cannot_displace_existing_work(client):
    """Imports are appended below everything already in the plan, so a new row
    loses the argument rather than winning it. It goes red itself."""
    state = post(client, HEADER + "\nHungry,PRO-1,2027-04,2027-06,1,\n").json()

    assert by_name(state)["Hungry"]["status"] == "red"
    assert state["import"]["changed"] == []
    assert by_name(state)["C"]["status"] == "green"


def test_the_summary_names_what_changed_colour(client):
    """A re-import can displace, because it edits an initiative that is
    already ranked. Promote the imported row, then relax its demand."""
    state = post(client, HEADER + "\nHungry,PRO-1,2027-04,2027-06,1,\n").json()
    hungry = by_name(state)["Hungry"]["id"]
    order = [hungry] + [i["id"] for i in state["initiatives"] if i["id"] != hungry]
    state = client.post("/api/initiatives/reorder", json={"ordered_ids": order}).json()

    # At rank 1 it takes all the spare SOC capacity, so C cannot be staffed.
    assert by_name(state)["Hungry"]["status"] == "green"
    assert by_name(state)["C"]["status"] == "red"

    relaxed = post(client, HEADER + "\nHungry,PRO-1,2027-04,2027-06,0.5,\n").json()
    changed = {c["name"]: (c["from"], c["to"]) for c in relaxed["import"]["changed"]}
    assert changed == {"C": ("red", "green")}
    assert relaxed["import"]["updated"] == ["Hungry"]


def test_importing_over_capacity_is_not_an_error(client):
    response = post(client, HEADER + "\nTooBig,PRO-1,2027-02,2027-03,50,50\n")
    assert response.status_code == 200
    assert by_name(response.json())["TooBig"]["status"] == "red"


# --- the parser on its own ---------------------------------------------------


def test_parser_needs_no_database():
    teams = [{"id": 1, "name": "SOC"}, {"id": 2, "name": "GRC"}]
    rows, errors = importer.parse(HEADER + "\nX,PRO-1,2027-01,2027-02,1,0.5\n", teams)
    assert errors == []
    assert rows == [
        {
            "line": 2,
            "name": "X",
            "reference": "PRO-1",
            "start_month": "2027-01",
            "months": 2,
            "demand": {1: 100, 2: 50},
        }
    ]


def test_parser_reports_unknown_teams_when_none_exist():
    rows, errors = importer.parse(HEADER + "\nX,PRO-1,2027-01,2027-02,1,0.5\n", [])
    assert rows == []
    assert "add a team first" in errors[0]
