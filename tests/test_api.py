"""API tests. These run against a throwaway database seeded with the fixture."""

import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from fastapi.testclient import TestClient

import db


@pytest.fixture()
def client(tmp_path):
    db.DB_PATH = str(tmp_path / "test.db")
    import app as app_module

    # Seed before the app starts, pinned to the spec's fixture month, so the
    # startup hook finds a populated database and the dates stay predictable.
    # An unpinned seed anchors on the real current month.
    conn = db.connect()
    db.init_db(conn)
    db.seed_fixture(conn, anchor="2027-01")
    conn.close()

    with TestClient(app_module.app) as test_client:
        yield test_client


def by_name(state):
    return {i["name"]: i for i in state["initiatives"]}


def test_seeded_state_matches_t1(client):
    state = client.get("/api/state").json()
    assert state["settings"]["current_month"] == "2027-01"
    assert len(state["months"]) == 24

    initiatives = by_name(state)
    assert initiatives["A"]["status"] == "green"
    assert initiatives["B"]["status"] == "red"
    assert initiatives["C"]["status"] == "green"
    assert [(s["month"], s["short"]) for s in initiatives["B"]["shortfalls"]] == [
        ("2027-02", 25),
        ("2027-03", 25),
        ("2027-04", 25),
    ]


def test_move_start_turns_b_green(client):
    state = client.get("/api/state").json()
    b = by_name(state)["B"]
    after = client.patch(f"/api/initiatives/{b['id']}", json={"start_month": "2027-07"})
    assert after.status_code == 200
    assert by_name(after.json())["B"]["status"] == "green"


def test_a_started_initiative_can_be_pushed_out(client):
    """A starts in the current month. It can still be moved forward."""
    state = client.get("/api/state").json()
    a = by_name(state)["A"]
    assert a["start_month"] == "2027-01"

    after = client.patch(f"/api/initiatives/{a['id']}", json={"start_month": "2027-06"})
    assert after.status_code == 200
    assert by_name(after.json())["A"]["start_month"] == "2027-06"


def test_a_start_behind_the_current_month_moves_forward_only(client):
    """The clock runs on past A's start. Forward is allowed, back is not."""
    state = client.put("/api/settings", json={"current_month": "2027-04"}).json()
    a = by_name(state)["A"]
    assert a["start_month"] == "2027-01"

    backwards = client.patch(f"/api/initiatives/{a['id']}", json={"start_month": "2027-03"})
    assert backwards.status_code == 400
    assert "before the current month" in backwards.json()["detail"]

    forwards = client.patch(f"/api/initiatives/{a['id']}", json={"start_month": "2027-09"})
    assert forwards.status_code == 200
    assert by_name(forwards.json())["A"]["start_month"] == "2027-09"


def test_editing_other_fields_leaves_a_past_start_alone(client):
    """Renaming an initiative whose start is behind the clock must not trip
    the no-starts-in-the-past check."""
    client.put("/api/settings", json={"current_month": "2027-04"})
    state = client.get("/api/state").json()
    a = by_name(state)["A"]

    response = client.patch(
        f"/api/initiatives/{a['id']}", json={"name": "A renamed", "start_month": "2027-01"}
    )
    assert response.status_code == 200
    renamed = by_name(response.json())["A renamed"]
    assert renamed["start_month"] == "2027-01"


def test_start_in_the_past_is_rejected(client):
    response = client.post(
        "/api/initiatives", json={"name": "Late", "start_month": "2026-12"}
    )
    assert response.status_code == 400
    assert "before the current month" in response.json()["detail"]


def test_fit_hints_endpoint(client):
    state = client.get("/api/state").json()
    b = by_name(state)["B"]
    hints = client.get(f"/api/initiatives/{b['id']}/fit").json()["hints"]
    assert hints[0] == "2027-07"
    assert hints[-1] == "2028-10"


def test_rerank_cascade(client):
    """T4 through the API: a new top-ranked initiative displaces A."""
    state = client.post(
        "/api/initiatives", json={"name": "D", "start_month": "2027-02", "end_month": "2027-03"}
    ).json()
    d = by_name(state)["D"]
    state = client.put(
        f"/api/initiatives/{d['id']}/demand",
        json={
            "lines": [
                {"team_id": state["teams"][1]["id"], "offset": 0, "fte_h": 50},
                {"team_id": state["teams"][1]["id"], "offset": 1, "fte_h": 50},
            ]
        },
    ).json()
    # teams come back sorted by name: GRC then SOC.
    assert state["teams"][1]["name"] == "SOC"

    order = [d["id"]] + [i["id"] for i in state["initiatives"] if i["id"] != d["id"]]
    state = client.post("/api/initiatives/reorder", json={"ordered_ids": order}).json()

    initiatives = by_name(state)
    assert initiatives["D"]["rank"] == 1
    assert initiatives["D"]["status"] == "green"
    assert initiatives["A"]["status"] == "red"
    assert initiatives["B"]["status"] == "green"
    assert initiatives["C"]["status"] == "green"


def test_reserve_exceeding_supply_warns(client):
    state = client.get("/api/state").json()
    grc = next(t for t in state["teams"] if t["name"] == "GRC")
    state = client.put(
        "/api/supply",
        json={"team_id": grc["id"], "from_month": "2027-05", "to_month": "2027-05", "fte_h": 50},
    ).json()

    cell = state["cells"][f"{grc['id']}|2027-05"]
    assert cell["warnings"] == ["reserve_exceeds_supply"]
    assert cell["free"] == 0
    assert by_name(state)["A"]["status"] == "red"


