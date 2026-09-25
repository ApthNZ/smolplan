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


# --- full dates where a month was asked for ----------------------------------


@pytest.mark.parametrize(
    "written,month",
    [
        ("2027-02", "2027-02"),          # already a month, untouched
        ("1/02/2027 0:00", "2027-02"),   # the Excel export in the request
        ("1/02/2027", "2027-02"),
        ("28/02/2027", "2027-02"),       # the day is irrelevant
        ("01/02/2027 09:30:00", "2027-02"),
        ("1-02-2027", "2027-02"),
        ("2027-02-01", "2027-02"),
        ("2027-02-28T00:00:00", "2027-02"),
        ("2027-2-9", "2027-02"),
    ],
)
def test_a_full_date_is_read_as_its_month(written, month):
    assert importer.month_of(written) == month


@pytest.mark.parametrize(
    "written",
    ["", "nonsense", "2027", "2027-13", "0/02/2027", "32/02/2027", "13/13/2027", "1/2/27"],
)
def test_what_is_not_a_date_is_left_alone(written):
    """Returned unchanged so the error message quotes what the file said."""
    assert importer.month_of(written) == written


def test_slashed_dates_are_read_day_first():
    assert importer.month_of("7/01/2027") == "2027-01"  # 7 January, not July
    assert importer.month_of("01/7/2027") == "2027-07"


def test_a_second_number_above_twelve_is_read_month_first():
    """Only one reading of 2/31/2027 is a date at all."""
    assert importer.month_of("2/31/2027") == "2027-02"


def test_a_file_of_full_dates_imports(client):
    response = post(client, HEADER + "\n"
        "Exported,PRO-1,1/02/2027 0:00,30/04/2027 0:00,1,\n")
    assert response.status_code == 200, response.json()

    state = response.json()
    teams = {t["name"]: t["id"] for t in state["teams"]}
    exported = by_name(state)["Exported"]
    assert demand_of(exported, teams["SOC"]) == [(0, 100), (1, 100), (2, 100)]


def test_the_two_columns_need_not_agree_on_format(client):
    rows, errors = importer.parse(
        HEADER + "\nX,PRO-1,1/02/2027 0:00,2027-03,1,\n",
        [{"id": 1, "name": "SOC"}, {"id": 2, "name": "GRC"}],
    )
    assert errors == []
    assert rows[0]["start_month"] == "2027-02"
    assert rows[0]["months"] == 2


def test_a_date_that_is_not_one_still_names_the_column(client):
    response = post(client, HEADER + "\nX,PRO-1,32/02/2027,2027-03,1,\n")
    assert response.status_code == 400
    errors = " ".join(response.json()["detail"]["errors"])
    assert "StartMonth" in errors and "32/02/2027" in errors


def test_full_dates_still_compare_as_months(client):
    """An end before the start is caught after normalisation, not before."""
    response = post(client, HEADER + "\nX,PRO-1,1/04/2027,28/02/2027,1,\n")
    assert response.status_code == 400
    assert "before StartMonth" in " ".join(response.json()["detail"]["errors"])


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


# --- the Deadline column -----------------------------------------------------

WITH_DEADLINE = HEADER + ",Deadline"
TEAMS = [{"id": 1, "name": "SOC"}, {"id": 2, "name": "GRC"}]


def test_a_deadline_column_sets_the_deadline(client):
    response = post(client, WITH_DEADLINE + "\nDue,PRO-1,2027-02,2027-04,1,,2027-06\n")
    assert response.status_code == 200, response.json()
    due = by_name(response.json())["Due"]
    assert (due["end_month"], due["deadline_month"]) == ("2027-04", "2027-06")


def test_the_import_sets_the_end_from_the_span(client):
    due = by_name(post(client, HEADER + "\nSpan,PRO-1,2027-02,2027-09,1,\n").json())["Span"]
    assert (due["end_month"], due["length"]) == ("2027-09", 8)


