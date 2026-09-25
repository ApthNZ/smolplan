"""Nothing reachable from ordinary input may 500, a request that changes
nothing must not cost a step of the undo history, and the schema's invariants
must survive any sequence of operations.

Ported from smoltask's suite of the same name. Every 500 in the first table was
real: a duplicate check that compared the unstripped name and then inserted the
stripped one, a rename with no duplicate check at all, foreign keys handed ids
that name nothing, and ids too large for SQLite's 64 bits, which the driver
answers with OverflowError rather than a miss.
"""

import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from fastapi.testclient import TestClient

import db

HUGE = [2**63, 2**70, 10**30, -(2**63) - 1]


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


def state(client):
    return client.get("/api/state").json()


def depth(client):
    return state(client)["undo"]["depth"]


def ids(client):
    s = state(client)
    return {
        "team": s["teams"][0]["id"],
        "other_team": s["teams"][1]["id"],
        "team_name": s["teams"][0]["name"],
        "other_name": s["teams"][1]["name"],
        "initiative": s["initiatives"][0]["id"],
    }


# --- no 500s ---------------------------------------------------------------------


def test_a_duplicate_name_with_a_trailing_space_is_a_400(client):
    name = ids(client)["team_name"]
    response = client.post("/api/teams", json={"name": f"{name} "})
    assert response.status_code == 400
    assert f"already a team called {name}" in response.json()["detail"]


def test_renaming_onto_an_existing_team_is_a_400(client):
    i = ids(client)
    response = client.patch(f"/api/teams/{i['team']}", json={"name": i["other_name"]})
    assert response.status_code == 400


def test_renaming_a_team_to_its_own_name_is_fine(client):
    i = ids(client)
    assert client.patch(f"/api/teams/{i['team']}", json={"name": i["team_name"]}).status_code == 200


@pytest.mark.parametrize("spelling", ["soc", "S.O.C", "s o c", "SOC!", "S-O-C"])
def test_a_team_name_the_importer_could_confuse_is_refused(client, spelling):
    """The importer matches a heading to a team ignoring case, spacing and
    punctuation, so two teams that differ only in those are one team to it."""
    assert "SOC" in [t["name"] for t in state(client)["teams"]]
    assert client.post("/api/teams", json={"name": spelling}).status_code == 400
    i = ids(client)
    target = i["other_team"] if i["team_name"] == "SOC" else i["team"]
    assert client.patch(f"/api/teams/{target}", json={"name": spelling}).status_code == 400


def test_names_made_only_of_punctuation_are_still_told_apart(client):
    assert client.post("/api/teams", json={"name": "🛡"}).status_code == 200
    assert client.post("/api/teams", json={"name": "🗡"}).status_code == 200
    assert client.post("/api/teams", json={"name": "🛡"}).status_code == 400


@pytest.mark.parametrize("team_id", [99999, *HUGE])
def test_supply_for_a_team_that_does_not_exist_is_a_404(client, team_id):
    response = client.put("/api/supply", json={"team_id": team_id, "from_month": "2027-01",
                                               "to_month": "2027-01", "fte_h": 100})
    assert response.status_code == 404


@pytest.mark.parametrize("team_id", [99999, *HUGE])
def test_demand_for_a_team_that_does_not_exist_is_a_400(client, team_id):
    initiative = ids(client)["initiative"]
    response = client.put(f"/api/initiatives/{initiative}/demand",
                          json={"lines": [{"team_id": team_id, "offset": 0, "fte_h": 100}]})
    assert response.status_code == 400


@pytest.mark.parametrize("row_id", [99999, *HUGE])
def test_an_id_that_names_nothing_is_a_404_on_every_route(client, row_id):
    team = ids(client)["team"]
    fill = {"team_id": team, "from_month": "2027-01", "to_month": "2027-01", "fte_h": 100}
    for method, path, body in [
        ("PATCH", f"/api/teams/{row_id}", {"name": "Zed"}),
        ("DELETE", f"/api/teams/{row_id}", None),
        ("DELETE", f"/api/reserves/{row_id}", None),
        ("PUT", f"/api/reserves/{row_id}/lines", fill),
        ("PATCH", f"/api/initiatives/{row_id}", {"notes": "x"}),
        ("DELETE", f"/api/initiatives/{row_id}", None),
        ("PUT", f"/api/initiatives/{row_id}/demand", {"lines": []}),
    ]:
        response = client.request(method, path, json=body)
        assert response.status_code == 404, f"{method} {path}: {response.status_code}"