def test_clearing_supply_is_no_supply_data(client):
    state = client.get("/api/state").json()
    soc = next(t for t in state["teams"] if t["name"] == "SOC")
    state = client.put(
        "/api/supply",
        json={"team_id": soc["id"], "from_month": "2027-04", "to_month": "2027-06", "fte_h": None},
    ).json()

    assert state["cells"][f"{soc['id']}|2027-04"]["has_supply_row"] is False
    c = by_name(state)["C"]
    assert c["status"] == "red"
    assert {s["reason"] for s in c["shortfalls"]} == {"no_supply_data"}


def test_bad_input_is_rejected(client):
    state = client.get("/api/state").json()
    soc = state["teams"][0]
    assert (
        client.put(
            "/api/supply",
            json={"team_id": soc["id"], "from_month": "2027-13", "to_month": "2027-14", "fte_h": 10},
        ).status_code
        == 400
    )
    assert (
        client.put(
            "/api/supply",
            json={"team_id": soc["id"], "from_month": "2027-01", "to_month": "2027-01", "fte_h": 999999},
        ).status_code
        == 400
    )
    assert (
        client.post("/api/initiatives/reorder", json={"ordered_ids": [1]}).status_code == 400
    )


def test_archive_frees_capacity(client):
    state = client.get("/api/state").json()
    a = by_name(state)["A"]
    state = client.patch(f"/api/initiatives/{a['id']}", json={"archived": True}).json()
    initiatives = by_name(state)
    assert initiatives["A"]["status"] == "archived"
    assert initiatives["B"]["status"] == "green"


def test_clock_override_changes_evaluation(client):
    state = client.put("/api/settings", json={"current_month": "2027-04"}).json()
    initiatives = by_name(state)
    assert state["settings"]["current_month"] == "2027-04"
    assert initiatives["A"]["status"] == "green"
    assert [(s["month"], s["short"]) for s in initiatives["B"]["shortfalls"]] == [
        ("2027-04", 25)
    ]


def test_health(client):
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert body["teams"] == 2


def test_requested_start_month_column_is_dropped(tmp_path):
    """A database created before the field was retired still carries the
    column. init_db removes it, and doing so twice is harmless."""
    db.DB_PATH = str(tmp_path / "old.db")
    conn = db.connect()
    db.init_db(conn)
    conn.execute("ALTER TABLE initiative ADD COLUMN requested_start_month TEXT")
    assert "requested_start_month" in _columns(conn)

    db.init_db(conn)
    assert "requested_start_month" not in _columns(conn)
    db.init_db(conn)
    assert "requested_start_month" not in _columns(conn)
    conn.close()


def _columns(conn):
    return [r["name"] for r in conn.execute("PRAGMA table_info(initiative)")]


# --- the connection and the threadpool ---------------------------------------


def test_a_connection_survives_the_handover_between_threads(tmp_path):
    """FastAPI runs a sync dependency's setup, the endpoint and its teardown as
    three separate threadpool jobs, and anyio need not give them the same worker
    thread. sqlite3's default check_same_thread rejects the handover — as a
    ProgrammingError out of the endpoint, or out of conn.close() after it.
    """
    conn = db.connect(str(tmp_path / "handover.db"))
    db.init_db(conn)

    failures = []

    def use_it_from_another_thread():
        try:
            conn.execute("SELECT id, name FROM team ORDER BY name").fetchall()
            conn.close()
        except Exception as exc:  # noqa: BLE001 — the point is to report any
            failures.append(exc)

    thread = threading.Thread(target=use_it_from_another_thread)
    thread.start()
    thread.join()

    assert failures == []


def test_concurrent_reads_all_succeed(client):
    """One request per worker thread is the case that used to fail: a warm
    threadpool hands the dependency and the endpoint different workers."""
    with ThreadPoolExecutor(max_workers=12) as pool:
        responses = [pool.submit(client.get, "/api/state") for _ in range(24)]
        codes = [r.result().status_code for r in responses]

    assert codes == [200] * 24


def test_concurrent_writes_all_succeed(client):
    with ThreadPoolExecutor(max_workers=8) as pool:
        responses = [
            pool.submit(client.post, "/api/teams", json={"name": f"T{i}"}) for i in range(8)
        ]
        codes = sorted(r.result().status_code for r in responses)

    assert codes == [200] * 8
    names = {t["name"] for t in client.get("/api/state").json()["teams"]}
    assert {f"T{i}" for i in range(8)} <= names


def test_a_shortfall_reaches_the_client_with_its_cause(client):
    """The names are the point: a shortfall the UI cannot explain is a number
    the user has to go and reconstruct by hand."""
    state = client.get("/api/state").json()
    b = by_name(state)["B"]
    assert b["status"] == "red"

    first = b["shortfalls"][0]
    assert [(c["kind"], c["name"], c["fte_h"]) for c in first["taken_by"]] == [
        ("reserve", "BAU", 50),
        ("reserve", "Unplanned", 25),
        ("initiative", "A", 50),
    ]
    assert first["supply"] == 150
    assert first["wanted"] == 50


# --- undo --------------------------------------------------------------------


def test_undo_puts_a_dragged_start_back(client):
    """The case Ctrl+Z exists for: a bar dropped somewhere, and no memory of
    where it came from."""
    b = by_name(client.get("/api/state").json())["B"]
    assert b["start_month"] == "2027-02" and b["status"] == "red"

    moved = client.patch(f"/api/initiatives/{b['id']}", json={"start_month": "2027-07"}).json()
    assert by_name(moved)["B"]["status"] == "green"

    back = client.post("/api/undo")
    assert back.status_code == 200
    assert back.json()["undone"] == "Moved B to Jul 2027."
    assert by_name(back.json())["B"]["start_month"] == "2027-02"
    assert by_name(back.json())["B"]["status"] == "red"