def test_a_deadline_may_be_a_full_date(client):
    response = post(client, WITH_DEADLINE + "\nDue,PRO-1,2027-02,2027-04,1,,30/06/2027 0:00\n")
    assert response.status_code == 200, response.json()
    assert by_name(response.json())["Due"]["deadline_month"] == "2027-06"


def test_a_blank_deadline_on_a_new_row_is_no_deadline(client):
    response = post(client, WITH_DEADLINE + "\nOpen,PRO-1,2027-02,2027-04,1,,\n")
    assert response.status_code == 200, response.json()
    assert by_name(response.json())["Open"]["deadline_month"] is None


def test_without_the_column_an_update_keeps_its_deadline(client):
    post(client, WITH_DEADLINE + "\nDue,PRO-1,2027-02,2027-04,1,,2027-06\n")
    again = post(client, HEADER + "\nDue,PRO-1,2027-03,2027-05,1,\n")
    assert again.status_code == 200, again.json()
    due = by_name(again.json())["Due"]
    assert (due["end_month"], due["deadline_month"]) == ("2027-05", "2027-06")


def test_with_the_column_a_blank_cell_clears_the_deadline(client):
    """The file is authoritative for every column it has, as a blank team cell
    clears that team's demand."""
    post(client, WITH_DEADLINE + "\nDue,PRO-1,2027-02,2027-04,1,,2027-06\n")
    again = post(client, WITH_DEADLINE + "\nDue,PRO-1,2027-02,2027-04,1,,\n").json()
    assert by_name(again)["Due"]["deadline_month"] is None


def test_the_column_moves_a_deadline_set_in_the_editor(client):
    state = post(client, HEADER + "\nDue,PRO-1,2027-02,2027-04,1,\n").json()
    due = by_name(state)["Due"]
    client.patch(f"/api/initiatives/{due['id']}", json={"deadline_month": "2027-05"})

    again = post(client, WITH_DEADLINE + "\nDue,PRO-1,2027-02,2027-08,1,,2027-09\n")
    assert again.status_code == 200, again.json()
    due = by_name(again.json())["Due"]
    assert (due["end_month"], due["deadline_month"]) == ("2027-08", "2027-09")


def test_an_end_after_the_rows_own_deadline_is_refused(client):
    before = client.get("/api/state").json()
    response = post(client, WITH_DEADLINE + "\nLate,PRO-1,2027-02,2027-07,1,,2027-06\n")
    assert response.status_code == 400
    assert response.json()["detail"]["errors"] == [
        "Line 2: EndMonth 2027-07 is after the Deadline 2027-06."
    ]
    assert client.get("/api/state").json()["initiatives"] == before["initiatives"]


@pytest.mark.parametrize("written", ["soon", "2027-13", "32/06/2027"])
def test_a_malformed_deadline_is_refused_in_the_files_own_words(client, written):
    response = post(client, WITH_DEADLINE + f"\nDue,PRO-1,2027-02,2027-04,1,,{written}\n")
    assert response.status_code == 400
    assert response.json()["detail"]["errors"] == [
        f"Line 2: Deadline {written!r} is not a month in YYYY-MM form."
    ]


def test_an_update_that_would_pass_an_existing_deadline_writes_nothing(client):
    """Without a Deadline column an update keeps the deadline already set, so
    an EndMonth past it is a breach of R11. It is caught against the plan
    before anything is written, every such row is named, and the good rows in
    the same file are not imported either."""
    post(client, WITH_DEADLINE + "\n"
        "One,PRO-1,2027-02,2027-04,1,,2027-05\n"
        "Two,PRO-2,2027-02,2027-04,1,,2027-06\n")
    before = client.get("/api/state").json()

    response = post(client, HEADER + "\n"
        "One,PRO-1,2027-02,2027-06,1,\n"
        "Two,PRO-2,2027-03,2027-07,1,\n"
        "Fine,PRO-3,2027-02,2027-04,1,\n")
    assert response.status_code == 400
    assert response.json()["detail"]["errors"] == [
        "Line 2: EndMonth 2027-06 is after the deadline already set on PRO-1, May 2027. "
        "End it by then, or add a Deadline column to move the deadline.",
        "Line 3: EndMonth 2027-07 is after the deadline already set on PRO-2, Jun 2027. "
        "End it by then, or add a Deadline column to move the deadline.",
    ]
    after = client.get("/api/state").json()
    assert after["initiatives"] == before["initiatives"]
    assert after["undo"]["depth"] == before["undo"]["depth"]