@pytest.mark.parametrize("row_id", [99999, *HUGE])
def test_fit_hints_for_an_id_that_names_nothing_do_not_crash(client, row_id):
    assert client.get(f"/api/initiatives/{row_id}/fit").status_code < 500


def test_is_possible_id_rejects_what_cannot_name_a_row():
    assert db.is_possible_id(1)
    assert not db.is_possible_id(2**63)
    assert not db.is_possible_id(True), "a bool is not an id"
    assert not db.is_possible_id("1")


@pytest.mark.parametrize("name", ["", " ", "   ", "\t\n", "\x00", "‮", "\x1b"])
def test_a_name_that_cleans_to_nothing_is_refused(client, name):
    """`"   "` passed min_length=1 and was stored stripped to nothing."""
    before = state(client)
    assert client.post("/api/teams", json={"name": name}).status_code in (400, 422)
    assert client.post("/api/reserves", json={"name": name}).status_code in (400, 422)
    assert client.post("/api/initiatives",
                       json={"name": name, "start_month": "2027-06"}).status_code in (400, 422)
    initiative = ids(client)["initiative"]
    assert client.patch(f"/api/initiatives/{initiative}", json={"name": name}).status_code in (400, 422)
    team = ids(client)["team"]
    assert client.patch(f"/api/teams/{team}", json={"name": name}).status_code in (400, 422)
    assert state(client)["teams"] == before["teams"]
    assert "" not in [i["name"] for i in state(client)["initiatives"]]


def test_reorder_refuses_duplicates(client):
    """[1, 1, 2, 3] is the right *set*, and ranked 2, 3, 4."""
    order = [i["id"] for i in state(client)["initiatives"]]
    for bad in ([order[0], *order], [*order, order[-1]], order[:-1] + [order[0]]):
        assert client.post("/api/initiatives/reorder", json={"ordered_ids": bad}).status_code == 400
    assert [i["rank"] for i in state(client)["initiatives"]] == [1, 2, 3]


@pytest.mark.parametrize("payload", [
    {}, [], "x", 42, {"name": None}, {"name": 1}, {"name": []}, {"name": {"a": 1}},
])
def test_no_body_shape_crashes_a_creator(client, payload):
    for path in ("/api/teams", "/api/reserves", "/api/initiatives"):
        assert client.post(path, json=payload).status_code < 500, path


@pytest.mark.parametrize("payload", [
    {"team_id": None}, {"team_id": "1"}, {"team_id": 1.5}, {"from_month": 202701},
    {"fte_h": -1}, {"fte_h": 2**70}, {"fte_h": "1"}, {"to_month": "2027-13"},
])
def test_no_fill_shape_crashes_it(client, payload):
    team = ids(client)["team"]
    body = {"team_id": team, "from_month": "2027-01", "to_month": "2027-02", "fte_h": 100, **payload}
    assert client.put("/api/supply", json=body).status_code < 500


@pytest.mark.parametrize("lines", [
    [{"team_id": 1, "offset": 2**70, "fte_h": 1}],
    [{"team_id": 1, "offset": 0, "fte_h": 2**70}],
    [{"team_id": None, "offset": 0, "fte_h": 1}],
    [{}], "x", None,
])
def test_no_demand_shape_crashes_it(client, lines):
    initiative = ids(client)["initiative"]
    assert client.put(f"/api/initiatives/{initiative}/demand",
                      json={"lines": lines}).status_code < 500


@pytest.mark.parametrize("ordered", [[2**70], [None], "x", [1.5], [-1]])
def test_no_reorder_shape_crashes_it(client, ordered):
    assert client.post("/api/initiatives/reorder", json={"ordered_ids": ordered}).status_code < 500


# --- undo is not eroded by requests that change nothing -------------------------