def test_undo_reaches_back_through_many_changes(client):
    """"A fair way back", not just the last thing."""
    b = by_name(client.get("/api/state").json())["B"]
    for month in ("2027-05", "2027-06", "2027-07", "2027-08", "2027-09"):
        client.patch(f"/api/initiatives/{b['id']}", json={"start_month": month})
    assert client.get("/api/state").json()["undo"]["depth"] == 5

    for _ in range(5):
        assert client.post("/api/undo").status_code == 200

    state = client.get("/api/state").json()
    assert by_name(state)["B"]["start_month"] == "2027-02"
    assert state["undo"]["depth"] == 0


def test_undo_restores_a_deleted_team_with_everything_that_hung_off_it(client):
    """The reason undo is a snapshot rather than an inverse operation: deleting
    a team cascades through supply, reserves and demand, and putting that back
    by hand would be a second implementation of the schema."""
    state = client.get("/api/state").json()
    grc = next(t for t in state["teams"] if t["name"] == "GRC")
    supply_before = state["supply"][str(grc["id"])]
    demand_before = by_name(state)["A"]["demand"]

    client.delete(f"/api/teams/{grc['id']}")
    gone = client.get("/api/state").json()
    assert [t["name"] for t in gone["teams"]] == ["SOC"]

    restored = client.post("/api/undo").json()
    assert restored["undone"] == "Deleted the team GRC."
    assert sorted(t["name"] for t in restored["teams"]) == ["GRC", "SOC"]
    assert restored["supply"][str(grc["id"])] == supply_before
    assert by_name(restored)["A"]["demand"] == demand_before


def test_undo_takes_back_a_whole_import(client):
    """An import can touch forty initiatives. It is one action, so it is one
    checkpoint and one Ctrl+Z."""
    before = len(client.get("/api/state").json()["initiatives"])
    csv = (
        "InitiativeName,Reference,StartMonth,EndMonth,SOC,GRC\n"
        "Imported one,IMP-1,2027-03,2027-05,0.5,\n"
        "Imported two,IMP-2,2027-04,2027-06,,0.25\n"
    )
    assert client.post("/api/import", json={"csv": csv}).status_code == 200
    assert len(client.get("/api/state").json()["initiatives"]) == before + 2

    undone = client.post("/api/undo").json()
    assert undone["undone"] == "Imported 2 rows."
    assert len(undone["initiatives"]) == before
    assert "Imported one" not in by_name(undone)


def test_one_save_in_the_editor_is_one_undo(client):
    """The editor saves an initiative as two requests — the fields, then the
    demand grid. `amend` folds the second into the first so that one click is
    one press of Ctrl+Z, not two."""
    state = client.get("/api/state").json()
    soc = next(t for t in state["teams"] if t["name"] == "SOC")
    depth = state["undo"]["depth"]

    created = client.post(
        "/api/initiatives", json={"name": "Delta", "start_month": "2027-05"}
    ).json()
    new_id = by_name(created)["Delta"]["id"]
    client.put(
        f"/api/initiatives/{new_id}/demand",
        params={"amend": "1"},
        json={"lines": [{"team_id": soc["id"], "offset": 0, "fte_h": 50}]},
    )
    assert client.get("/api/state").json()["undo"]["depth"] == depth + 1

    undone = client.post("/api/undo").json()
    assert "Delta" not in by_name(undone)
    assert undone["undo"]["depth"] == depth


def test_a_demand_change_on_its_own_is_undoable(client):
    """Without amend, a demand replacement checkpoints like anything else."""
    state = client.get("/api/state").json()
    soc = next(t for t in state["teams"] if t["name"] == "SOC")
    a = by_name(state)["A"]

    client.put(
        f"/api/initiatives/{a['id']}/demand",
        json={"lines": [{"team_id": soc["id"], "offset": 0, "fte_h": 25}]},
    )
    assert by_name(client.get("/api/state").json())["A"]["demand"] == [
        {"team_id": soc["id"], "offset": 0, "fte_h": 25}
    ]

    undone = client.post("/api/undo").json()
    assert undone["undone"] == "Changed the demand for A."
    assert by_name(undone)["A"]["demand"] == a["demand"]


def test_a_rejected_change_leaves_no_checkpoint(client):
    """The snapshot rides the same transaction as the change it precedes, so a
    request that fails validation must leave nothing behind — otherwise Ctrl+Z
    would spend a press undoing something that never happened."""
    b = by_name(client.get("/api/state").json())["B"]
    depth = client.get("/api/state").json()["undo"]["depth"]

    refused = client.patch(f"/api/initiatives/{b['id']}", json={"start_month": "2020-01"})
    assert refused.status_code == 400
    assert client.get("/api/state").json()["undo"]["depth"] == depth

    duplicate = client.post("/api/teams", json={"name": "SOC"})
    assert duplicate.status_code == 400
    assert client.get("/api/state").json()["undo"]["depth"] == depth


def test_the_undo_stack_is_capped(client):
    """Fifty is well past "what did I just drag?"; unbounded is a memory leak
    with a plan in it."""
    b = by_name(client.get("/api/state").json())["B"]
    for n in range(db.UNDO_DEPTH + 5):
        client.patch(
            f"/api/initiatives/{b['id']}", json={"name": f"B{n}", "start_month": "2027-02"}
        )
    assert client.get("/api/state").json()["undo"]["depth"] == db.UNDO_DEPTH