def test_a_deadline_conflict_is_reported_alongside_a_parse_error(client):
    """Every problem at once means both kinds in one reply. The deadline check
    used to run only on a file that had parsed cleanly, so the conflict on
    line 2 surfaced only after line 3 had been fixed — a second attempt, which
    is what reporting everything at once is there to save."""
    post(client, WITH_DEADLINE + "\nOne,PRO-1,2027-02,2027-04,1,,2027-06\n")
    before = client.get("/api/state").json()

    response = post(client, HEADER + "\n"
        "One,PRO-1,2027-02,2027-09,1,\n"
        "Two,,2027-02,2027-03,1,\n"
        "Three,PRO-3,2027-02,2027-03,abc,1\n")
    assert response.status_code == 400
    assert response.json()["detail"]["errors"] == [
        "Line 2: EndMonth 2027-09 is after the deadline already set on PRO-1, Jun 2027. "
        "End it by then, or add a Deadline column to move the deadline.",
        "Line 3: Reference is empty. It is what a re-import matches on.",
        "Line 4, column SOC: 'abc' is not a number.",
    ]
    assert response.json()["detail"]["message"] == "Nothing was imported. 3 problems found:"
    after = client.get("/api/state").json()
    assert after["initiatives"] == before["initiatives"]
    assert after["undo"]["depth"] == before["undo"]["depth"]


def test_the_parser_hands_back_the_clean_rows_with_the_errors():
    """So the caller can check them against the plan in the same pass. The
    caller still writes nothing: the errors are what say so."""
    rows, errors = importer.parse(
        HEADER + "\nGood,PRO-1,2027-01,2027-02,1,\nBad,PRO-2,2027-01,2027-02,x,1\n", TEAMS
    )
    assert [r["reference"] for r in rows] == ["PRO-1"]
    assert errors == ["Line 3, column SOC: 'x' is not a number."]


def test_the_existing_deadline_is_matched_on_reference_case_insensitively(client):
    post(client, WITH_DEADLINE + "\nOne,PRO-1,2027-02,2027-04,1,,2027-05\n")
    response = post(client, HEADER + "\nOne,pro-1,2027-02,2027-06,1,\n")
    assert response.status_code == 400
    assert "already set on pro-1" in response.json()["detail"]["errors"][0]


def test_a_column_called_deadline_is_never_a_team():
    """Which is why a team cannot be called Deadline and still be imported."""
    teams = TEAMS + [{"id": 3, "name": "Deadline"}]
    rows, errors = importer.parse(
        "InitiativeName,Reference,StartMonth,EndMonth,SOC,deadline\n"
        "X,PRO-1,2027-01,2027-02,1,2027-03\n",
        teams,
    )
    assert errors == []
    assert rows[0]["deadline_month"] == "2027-03"
    assert rows[0]["demand"] == {1: 100}


def test_the_parser_tells_a_missing_column_from_a_blank_cell():
    """No column leaves an existing deadline alone; a blank cell clears it.
    The row says which by whether it has the key at all."""
    without, _ = importer.parse(HEADER + "\nX,PRO-1,2027-01,2027-02,1,\n", TEAMS)
    blank, _ = importer.parse(WITH_DEADLINE + "\nX,PRO-1,2027-01,2027-02,1,,\n", TEAMS)
    assert "deadline_month" not in without[0]
    assert blank[0]["deadline_month"] is None


def test_an_imported_deadline_reaches_the_export(client):
    post(client, WITH_DEADLINE + "\nTracked,PRO-77,2027-03,2027-05,0.5,,2027-08\n")
    lines = client.get("/api/export.csv").text.splitlines()
    assert lines[0] == "InitiativeName,Reference,StartMonth,EndMonth,Deadline"
    assert "Tracked,PRO-77,2027-03,2027-05,2027-08" in lines