def test_requests_that_change_nothing_take_no_step_of_the_history(client):
    """Each of these used to push a checkpoint. Fifty of them and the real
    history was gone — and with no redo, gone for good."""
    i = ids(client)
    s = state(client)
    initiative = s["initiatives"][0]
    before = depth(client)
    for method, path, body in [
        ("PATCH", "/api/teams/99999", {"name": "Zed"}),
        ("DELETE", "/api/teams/99999", None),
        ("DELETE", "/api/reserves/99999", None),
        ("DELETE", "/api/initiatives/99999", None),
        ("PUT", "/api/reserves/99999/lines",
         {"team_id": i["team"], "from_month": "2027-01", "to_month": "2027-01", "fte_h": 1}),
        ("PUT", "/api/settings", {}),
        ("PUT", "/api/settings", {"horizon_months": s["settings"]["horizon_months"]}),
        ("PATCH", f"/api/teams/{i['team']}", {"name": i["team_name"]}),
        ("PATCH", f"/api/initiatives/{initiative['id']}", {"name": initiative["name"]}),
        ("POST", "/api/initiatives/reorder",
         {"ordered_ids": [x["id"] for x in s["initiatives"]]}),
        ("PUT", f"/api/initiatives/{initiative['id']}/demand",
         {"lines": [{"team_id": d["team_id"], "offset": d["offset"], "fte_h": d["fte_h"]}
                    for d in initiative["demand"]]}),
    ]:
        client.request(method, path, json=body)
        assert depth(client) == before, f"{method} {path} pushed a checkpoint"


def test_a_reset_of_an_empty_plan_is_not_a_step_either(client):
    client.post("/api/reset")
    after_first = depth(client)
    for _ in range(3):
        client.post("/api/reset")
    assert depth(client) == after_first


def test_a_real_change_is_still_a_step(client):
    before = depth(client)
    client.put("/api/settings", json={"horizon_months": 6})
    assert depth(client) == before + 1
    assert state(client)["undo"]["label"] == "Changed the settings."


def test_a_change_that_is_then_reverted_by_hand_leaves_both_steps(client):
    """Only the top checkpoint is compared, and only against the plan as it now
    stands, so a genuine there-and-back keeps its history."""
    before = depth(client)
    client.put("/api/settings", json={"horizon_months": 6})
    client.put("/api/settings", json={"horizon_months": 24})
    assert depth(client) == before + 2


# --- the editor's create hands back what it made --------------------------------


def test_create_returns_the_id_it_made(client):
    response = client.post("/api/initiatives", json={"name": "New", "start_month": "2027-06"})
    body = response.json()
    made = next(i for i in body["initiatives"] if i["name"] == "New")
    assert body["created_id"] == made["id"]


# --- the soak ----------------------------------------------------------------------


def test_the_invariants_survive_a_soak(client, tmp_path):
    """A hundred and fifty operations in a seeded random order, hostile ids included."""
    random.seed(11)
    for step in range(150):
        # Refreshed every few steps rather than every one: /api/state runs the
        # engine, and the stale ids in between are hostile input of their own.
        if step % 10 == 0:
            s = state(client)
            teams = [t["id"] for t in s["teams"]] or [1]
            initiatives = [i["id"] for i in s["initiatives"]] or [1]
        team = random.choice(teams + [99999, 2**70])
        initiative = random.choice(initiatives + [99999, 2**70])
        response = random.choice([
            lambda: client.post("/api/teams", json={"name": random.choice(["SOC", "soc ", "N", "  "])}),
            lambda: client.patch(f"/api/teams/{team}", json={"name": random.choice(["GRC", "Q", " "])}),
            lambda: client.put("/api/supply", json={"team_id": team, "from_month": "2027-01",
                                                    "to_month": "2027-03", "fte_h": 100}),
            lambda: client.patch(f"/api/initiatives/{initiative}",
                                 json={"notes": random.choice(["", "x", None])}),
            lambda: client.put(f"/api/initiatives/{initiative}/demand",
                               json={"lines": [{"team_id": team, "offset": 0, "fte_h": 50}]}),
            lambda: client.post("/api/initiatives/reorder",
                                json={"ordered_ids": random.sample(initiatives, len(initiatives))}),
            lambda: client.delete(f"/api/initiatives/{initiative}"),
            lambda: client.post("/api/initiatives", json={"name": "S", "start_month": "2027-02"}),
            lambda: client.post("/api/undo"),
            lambda: client.post("/api/seed"),
        ])()
        assert response.status_code < 500

    conn = db.connect()
    try:
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert conn.execute("SELECT COUNT(*) FROM team WHERE trim(name) = ''").fetchone()[0] == 0
        ranks = [r[0] for r in conn.execute('SELECT "rank" FROM initiative ORDER BY "rank"')]
        assert ranks == list(range(1, len(ranks) + 1))
        assert conn.execute("SELECT COUNT(*) FROM undo_snapshot").fetchone()[0] <= db.UNDO_DEPTH
    finally:
        conn.close()