def test_nothing_to_undo_is_refused_not_silently_ignored(client):
    while client.get("/api/state").json()["undo"]["depth"]:
        client.post("/api/undo")
    empty = client.post("/api/undo")
    assert empty.status_code == 400
    assert "nothing" in empty.json()["detail"].lower()


def test_the_state_names_what_undo_would_take_back(client):
    """"Undo" on its own is a question. The label is the answer."""
    state = client.get("/api/state").json()
    assert state["undo"] == {"depth": 0, "label": None}

    grc = next(t for t in state["teams"] if t["name"] == "GRC")
    client.post("/api/teams", json={"name": "ENG"})
    assert client.get("/api/state").json()["undo"]["label"] == "Added the team ENG."

    client.put(
        "/api/supply",
        json={"team_id": grc["id"], "from_month": "2027-01", "to_month": "2027-03", "fte_h": 175},
    )
    assert (
        client.get("/api/state").json()["undo"]["label"]
        == "Set supply for GRC to 1.75 FTE, Jan 2027 to Mar 2027."
    )

    ordered = [i["id"] for i in client.get("/api/state").json()["initiatives"]]
    client.post("/api/initiatives/reorder", json={"ordered_ids": ordered[::-1]})
    assert client.get("/api/state").json()["undo"]["label"] == "Moved C to rank 1."


def test_undoing_a_reserve_edit_restores_the_lines(client):
    state = client.get("/api/state").json()
    bau = next(r for r in state["reserves"] if r["name"] == "BAU")
    soc = next(t for t in state["teams"] if t["name"] == "SOC")
    before = bau["lines"][str(soc["id"])]

    client.put(
        f"/api/reserves/{bau['id']}/lines",
        json={"team_id": soc["id"], "from_month": "2027-01", "to_month": "2027-12", "fte_h": None},
    )
    cleared = next(
        r for r in client.get("/api/state").json()["reserves"] if r["name"] == "BAU"
    )["lines"][str(soc["id"])]
    # Only the twelve months asked for; the fixture runs for twenty-four.
    assert "2027-06" not in cleared and cleared["2028-06"] == 100

    undone = client.post("/api/undo").json()
    assert next(r for r in undone["reserves"] if r["name"] == "BAU")["lines"][str(soc["id"])] == before


# --- reset to zero -----------------------------------------------------------


def test_reset_to_zero_empties_the_plan_but_keeps_the_settings(client):
    client.put("/api/settings", json={"horizon_months": 12, "current_month": "2027-01"})
    state = client.post("/api/reset").json()

    assert state["teams"] == []
    assert state["initiatives"] == []
    assert state["reserves"] == []
    assert state["supply"] == {}
    # How you are looking at a plan is not part of one.
    assert state["settings"]["horizon_months"] == 12
    assert state["settings"]["current_month"] == "2027-01"


def test_reset_to_zero_is_undoable(client):
    before = client.get("/api/state").json()
    client.post("/api/reset")
    undone = client.post("/api/undo").json()
    assert undone["undone"] == "Reset to an empty plan."
    assert sorted(t["name"] for t in undone["teams"]) == ["GRC", "SOC"]
    assert by_name(undone).keys() == by_name(before).keys()


def test_reset_to_zero_survives_a_restart(tmp_path):
    """The startup hook seeds the fixture into a fresh database. An emptied one
    is not fresh — it is a deliberate blank page — and with the deploy hook
    rebuilding the container on every commit, reading it as fresh would put the
    fixture back within the minute."""
    db.DB_PATH = str(tmp_path / "restart.db")
    import app as app_module

    conn = db.connect()
    db.init_db(conn)
    db.seed_fixture(conn, anchor="2027-01")
    conn.close()

    with TestClient(app_module.app) as first:
        assert first.post("/api/reset").json()["teams"] == []

    with TestClient(app_module.app) as second:
        assert second.get("/api/state").json()["teams"] == [], "the fixture came back"


def test_an_untouched_database_is_still_seeded(tmp_path):
    """The guard must not stop a genuinely new install getting its demo."""
    db.DB_PATH = str(tmp_path / "fresh.db")
    import app as app_module

    with TestClient(app_module.app) as fresh:
        assert sorted(t["name"] for t in fresh.get("/api/state").json()["teams"]) == ["GRC", "SOC"]


def test_a_database_predating_the_seeded_flag_is_left_alone(tmp_path):
    """The flag's first act must not be to declare an existing plan fresh."""
    path = str(tmp_path / "old.db")
    db.DB_PATH = path
    conn = db.connect()
    db.init_db(conn)
    db.seed_fixture(conn, anchor="2027-01")
    conn.execute("UPDATE initiative SET name = 'Real work' WHERE name = 'A'")
    # Wind the database back to before the flag existed.
    conn.execute("DELETE FROM setting WHERE key = 'seeded'")
    conn.commit()
    conn.close()

    import app as app_module

    with TestClient(app_module.app) as client_:
        assert "Real work" in by_name(client_.get("/api/state").json())


# --- export ------------------------------------------------------------------


def export_lines(client):
    response = client.get("/api/export.csv")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert "attachment" in response.headers["content-disposition"]
    return [line for line in response.text.splitlines() if line]