# --- the demand-summary converter --------------------------------------------


def convert(client, text):
    return client.post("/api/convert", json={"text": text})


def test_the_example_from_the_request(client):
    body = convert(client, "GRC: 1\nSOC: 2\nENG: 0.5").json()
    assert body["csv"] == "GRC,SOC,ENG\n1,2,0.5"
    assert body["names"] == ["GRC", "SOC", "ENG"]
    assert body["values"] == ["1", "2", "0.5"]


@pytest.mark.parametrize(
    "text",
    [
        "GRC:1\nSOC:2",
        "GRC: 1\nSOC: 2",
        "GRC:   1   \nSOC:2",
        "   GRC :1\n  SOC : 2  ",
        "\n\nGRC: 1\n\n\nSOC: 2\n\n",
        "\tGRC:\t1\nSOC:2\t",
    ],
)
def test_spacing_does_not_matter(client, text):
    assert convert(client, text).json()["csv"] == "GRC,SOC\n1,2"


def test_known_team_names_are_corrected_to_their_real_case(client):
    """The import matches case-insensitively anyway, but the CSV reads better
    with the team's actual name in it."""
    body = convert(client, "grc: 1\n  soc : 2").json()
    assert body["names"] == ["GRC", "SOC"]


def test_values_are_echoed_not_reformatted(client):
    body = convert(client, "GRC: 0.5\nSOC: 1").json()
    assert body["values"] == ["0.5", "1"]  # not 0.50 and 1.00


def test_a_team_that_does_not_exist_is_a_warning_not_an_error(client):
    body = convert(client, "GRC: 1\nENG: 0.5").json()
    assert body["unknown"] == ["ENG"]
    assert body["csv"] == "GRC,ENG\n1,0.5"


@pytest.mark.parametrize(
    "text,expected",
    [
        ("GRC 1", "no colon"),
        ("GRC:", "no FTE after the colon"),
        (": 1", "no team name"),
        ("GRC: 1\nGRC: 2", "already appears on line 1"),
        ("GRC: one", "not a number"),
        ("GRC: 0.333", "two decimal places"),
        ("GRC: -1", "negative"),
        ("GRC: 9999", "above the maximum"),
        ("", "Nothing to convert"),
        ("   \n\n  ", "Nothing to convert"),
    ],
)
def test_rejected_summaries(client, text, expected):
    response = convert(client, text)
    assert response.status_code == 400, text
    assert expected.lower() in " ".join(response.json()["detail"]["errors"]).lower()


def test_every_problem_is_reported_at_once_here_too(client):
    response = convert(client, "GRC 1\nSOC: two\nGRC: 1\nGRC: 2")
    errors = response.json()["detail"]["errors"]
    assert len(errors) >= 3


def test_converted_output_is_something_the_importer_accepts(client):
    """The point of sharing parse_fte: whatever the converter emits must get
    through the import's validation."""
    converted = convert(client, "SOC: 1\nGRC: 0.25").json()["csv"]
    header, values = converted.split("\n")

    csv = f"InitiativeName,Reference,StartMonth,EndMonth,{header}\n"
    csv += f"Built by the converter,PRO-9,2027-02,2027-04,{values}\n"
    response = client.post("/api/import", json={"csv": csv})
    assert response.status_code == 200, response.json()

    state = response.json()
    teams = {t["name"]: t["id"] for t in state["teams"]}
    built = by_name(state)["Built by the converter"]
    assert demand_of(built, teams["SOC"])[0] == (0, 100)
    assert demand_of(built, teams["GRC"])[0] == (0, 25)


def test_convert_writes_nothing(client):
    before = client.get("/api/state").json()
    convert(client, "GRC: 1\nSOC: 2")
    assert client.get("/api/state").json() == before


