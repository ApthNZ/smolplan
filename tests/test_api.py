"""API tests. These run against a throwaway database seeded with the fixture."""

import sys
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
        "/api/initiatives", json={"name": "D", "start_month": "2027-02"}
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