def test_export_is_the_five_identity_columns_in_rank_order(client):
    lines = export_lines(client)
    assert lines[0] == "InitiativeName,Reference,StartMonth,EndMonth,Deadline"
    # A runs 2027-01 for six months; B for three from 2027-02; C for three
    # from 2027-04. None of the fixture has a deadline, so that column is blank.
    assert lines[1:] == [
        "A,,2027-01,2027-06,",
        "B,,2027-02,2027-04,",
        "C,,2027-04,2027-06,",
    ]


def test_export_end_month_is_the_stored_end(client):
    """The end is set on the initiative, not read off its demand: the grid
    reaching further needs the end moved first, and a grid that stops short
    of the end does not bring it in."""
    state = client.get("/api/state").json()
    soc = next(t for t in state["teams"] if t["name"] == "SOC")
    c = by_name(state)["C"]
    client.patch(f"/api/initiatives/{c['id']}", json={"end_month": "2027-12"})
    client.put(
        f"/api/initiatives/{c['id']}/demand",
        json={"lines": [{"team_id": soc["id"], "offset": o, "fte_h": 25} for o in range(9)]},
    )
    assert "C,,2027-04,2027-12," in export_lines(client)

    client.put(
        f"/api/initiatives/{c['id']}/demand",
        json={"lines": [{"team_id": soc["id"], "offset": 0, "fte_h": 25}]},
    )
    assert "C,,2027-04,2027-12," in export_lines(client)


def test_export_carries_the_deadline(client):
    c = by_name(client.get("/api/state").json())["C"]
    client.patch(f"/api/initiatives/{c['id']}", json={"deadline_month": "2027-09"})
    assert "C,,2027-04,2027-06,2027-09" in export_lines(client)


def test_export_carries_the_reference_an_import_set(client):
    csv = (
        "InitiativeName,Reference,StartMonth,EndMonth,SOC,GRC\n"
        "Tracked,PRO-77,2027-03,2027-05,0.5,\n"
    )
    assert client.post("/api/import", json={"csv": csv}).status_code == 200
    assert "Tracked,PRO-77,2027-03,2027-05," in export_lines(client)


def test_export_leaves_archived_initiatives_out(client):
    b = by_name(client.get("/api/state").json())["B"]
    client.patch(f"/api/initiatives/{b['id']}", json={"archived": True})
    names = [line.split(",")[0] for line in export_lines(client)[1:]]
    assert names == ["A", "C"]


def test_export_neutralises_a_name_that_would_be_a_formula(client):
    """The file is meant to be opened in a spreadsheet, where a leading "=" is
    executed rather than displayed."""
    a = by_name(client.get("/api/state").json())["A"]
    client.patch(f"/api/initiatives/{a['id']}", json={"name": "=HYPERLINK(\"x\")"})
    row = next(line for line in export_lines(client) if "HYPERLINK" in line)
    assert row.startswith("\"'=HYPERLINK")


def test_an_initiative_with_no_demand_occupies_its_start_month(client):
    created = client.post(
        "/api/initiatives", json={"name": "Empty", "start_month": "2027-08"}
    ).json()
    assert created is not None
    assert "Empty,,2027-08,2027-08," in export_lines(client)


def test_the_export_is_not_an_import(client):
    """Feeding an export back in is refused, and that is the safe answer: an
    import replaces the demand of every row it matches, so a file with no team
    columns would quietly empty every profile it touched."""
    csv = client.get("/api/export.csv").text
    refused = client.post("/api/import", json={"csv": csv})
    assert refused.status_code == 400
    assert any("team column" in e for e in refused.json()["detail"]["errors"])


def test_export_touches_nothing(client):
    before = client.get("/api/state").json()
    client.get("/api/export.csv")
    assert client.get("/api/state").json() == before


# --- end month ---------------------------------------------------------------


def undo_label(client):
    return client.get("/api/state").json()["undo"]["label"]


def test_the_fixture_carries_its_ends(client):
    initiatives = by_name(client.get("/api/state").json())
    assert {n: (i["end_month"], i["length"]) for n, i in initiatives.items()} == {
        "A": ("2027-06", 6),
        "B": ("2027-04", 3),
        "C": ("2027-06", 3),
    }
    assert {i["deadline_month"] for i in initiatives.values()} == {None}


def test_a_new_initiative_takes_an_end_and_a_deadline(client):
    state = client.post(
        "/api/initiatives",
        json={
            "name": "Delta",
            "start_month": "2027-05",
            "end_month": "2027-08",
            "deadline_month": "2027-10",
        },
    ).json()
    delta = by_name(state)["Delta"]
    assert delta["end_month"] == "2027-08"
    assert delta["length"] == 4
    assert delta["deadline_month"] == "2027-10"


def test_a_new_initiative_is_one_month_long_with_no_deadline_by_default(client):
    for n, extra in enumerate(({}, {"deadline_month": None}, {"deadline_month": ""})):
        name = f"Delta {n}"
        state = client.post(
            "/api/initiatives", json={"name": name, "start_month": "2027-05", **extra}
        ).json()
        created = by_name(state)[name]
        assert (created["end_month"], created["length"]) == ("2027-05", 1)
        assert created["deadline_month"] is None


def test_moving_the_start_carries_the_end(client):
    """A drag sends only the start. The end is stored as a duration, so it
    travels with the start as the demand profile does."""
    b = by_name(client.get("/api/state").json())["B"]
    moved = by_name(
        client.patch(f"/api/initiatives/{b['id']}", json={"start_month": "2027-07"}).json()
    )["B"]
    assert (moved["start_month"], moved["end_month"], moved["length"]) == ("2027-07", "2027-09", 3)
    assert moved["demand"] == b["demand"]