@pytest.mark.parametrize(
    "text",
    [
        '"GRC": 1\n"SOC": 2',
        'GRC: "1"\nSOC: "2"',
        '"GRC: 1"\n"SOC: 2"',
        '  "GRC" :  "1"  \n SOC: 2 ',
    ],
)
def test_quotes_from_a_paste_are_stripped_not_rejected(client, text):
    body = convert(client, text).json()
    assert body["csv"] == "GRC,SOC\n1,2"
    assert body["unknown"] == []


def test_a_quoted_team_name_still_matches_a_known_team(client):
    body = convert(client, '"grc": 1').json()
    assert body["names"] == ["GRC"]


def test_a_line_of_nothing_but_quotes_is_spacing(client):
    assert convert(client, 'GRC: 1\n"""\nSOC: 2').json()["csv"] == "GRC,SOC\n1,2"


def test_stripping_quotes_does_not_excuse_a_bad_number(client):
    response = convert(client, 'GRC: "one"')
    assert response.status_code == 400
    assert "not a number" in " ".join(response.json()["detail"]["errors"])


# --- Jira exports ------------------------------------------------------------

JIRA_HEADER = (
    "Summary,Issue key,Custom field (Target start),Custom field (Target end),"
    "Custom field (Team Capacity)"
)


def capacity(*lines):
    """A Team Capacity cell as Jira exports it: multi-line, so quoted."""
    return '"' + "\n".join(lines) + '"'


def jira_row(summary, key_, start, end, *lines):
    return f"{summary},{key_},{start},{end},{capacity(*lines)}"


def test_a_jira_export_imports(client):
    """The shape of a real export: prose around the entries, a heading with an
    empty value, the prose line written differently on each issue, zeroes for
    teams this plan does not have, and Excel's full dates."""
    response = post(client, "\n".join([
        JIRA_HEADER,
        jira_row("Firewall refresh", "SEC-101", "1/01/2027 0:00", "30/04/2027 0:00",
                 "Teams required to resource: Networks / Platform / DevOps",
                 "Estimated Team FTE:",
                 "SOC: 0.5", "AppSec: 0", "GRC: 0", "TVM: 0"),
        jira_row("Logging uplift", "SEC-102", "19/10/2026 0:00", "31/12/2026 0:00",
                 "Teams required to resource: Architecture (Network) | Network (Core) | ",
                 "Estimated Team FTE:",
                 "SOC: 0", "AppSec: 0", "GRC: 0.75"),
    ]) + "\n")
    assert response.status_code == 200, response.json()
    state = response.json()
    assert state["import"]["created"] == ["Firewall refresh", "Logging uplift"]

    teams = {t["name"]: t["id"] for t in state["teams"]}
    firewall = by_name(state)["Firewall refresh"]
    assert (firewall["reference"], firewall["start_month"], firewall["end_month"]) == (
        "SEC-101", "2027-01", "2027-04")
    assert demand_of(firewall, teams["SOC"]) == [(m, 50) for m in range(4)]
    assert demand_of(firewall, teams["GRC"]) == []

    logging = by_name(state)["Logging uplift"]
    assert (logging["start_month"], logging["end_month"]) == ("2026-10", "2026-12")
    assert demand_of(logging, teams["GRC"]) == [(m, 75) for m in range(3)]
    assert demand_of(logging, teams["SOC"]) == []


def test_a_jira_reimport_updates_in_place(client):
    post(client, JIRA_HEADER + "\n" + jira_row("First", "SEC-1", "1/02/2027", "30/04/2027", "SOC: 1"))
    again = post(client, JIRA_HEADER + "\n" + jira_row("Renamed", "SEC-1", "1/03/2027", "31/05/2027", "GRC: 2"))
    assert again.status_code == 200, again.json()
    assert again.json()["import"]["updated"] == ["Renamed"]
    assert "First" not in by_name(again.json())


