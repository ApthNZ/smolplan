"""Security tests.

smolplan has no authentication by design — it is a LAN-only planning toy, and
the threat model is "someone fat-fingers a URL", not "an attacker has network
access". These tests therefore cover what the app can still get wrong on its
own: injection through the fields it does accept, traversal out of the static
directory, and secrets committed to the tree.

See SECURITY_STATUS.md for the posture these tests defend.
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from fastapi.testclient import TestClient

import db

ROOT = Path(__file__).resolve().parents[1]
SOURCE_FILES = [
    p
    for p in list(ROOT.glob("*.py")) + list(ROOT.glob("static/*.js")) + list(ROOT.glob("tests/*.py"))
    if p.name != "test_security.py"  # this file quotes the patterns it hunts for
]


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


# --- secrets -----------------------------------------------------------------


def test_no_hardcoded_secrets():
    patterns = [
        r'api[_-]?key\s*=\s*["\'][^"\']+["\']',
        r'password\s*=\s*["\'][^"\']+["\']',
        r'secret\s*=\s*["\'][^"\']+["\']',
        r'token\s*=\s*["\'][^"\']+["\']',
        r"webhook.*discord\.com",
        r"postgresql://.*:.*@",
        r"gh[pousr]_[A-Za-z0-9]{16,}",
    ]
    for path in SOURCE_FILES:
        text = path.read_text()
        for pattern in patterns:
            assert not re.search(pattern, text, re.IGNORECASE), f"possible secret in {path.name}"


@pytest.mark.skipif(
    not (ROOT / ".gitignore").exists(),
    reason="repo-only check; the container image ships code, not the working tree",
)
def test_database_file_is_not_tracked():
    ignored = (ROOT / ".gitignore").read_text()
    assert "*.db" in ignored
    assert "data/" in ignored


# --- SQL injection -----------------------------------------------------------

INJECTION = "Robert'); DROP TABLE initiative;--"


def test_initiative_name_is_data_not_sql(client):
    state = client.post(
        "/api/initiatives", json={"name": INJECTION, "start_month": "2027-06"}
    ).json()
    names = [i["name"] for i in state["initiatives"]]
    assert INJECTION in names
    # The table is still there, with the originals in it.
    assert {"A", "B", "C"} <= set(names)


def test_team_name_is_data_not_sql(client):
    state = client.post("/api/teams", json={"name": INJECTION}).json()
    assert INJECTION in [t["name"] for t in state["teams"]]
    assert len(state["teams"]) == 3


def test_patch_rejects_unknown_columns(client):
    """update_initiative builds `SET {key} = ?` from the payload keys, so the
    allowlist is what stops a crafted field name reaching the SQL text."""
    state = client.get("/api/state").json()
    target = state["initiatives"][0]["id"]

    for payload in (
        {"rank": 99},
        {"id": 1},
        {"name = 'x' WHERE 1=1 --": "y"},
        {"archived); DROP TABLE demand;--": True},
    ):
        response = client.patch(f"/api/initiatives/{target}", json=payload)
        # Either pydantic drops the unknown key (leaving nothing to update) or
        # the allowlist rejects it. Neither may alter another row.
        assert response.status_code in (200, 400, 422)

    after = client.get("/api/state").json()
    assert [i["rank"] for i in after["initiatives"]] == [1, 2, 3]
    assert sum(len(i["demand"]) for i in after["initiatives"]) == 15


def test_month_strings_reaching_sql_are_validated(client):
    state = client.get("/api/state").json()
    team = state["teams"][0]["id"]
    response = client.put(
        "/api/supply",
        json={
            "team_id": team,
            "from_month": "2027-01'; DROP TABLE supply;--",
            "to_month": "2027-01",
            "fte_h": 100,
        },
    )
    assert response.status_code == 400
    assert client.get("/api/state").json()["supply"]  # table intact


# --- input validation --------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        {"team_id": 1, "from_month": "2027-13", "to_month": "2027-13", "fte_h": 100},
        {"team_id": 1, "from_month": "not-a-month", "to_month": "2027-01", "fte_h": 100},
        {"team_id": 1, "from_month": "2027-01", "to_month": "2027-01", "fte_h": -5},
        {"team_id": 1, "from_month": "2027-01", "to_month": "2027-01", "fte_h": 10 ** 9},
        {"team_id": 1, "from_month": "2027-12", "to_month": "2027-01", "fte_h": 100},
        {"team_id": 1, "from_month": "2027-01", "to_month": "2199-01", "fte_h": 100},
    ],
)
def test_supply_input_is_validated(client, payload):
    state = client.get("/api/state").json()
    payload = {**payload, "team_id": state["teams"][0]["id"]}
    assert client.put("/api/supply", json=payload).status_code in (400, 422)


def test_demand_offsets_are_bounded(client):
    state = client.get("/api/state").json()
    initiative = state["initiatives"][0]["id"]
    team = state["teams"][0]["id"]

    for offset in (-1, 10_000):
        response = client.put(
            f"/api/initiatives/{initiative}/demand",
            json={"lines": [{"team_id": team, "offset": offset, "fte_h": 50}]},
        )
        assert response.status_code in (400, 422)


def test_oversized_names_are_rejected(client):
    response = client.post(
        "/api/initiatives", json={"name": "x" * 5000, "start_month": "2027-06"}
    )
    assert response.status_code == 422


# --- path traversal ----------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "/static/../app.py",
        "/static/../../etc/passwd",
        "/static/%2e%2e%2fapp.py",
        "/static/..%2fdb.py",
        "/static/....//app.py",
    ],
)
def test_static_mount_does_not_escape(client, path):
    response = client.get(path)
    assert response.status_code != 200 or "SMOLPLAN_DB" not in response.text


def test_static_serves_only_what_it_should(client):
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/static/favicon.svg").status_code == 200
    assert client.get("/static/nonexistent.js").status_code == 404


# --- surface -----------------------------------------------------------------


def test_no_route_exposes_the_database_path(client):
    """SMOLPLAN_DB is deployment configuration, never user input."""
    body = client.get("/api/state").text
    assert db.DB_PATH not in body
    assert "SMOLPLAN_DB" not in body


def test_health_leaks_nothing(client):
    body = client.get("/health").json()
    assert set(body) == {"status", "teams"}