def test_an_end_sent_with_a_start_is_measured_from_the_new_start(client):
    b = by_name(client.get("/api/state").json())["B"]
    moved = by_name(
        client.patch(
            f"/api/initiatives/{b['id']}", json={"start_month": "2027-07", "end_month": "2027-12"}
        ).json()
    )["B"]
    assert (moved["end_month"], moved["length"]) == ("2027-12", 6)


def test_shortening_the_end_trims_the_demand_past_it(client):
    """The invariant is that nothing is asked for after the end, even from an
    API client that never re-sends the grid. A's GRC demand ran to June."""
    state = client.get("/api/state").json()
    grc = next(t for t in state["teams"] if t["name"] == "GRC")
    a = by_name(state)["A"]

    after = by_name(
        client.patch(f"/api/initiatives/{a['id']}", json={"end_month": "2027-03"}).json()
    )["A"]
    assert (after["end_month"], after["length"]) == ("2027-03", 3)
    assert max(d["offset"] for d in after["demand"]) == 2
    assert [d["offset"] for d in after["demand"] if d["team_id"] == grc["id"]] == [0, 1, 2]

    # One change, one undo, and the trimmed months come back with it.
    undone = client.post("/api/undo").json()
    assert undone["undone"] == "Changed the end of A to Mar 2027."
    assert by_name(undone)["A"]["demand"] == a["demand"]
    assert by_name(undone)["A"]["end_month"] == "2027-06"


def test_lengthening_the_end_leaves_the_demand_alone(client):
    c = by_name(client.get("/api/state").json())["C"]
    after = by_name(
        client.patch(f"/api/initiatives/{c['id']}", json={"end_month": "2027-12"}).json()
    )["C"]
    assert (after["end_month"], after["length"]) == ("2027-12", 9)
    assert after["demand"] == c["demand"]


def test_an_end_before_the_start_is_refused(client):
    c = by_name(client.get("/api/state").json())["C"]
    refused = client.patch(f"/api/initiatives/{c['id']}", json={"end_month": "2027-03"})
    assert refused.status_code == 400
    assert refused.json()["detail"] == "The end month, Mar 2027, is before the start month, Apr 2027."

    created = client.post(
        "/api/initiatives", json={"name": "Odd", "start_month": "2027-05", "end_month": "2027-04"}
    )
    assert created.status_code == 400
    assert "Odd" not in by_name(client.get("/api/state").json())


def test_an_end_more_than_ten_years_out_is_refused(client):
    """The demand grid's offsets stop at 119, so a longer initiative would
    run for months nobody could be asked for."""
    too_long = client.post(
        "/api/initiatives", json={"name": "Long", "start_month": "2027-01", "end_month": "2037-01"}
    )
    assert too_long.status_code == 400
    assert "more than the limit of 120" in too_long.json()["detail"]

    longest = client.post(
        "/api/initiatives", json={"name": "Long", "start_month": "2027-01", "end_month": "2036-12"}
    )
    assert by_name(longest.json())["Long"]["length"] == 120


def test_demand_past_the_end_is_refused(client):
    """Refused rather than trimmed: the grid and the end disagree about how
    long the initiative runs, and quietly picking one would lose the other."""
    state = client.get("/api/state").json()
    soc = next(t for t in state["teams"] if t["name"] == "SOC")
    c = by_name(state)["C"]

    for params in ({}, {"amend": "1"}):
        refused = client.put(
            f"/api/initiatives/{c['id']}/demand",
            params=params,
            json={"lines": [{"team_id": soc["id"], "offset": o, "fte_h": 50} for o in range(4)]},
        )
        assert refused.status_code == 400
        assert refused.json()["detail"] == (
            "Demand runs past the end month, Jun 2027. Move the end month first."
        )

    after = client.get("/api/state").json()
    assert by_name(after)["C"]["demand"] == c["demand"]
    assert after["undo"]["depth"] == state["undo"]["depth"]


def test_demand_for_an_initiative_that_does_not_exist_is_404(client):
    response = client.put("/api/initiatives/9999/demand", json={"lines": []})
    assert response.status_code == 404


def test_an_initiative_under_way_can_have_its_end_changed(client):
    """R8 guards the start, not the end: an initiative that began before the
    clock can still be shortened (R6) or run on, and saving the editor sends
    its unchanged start along with the new end."""
    client.put("/api/settings", json={"current_month": "2027-04"})
    a = by_name(client.get("/api/state").json())["A"]
    response = client.patch(
        f"/api/initiatives/{a['id']}", json={"start_month": "2027-01", "end_month": "2027-04"}
    )
    assert response.status_code == 200
    assert by_name(response.json())["A"]["end_month"] == "2027-04"


# --- deadline (R11) ----------------------------------------------------------


def set_deadline(client, name, month):
    target = by_name(client.get("/api/state").json())[name]
    response = client.patch(f"/api/initiatives/{target['id']}", json={"deadline_month": month})
    assert response.status_code == 200, response.json()
    return by_name(response.json())[name]


def test_a_deadline_can_be_set_and_cleared(client):
    assert set_deadline(client, "C", "2027-09")["deadline_month"] == "2027-09"
    assert set_deadline(client, "C", None)["deadline_month"] is None
    assert set_deadline(client, "C", "2027-09")["deadline_month"] == "2027-09"
    # Blank clears it too: it is what the editor's "No deadline" option sends.
    assert set_deadline(client, "C", "")["deadline_month"] is None


def test_clearing_a_deadline_lets_the_initiative_move_later(client):
    c = set_deadline(client, "C", "2027-06")
    later = {"start_month": "2027-05"}
    assert client.patch(f"/api/initiatives/{c['id']}", json=later).status_code == 400
    set_deadline(client, "C", None)
    assert client.patch(f"/api/initiatives/{c['id']}", json=later).status_code == 200