def test_every_other_jira_column_is_ignored(client):
    """Jira exports dozens of fields, some of them repeated (one Sprint column
    per sprint). None of them is a team, and a repeat is not a duplicate that
    matters."""
    response = post(client,
        "Issue Type,Summary,Issue key,Issue id,Status,Sprint,Sprint,"
        "Custom field (Target start),Custom field (Target end),Custom field (Team Capacity)\n"
        f"Epic,Thing,SEC-5,10001,To Do,S1,S2,1/02/2027,30/04/2027,{capacity('SOC: 1')}\n")
    assert response.status_code == 200, response.json()
    assert by_name(response.json())["Thing"]["reference"] == "SEC-5"


def test_a_team_the_plan_lacks_is_an_error_only_with_fte(client):
    """SecArch at 0 needs nothing, so nothing is lost by skipping it. At 0.75 it
    is demand that would vanish, so the file is refused — once, naming every
    line, rather than once per row."""
    response = post(client, "\n".join([
        JIRA_HEADER,
        jira_row("A", "SEC-1", "2027-02", "2027-03", "SOC: 1", "SecArch: 0.75", "ISM: 0"),
        jira_row("B", "SEC-2", "2027-02", "2027-03", "SOC: 1", "SecArch: 0.5", "ISM: 0.25"),
        jira_row("C", "SEC-3", "2027-02", "2027-03", "SOC: 1", "SecArch: 0", "ISM: 0"),
    ]) + "\n")
    assert response.status_code == 400
    errors = response.json()["detail"]["errors"]
    assert len(errors) == 1, errors
    assert "ISM (line 3)" in errors[0]
    assert "SecArch (lines 2, 3)" in errors[0]
    assert "Known teams are: GRC, SOC" in errors[0]
    references = {i["reference"] for i in client.get("/api/state").json()["initiatives"]}
    assert "SEC-3" not in references


def test_a_row_whose_only_fte_is_for_an_unknown_team_is_not_also_called_empty(client):
    response = post(client, JIRA_HEADER + "\n" + jira_row("A", "SEC-1", "2027-02", "2027-03", "SecArch: 1"))
    errors = response.json()["detail"]["errors"]
    assert len(errors) == 1 and "SecArch" in errors[0], errors


def test_a_jira_row_with_no_fte_is_refused(client):
    response = post(client, JIRA_HEADER + "\n" + jira_row(
        "A", "SEC-1", "2027-02", "2027-03", "Estimated Team FTE:", "SOC: 0", "GRC: 0"))
    assert response.json()["detail"]["errors"] == [
        "Line 2 (SEC-1): no team has any FTE, so this initiative would need nothing."
    ]


def test_jira_errors_name_the_issue_and_the_field(client):
    """A Jira row runs over several lines of the file, so its issue key is the
    thing to find it by, and the columns are named as the file names them."""
    response = post(client, JIRA_HEADER + "\n" + jira_row(
        "A", "SEC-9", "someday", "2027-03", "SOC: TBC", "GRC: 1"))
    assert response.json()["detail"]["errors"] == [
        "Line 2 (SEC-9): Custom field (Target start) 'someday' is not a month in YYYY-MM form.",
        "Line 2 (SEC-9), Custom field (Team Capacity): SOC: 'TBC' is not a number.",
    ]


def test_a_jira_export_without_team_capacity_says_so(client):
    """Otherwise every Jira column would be reported as an unknown team."""
    response = post(client,
        "Summary,Issue key,Status,Custom field (Target start),Custom field (Target end)\n"
        "A,SEC-1,To Do,1/02/2027,30/04/2027\n")
    errors = response.json()["detail"]["errors"]
    assert len(errors) == 1 and "Team Capacity" in errors[0], errors


def test_a_jira_export_missing_a_field_names_it_in_jira_terms(client):
    response = post(client,
        "Summary,Issue key,Custom field (Target start),Custom field (Team Capacity)\n"
        f"A,SEC-1,1/02/2027,{capacity('SOC: 1')}\n")
    errors = response.json()["detail"]["errors"]
    assert len(errors) == 1 and "Target end" in errors[0], errors