def test_a_deadline_on_the_end_month_is_allowed(client):
    assert set_deadline(client, "C", "2027-06")["end_month"] == "2027-06"


def test_a_create_that_would_end_after_its_deadline_is_refused(client):
    before = client.get("/api/state").json()
    refused = client.post(
        "/api/initiatives",
        json={
            "name": "Late",
            "start_month": "2027-05",
            "end_month": "2027-08",
            "deadline_month": "2027-07",
        },
    )
    assert refused.status_code == 400
    assert refused.json()["detail"] == "That would end Late in Aug 2027, after its deadline of Jul 2027."

    after = client.get("/api/state").json()
    assert after["initiatives"] == before["initiatives"]
    assert after["undo"]["depth"] == before["undo"]["depth"]


@pytest.mark.parametrize(
    "change,message",
    [
        # A drag: the start moves, the end rides along with it past July.
        ({"start_month": "2027-06"}, "That would end C in Aug 2027, after its deadline of Jul 2027."),
        # The end moved out on its own.
        ({"end_month": "2027-08"}, "That would end C in Aug 2027, after its deadline of Jul 2027."),
        # The deadline brought in before the end.
        ({"deadline_month": "2027-05"}, "That would end C in Jun 2027, after its deadline of May 2027."),
        # All three at once, as the editor's Save sends them.
        (
            {"start_month": "2027-05", "end_month": "2027-09", "deadline_month": "2027-08"},
            "That would end C in Sep 2027, after its deadline of Aug 2027.",
        ),
        # A rename in the same request names the initiative by its new name.
        (
            {"name": "C2", "end_month": "2027-08"},
            "That would end C2 in Aug 2027, after its deadline of Jul 2027.",
        ),
    ],
)
def test_every_way_past_a_deadline_is_refused(client, change, message):
    """R11 is enforced on the server whatever the client does, and a refusal
    writes nothing — not even an undo checkpoint."""
    set_deadline(client, "C", "2027-07")  # C runs April to June
    before = client.get("/api/state").json()
    c = by_name(before)["C"]

    refused = client.patch(f"/api/initiatives/{c['id']}", json=change)
    assert refused.status_code == 400
    assert refused.json()["detail"] == message

    after = client.get("/api/state").json()
    assert after["initiatives"] == before["initiatives"]
    assert after["undo"]["depth"] == before["undo"]["depth"]


def test_the_deadline_is_checked_on_the_result_not_the_field(client):
    """A start that would breach the deadline on its own is fine when the end
    sent with it keeps the initiative inside."""
    c = set_deadline(client, "C", "2027-07")
    response = client.patch(
        f"/api/initiatives/{c['id']}", json={"start_month": "2027-06", "end_month": "2027-07"}
    )
    assert response.status_code == 200
    assert by_name(response.json())["C"]["end_month"] == "2027-07"


def test_fit_hints_stop_at_the_deadline(client):
    """So the drag shading never offers a month the server would refuse."""
    b = set_deadline(client, "B", "2027-12")
    hints = client.get(f"/api/initiatives/{b['id']}/fit").json()["hints"]
    assert hints == ["2027-07", "2027-08", "2027-09", "2027-10"]


# --- edge glows in the state -------------------------------------------------


def test_the_state_says_which_ends_of_a_bar_have_room(client):
    state = client.get("/api/state").json()
    edges = {n: (i["earliest_start"], i["end_limit"]) for n, i in by_name(state).items()}
    # A has started, B is red, and C is already at its earliest start.
    assert edges == {"A": (None, None), "B": (None, None), "C": (None, None)}

    c = by_name(state)["C"]
    state = client.patch(f"/api/initiatives/{c['id']}", json={"start_month": "2027-08"}).json()
    assert by_name(state)["C"]["earliest_start"] == "2027-04"

    assert set_deadline(client, "C", "2027-10")["end_limit"] == "deadline"
    assert set_deadline(client, "B", "2027-04")["end_limit"] == "deadline"  # red, but pinned


def test_capacity_running_out_reaches_the_state(client):
    """The fixture's supply stops after December 2028."""
    c = by_name(client.get("/api/state").json())["C"]
    state = client.patch(f"/api/initiatives/{c['id']}", json={"start_month": "2028-10"}).json()
    assert by_name(state)["C"]["status"] == "green"
    assert by_name(state)["C"]["end_limit"] == "capacity"


def test_an_initiative_is_not_past_while_its_end_is_ahead(client):
    """B's demand runs July to September, its end and deadline to December.
    With the clock in October its demand is all behind it, but it is still
    running: not past, and still on its deadline, which is what draws the red
    glow and the tick."""
    b = by_name(client.get("/api/state").json())["B"]
    client.patch(
        f"/api/initiatives/{b['id']}",
        json={"start_month": "2027-07", "end_month": "2027-12", "deadline_month": "2027-12"},
    )
    state = client.put("/api/settings", json={"current_month": "2027-10"}).json()
    b = by_name(state)["B"]
    assert (b["status"], b["end_limit"], b["end_month"]) == ("green", "deadline", "2027-12")

    state = client.put("/api/settings", json={"current_month": "2028-01"}).json()
    assert by_name(state)["B"]["status"] == "past"


# --- month names in what a person reads --------------------------------------


def test_r8_names_the_months(client):
    response = client.post("/api/initiatives", json={"name": "Late", "start_month": "2026-12"})
    assert response.json()["detail"] == "Start month Dec 2026 is before the current month Jan 2027."