def test_a_jira_export_may_carry_a_deadline(client):
    response = post(client,
        JIRA_HEADER + ",Custom field (Deadline)\n"
        f"A,SEC-1,1/02/2027,30/04/2027,{capacity('SOC: 1')},30/06/2027\n")
    assert response.status_code == 200, response.json()
    assert by_name(response.json())["A"]["deadline_month"] == "2027-06"


def test_jira_target_fields_need_not_be_custom_fields(client):
    response = post(client,
        "Summary,Issue key,Target start,Target end,Team Capacity\n"
        f"A,SEC-1,1/02/2027,30/04/2027,{capacity('SOC: 1')}\n")
    assert response.status_code == 200, response.json()


TEAMS = [{"id": 1, "name": "SOC"}, {"id": 2, "name": "GRC"}, {"id": 3, "name": "Sec Eng"}]


@pytest.mark.parametrize(
    "text,demand",
    [
        ("SOC: 0.5\nGRC: 1", {1: 50, 2: 100}),
        ("SOC:0.5\r\nGRC : 1", {1: 50, 2: 100}),         # spacing and Windows line ends
        ("soc: 0.5", {1: 50}),                            # case
        ("SOC = 0.5", {1: 50}),
        ("SOC: 0.5 FTE", {1: 50}),                        # a unit
        ("- SOC: 0.5\n* GRC: 1\n• Sec Eng: 2", {1: 50, 2: 100, 3: 200}),  # bullets
        ("*SOC*: *0.5*", {1: 50}),                        # Jira bold
        ("SOC 0.5\nGRC - 1", {1: 50, 2: 100}),            # no colon, but a team
        ("SOC: 0.5 | GRC: 1", {1: 50, 2: 100}),           # one line
        ("SOC: 0.5; GRC: 1", {1: 50, 2: 100}),
        ("SecEng: 1", {3: 100}),                          # spacing inside a name
        ("sec-eng: 1", {3: 100}),
        ("SOC: -\nGRC: n/a\nSec Eng: none", {}),          # written-out nothing
        ("SOC:\nGRC: 1", {2: 100}),                       # a blank value, as a blank cell
        ('"SOC: 0.5"', {1: 50}),
        ("", {}),
        ("Teams: SOC, GRC\nNeeded by: Q3\nSOC: 1", {1: 100}),  # prose with colons
        ("Ring SOC on 0800 1234", {}),                    # prose ending in a number
    ],
)
def test_team_capacity_formats(text, demand):
    got, problems, unknown = importer.parse_capacity(text, TEAMS)
    assert (got, problems, unknown) == (demand, [], [])


@pytest.mark.parametrize(
    "text,problem",
    [
        ("SOC: TBC", "SOC: 'TBC' is not a number"),
        ("SOC: -0.5", "SOC: -0.5 is negative"),           # a dash is not stripped from a number
        ("SOC: 0,5", "SOC: '0,5' is not a number"),       # never silently 0
        ("SOC: 0.125", "SOC: 0.125 has more than two decimal places"),
        ("SOC: 1\nsoc: 1", "SOC is given more than once"),
    ],
)
def test_team_capacity_problems(text, problem):
    _, problems, _ = importer.parse_capacity(text, TEAMS)
    assert problems == [problem]


def test_team_capacity_reports_unknown_teams_given_fte():
    _, problems, unknown = importer.parse_capacity("AppSec: 0\nTVM: 0.5\nTVM: 1\nOther words: 2", TEAMS)
    assert problems == []
    assert unknown == ["TVM", "Other words"]


@pytest.mark.parametrize(
    "written,month",
    [
        ("01/Jan/27 12:00 AM", "2027-01"),  # Jira's own export format
        ("15/Sep/26 3:45 PM", "2026-09"),
        ("15/Sept/2026", "2026-09"),
        ("1 March 2027", "2027-03"),
        ("31-Dec-26", "2026-12"),
    ],
)
def test_a_date_with_a_named_month(written, month):
    assert importer.month_of(written) == month


@pytest.mark.parametrize("written", ["01/Foo/27", "32/Jan/27", "1/Ja/27"])
def test_a_named_month_that_is_not_one_is_left_alone(written):
    assert importer.month_of(written) == written