def test_a_malformed_month_still_says_what_form_is_wanted(client):
    """This one describes something to type, so it keeps the typed form."""
    b = by_name(client.get("/api/state").json())["B"]
    response = client.patch(f"/api/initiatives/{b['id']}", json={"end_month": "2027-13"})
    assert response.status_code == 400
    assert response.json()["detail"] == "Not a month: '2027-13'. Expected YYYY-MM."


def test_undo_labels_name_the_change_and_the_month(client):
    state = client.get("/api/state").json()
    a, b = by_name(state)["A"], by_name(state)["B"]

    client.patch(f"/api/initiatives/{a['id']}", json={"end_month": "2027-03"})
    assert undo_label(client) == "Changed the end of A to Mar 2027."

    client.patch(f"/api/initiatives/{a['id']}", json={"deadline_month": "2027-03"})
    assert undo_label(client) == "Set a deadline of Mar 2027 on A."

    client.patch(f"/api/initiatives/{a['id']}", json={"deadline_month": "2027-05"})
    assert undo_label(client) == "Set a deadline of May 2027 on A."

    client.patch(f"/api/initiatives/{a['id']}", json={"deadline_month": None})
    assert undo_label(client) == "Removed the deadline from A."

    # A drag is the commonest change, so a moved start is what gets named
    # even when the end moved with it.
    client.patch(f"/api/initiatives/{b['id']}", json={"start_month": "2027-07", "end_month": "2027-10"})
    assert undo_label(client) == "Moved B to Jul 2027."


def test_an_editor_save_is_named_by_what_actually_changed(client):
    """Save sends every field. Unchanged ones must not claim the label."""
    b = by_name(client.get("/api/state").json())["B"]
    everything = {
        "name": "B", "owner": "", "notes": "",
        "start_month": b["start_month"], "end_month": b["end_month"], "deadline_month": None,
    }
    client.patch(f"/api/initiatives/{b['id']}", json=everything)
    assert undo_label(client) == "Edited B."

    client.patch(f"/api/initiatives/{b['id']}", json={**everything, "deadline_month": "2027-06"})
    assert undo_label(client) == "Set a deadline of Jun 2027 on B."

    client.patch(
        f"/api/initiatives/{b['id']}",
        json={**everything, "deadline_month": "2027-06", "end_month": "2027-05"},
    )
    assert undo_label(client) == "Changed the end of B to May 2027."


# --- migrating a database from before end months -----------------------------

# The initiative table as it stood before end months and deadlines, written
# out rather than derived from db.SCHEMA so that it cannot drift along with it.
OLD_INITIATIVE_TABLE = """
CREATE TABLE initiative (
    id                    INTEGER PRIMARY KEY,
    name                  TEXT NOT NULL,
    "rank"                INTEGER NOT NULL,
    start_month           TEXT NOT NULL,
    reference             TEXT,
    owner                 TEXT NOT NULL DEFAULT '',
    notes                 TEXT NOT NULL DEFAULT '',
    archived              INTEGER NOT NULL DEFAULT 0,
    created_at            TEXT NOT NULL,
    updated_at            TEXT NOT NULL
);
"""


def test_a_database_from_before_end_months_is_migrated(tmp_path):
    """Each existing initiative keeps the end its demand implied — the last
    offset plus one, or one month with no demand — and has no deadline. Undo
    snapshots taken with the old shape are dropped, as they could not be put
    back into the new one."""
    db.DB_PATH = str(tmp_path / "before-end-months.db")
    conn = db.connect()
    conn.executescript(OLD_INITIATIVE_TABLE)
    conn.executescript(db.SCHEMA)  # every other table, unchanged by this migration
    assert "duration_m" not in _columns(conn)

    team = conn.execute("INSERT INTO team (name) VALUES ('SOC')").lastrowid
    for rank, (name, offsets) in enumerate((("Long", [0, 4]), ("Empty", [])), start=1):
        initiative = conn.execute(
            'INSERT INTO initiative (name, "rank", start_month, created_at, updated_at) '
            "VALUES (?, ?, '2027-02', 'then', 'then')",
            (name, rank),
        ).lastrowid
        for offset in offsets:
            conn.execute(
                "INSERT INTO demand (initiative_id, team_id, offset_m, fte_h) VALUES (?, ?, ?, 50)",
                (initiative, team, offset),
            )
    conn.execute("INSERT INTO setting (key, value) VALUES ('current_month', '2027-01')")
    db.checkpoint(conn, "Something from before.")
    conn.commit()

    db.init_db(conn)
    rows = {
        r["name"]: (r["duration_m"], r["deadline_month"])
        for r in conn.execute("SELECT name, duration_m, deadline_month FROM initiative")
    }
    assert rows == {"Long": (5, None), "Empty": (1, None)}
    assert db.undo_state(conn)["depth"] == 0

    # Idempotent: a second start does not derive the durations again.
    conn.execute("UPDATE initiative SET duration_m = 9 WHERE name = 'Long'")
    conn.commit()
    db.init_db(conn)
    assert conn.execute("SELECT duration_m FROM initiative WHERE name = 'Long'").fetchone()[0] == 9
    conn.close()

    import app as app_module

    with TestClient(app_module.app) as migrated:
        initiatives = by_name(migrated.get("/api/state").json())
    assert initiatives["Long"]["end_month"] == "2027-10"
    assert initiatives["Empty"]["end_month"] == "2027-02"
    assert initiatives["Empty"]["deadline_month"] is None
