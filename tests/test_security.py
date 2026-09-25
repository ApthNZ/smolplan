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
        # The end is stored as a duration, but only end_month may set it:
        # that is the path that checks it against the start and the deadline.
        {"duration_m": 1},
        {"name = 'x' WHERE 1=1 --": "y"},
        {"archived); DROP TABLE demand;--": True},
    ):
        response = client.patch(f"/api/initiatives/{target}", json=payload)
        # Either pydantic drops the unknown key (leaving nothing to update) or
        # the allowlist rejects it. Neither may alter another row.
        assert response.status_code in (200, 400, 422)

    after = client.get("/api/state").json()
    assert [i["rank"] for i in after["initiatives"]] == [1, 2, 3]
    assert [i["length"] for i in after["initiatives"]] == [6, 3, 3]
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


# "2027-06\n" because `$` also matches before a final newline: re.match let
# it through, and it was stored with the newline in it.
BAD_MONTHS = ["2027-06'; DROP TABLE initiative;--", "2027-13", "not-a-month", "2027-6", " ", "2027-06\n"]


@pytest.mark.parametrize("field", ["end_month", "deadline_month"])
@pytest.mark.parametrize("month", BAD_MONTHS)
def test_initiative_month_fields_are_validated(client, field, month):
    """The end and the deadline are written to the database, so they are held
    to YYYY-MM before any SQL runs, on a create and on an edit alike."""
    before = client.get("/api/state").json()
    target = before["initiatives"][0]["id"]

    created = client.post(
        "/api/initiatives", json={"name": "X", "start_month": "2027-06", field: month}
    )
    patched = client.patch(f"/api/initiatives/{target}", json={field: month})
    assert created.status_code == 400
    assert patched.status_code == 400

    after = client.get("/api/state").json()
    assert after["initiatives"] == before["initiatives"]
    assert after["undo"]["depth"] == before["undo"]["depth"]


def test_a_trailing_newline_is_not_a_month_anywhere(client):
    """One validator holds every month the API takes, so the start and the
    clock are held to it too. A month stored with a newline breaks a CSV cell
    on export and never compares equal to the month it looks like."""
    before = client.get("/api/state").json()
    target = before["initiatives"][0]["id"]
    for response in (
        client.post("/api/initiatives", json={"name": "X", "start_month": "2027-06\n"}),
        client.patch(f"/api/initiatives/{target}", json={"start_month": "2027-06\n"}),
        client.put("/api/settings", json={"current_month": "2027-06\n"}),
    ):
        assert response.status_code == 400, response.json()

    after = client.get("/api/state").json()
    assert after["initiatives"] == before["initiatives"]
    assert after["settings"] == before["settings"]
    assert after["undo"]["depth"] == before["undo"]["depth"]


def test_an_imported_deadline_is_validated(client):
    before = client.get("/api/state").json()
    response = client.post(
        "/api/import",
        json={
            "csv": "InitiativeName,Reference,StartMonth,EndMonth,SOC,Deadline\n"
            "X,PRO-1,2027-02,2027-03,1,2027-06'); DROP TABLE initiative;--\n"
        },
    )
    assert response.status_code == 400
    assert client.get("/api/state").json()["initiatives"] == before["initiatives"]


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

    # 6 is inside the grid's reach but past the end of A, which runs six months.
    for offset in (-1, 6, 10_000):
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


# --- who may talk to it: Host, Origin, Sec-Fetch-Site (guard.py) --------------

# What a form on another site, or a `fetch(..., {mode: "no-cors"})` from one,
# looks like when it arrives. No preflight is sent for either, so these reach
# the app unless the app itself refuses them.
CROSS_SITE_FORM = {"Origin": "https://evil.example",
                   "Content-Type": "application/x-www-form-urlencoded"}
CROSS_SITE_FETCH = {"Sec-Fetch-Site": "cross-site", "Origin": "https://evil.example"}


def plan(client):
    """Everything a write could change, minus the clock-derived bits."""
    state = client.get("/api/state").json()
    return {k: state[k] for k in ("teams", "supply", "reserves", "initiatives", "undo")}


@pytest.mark.parametrize("headers", [CROSS_SITE_FORM, CROSS_SITE_FETCH,
                                     {"Sec-Fetch-Site": "same-site"},
                                     {"Origin": "null"},
                                     {"Origin": "http://testserver:9999"}])
@pytest.mark.parametrize("path", ["/api/reset", "/api/seed", "/api/undo"])
def test_a_cross_site_write_is_refused_and_changes_nothing(client, headers, path):
    """The body-less POSTs were the live hole. One reset, then enough seeds to
    push the real plan off the bottom of a fifty-deep undo stack, and the plan
    was gone for good — from any page the user happened to have open."""
    client.post("/api/teams", json={"name": "Red Team"})  # something to undo
    before = plan(client)
    assert client.post(path, headers=headers).status_code == 403
    assert plan(client) == before


def test_every_write_route_is_covered_not_just_the_body_less_ones(client):
    state = client.get("/api/state").json()
    team, initiative = state["teams"][0]["id"], state["initiatives"][0]["id"]
    before = plan(client)
    for method, path, body in [
        ("POST", "/api/teams", {"name": "X"}),
        ("PATCH", f"/api/teams/{team}", {"name": "X"}),
        ("DELETE", f"/api/teams/{team}", None),
        ("PUT", "/api/supply", {"team_id": team, "from_month": "2027-01",
                                "to_month": "2027-01", "fte_h": 1}),
        ("POST", "/api/initiatives", {"name": "X", "start_month": "2027-06"}),
        ("PATCH", f"/api/initiatives/{initiative}", {"name": "X"}),
        ("DELETE", f"/api/initiatives/{initiative}", None),
        ("DELETE", "/api/initiatives", None),
        ("PUT", "/api/settings", {"horizon_months": 6}),
        ("POST", "/api/import", {"csv": "x"}),
    ]:
        response = client.request(method, path, json=body, headers=CROSS_SITE_FETCH)
        assert response.status_code == 403, path
    assert plan(client) == before


def test_the_page_own_requests_still_work(client):
    """What a browser attaches to a fetch from the app's own page."""
    same_origin = {"Sec-Fetch-Site": "same-origin", "Origin": "http://testserver"}
    assert client.post("/api/teams", json={"name": "Red Team"},
                       headers=same_origin).status_code == 200
    assert client.post("/api/undo", headers=same_origin).status_code == 200
    # A browser too old to send Sec-Fetch-Site still sends a matching Origin.
    assert client.post("/api/seed", headers={"Origin": "http://testserver"}).status_code == 200


def test_scripts_without_browser_headers_still_work(client):
    """curl and scripts send neither header, and scripted access is intended."""
    assert client.post("/api/reset").status_code == 200
    assert client.post("/api/undo").status_code == 200


def test_reads_are_not_subject_to_the_origin_check(client):
    assert client.get("/api/state", headers=CROSS_SITE_FETCH).status_code == 200
    assert client.get("/api/export.csv", headers=CROSS_SITE_FETCH).status_code == 200


@pytest.mark.parametrize("path", ["/", "/health", "/api/state", "/api/export.csv",
                                  "/static/app.js"])
def test_an_unknown_host_is_refused_everywhere(client, path):
    """DNS rebinding: a page on attacker.example re-points its name at this
    machine, and the browser then treats the app as the attacker's own origin —
    able to read the whole plan. The Host header still says which name was used."""
    response = client.get(path, headers={"Host": "attacker.example"})
    assert response.status_code == 400
    assert "SOC" not in response.text


def test_the_host_allowlist_comes_from_the_environment(monkeypatch):
    import guard

    monkeypatch.delenv("SMOLPLAN_ALLOWED_HOSTS", raising=False)
    assert guard.allowed_hosts("SMOLPLAN_ALLOWED_HOSTS") == {"localhost", "127.0.0.1", "[::1]"}
    monkeypatch.setenv("SMOLPLAN_ALLOWED_HOSTS", "Planner.example:8107, [FD00::1] ,")
    assert guard.allowed_hosts("SMOLPLAN_ALLOWED_HOSTS") == {"planner.example", "[fd00::1]"}


def test_the_guard_is_the_same_file_in_both_apps():
    """smoltask carries the same guard.py. Only its first line would say so if
    the two were edited apart, so this pins the shape both depend on."""
    import guard

    assert guard.DEFAULT_HOSTS == ("localhost", "127.0.0.1", "[::1]")
    assert guard.host_only("[::1]:8107") == "[::1]"
    assert guard.TRUSTED_FETCH_SITES == {"same-origin", "none"}


# --- what a loaded page may do ------------------------------------------------


@pytest.mark.parametrize("path", ["/", "/static/app.js", "/api/state", "/health"])
def test_security_headers_are_on_every_response(client, path):
    headers = client.get(path).headers
    csp = headers["content-security-policy"]
    for directive in ("default-src 'self'", "frame-ancestors 'none'", "object-src 'none'",
                      "base-uri 'none'", "form-action 'self'"):
        assert directive in csp
    assert "unsafe-inline" not in csp and "unsafe-eval" not in csp
    assert headers["x-frame-options"] == "DENY"
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["referrer-policy"] == "no-referrer"


def test_refusals_and_errors_carry_the_headers_too(client):
    assert "content-security-policy" in client.get(
        "/", headers={"Host": "attacker.example"}).headers
    assert "content-security-policy" in client.post(
        "/api/reset", headers=CROSS_SITE_FETCH).headers
    assert "content-security-policy" in client.delete("/api/teams/999999").headers


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json"])
def test_the_api_docs_are_not_served(client, path):
    assert client.get(path).status_code == 404


# --- bounded text ---------------------------------------------------------------


@pytest.mark.parametrize("field,limit", [("owner", 120), ("notes", 10_000)])
def test_owner_and_notes_are_bounded_on_create_and_patch(client, field, limit):
    """Every write snapshots the whole plan, fifty deep, and every /api/state
    carries every note. Six 2 MB notes made a 12 MB database."""
    target = client.get("/api/state").json()["initiatives"][0]["id"]
    create = {"name": "Big", "start_month": "2027-06"}
    assert client.post("/api/initiatives", json={**create, field: "x" * (limit + 1)}).status_code == 422
    assert client.patch(f"/api/initiatives/{target}", json={field: "x" * (limit + 1)}).status_code == 422
    assert client.post("/api/initiatives", json={**create, field: "x" * limit}).status_code == 200
    assert client.patch(f"/api/initiatives/{target}", json={field: "x" * limit}).status_code == 200


# --- text that does something other than read -----------------------------------

SPOOF = "‮evil\x00\x1b[2J"


@pytest.mark.parametrize("route", ["team", "reserve", "initiative"])
def test_controls_and_bidi_overrides_are_stripped_from_names(client, route):
    """A bidi override reorders a name in a spreadsheet; an ANSI escape is live
    the moment `curl /api/export.csv` prints to a terminal."""
    if route == "initiative":
        state = client.post("/api/initiatives",
                            json={"name": f"Ops{SPOOF}", "start_month": "2027-06"}).json()
        names = [i["name"] for i in state["initiatives"]]
    else:
        state = client.post(f"/api/{route}s", json={"name": f"Ops{SPOOF}"}).json()
        names = [t["name"] for t in state[f"{route}s"]]
    assert "Opsevil[2J" in names
    assert not any(c in "".join(names) for c in "‮\x00\x1b")


def test_the_export_carries_no_controls(client):
    client.post("/api/initiatives", json={"name": f"Ops{SPOOF}", "start_month": "2027-06"})
    body = client.get("/api/export.csv").text
    assert not any(c in body for c in "‮\x00\x1b")


def test_owner_and_notes_are_cleaned_but_notes_keep_their_lines(client):
    target = client.get("/api/state").json()["initiatives"][0]
    state = client.patch(f"/api/initiatives/{target['id']}",
                         json={"owner": "Ana\x1b[31m‮", "notes": "one\r\ntwo\x00⁦"}).json()
    after = next(i for i in state["initiatives"] if i["id"] == target["id"])
    assert after["owner"] == "Ana[31m"
    assert after["notes"] == "one\ntwo"


def test_joined_emoji_survive_in_a_name(client):
    family = "\U0001F468‍\U0001F469‍\U0001F467"
    state = client.post("/api/teams", json={"name": f"Ops {family}"}).json()
    assert f"Ops {family}" in [t["name"] for t in state["teams"]]


def test_an_imported_name_is_cleaned_like_a_typed_one(client):
    # No NUL here: the csv module refuses a file containing one outright.
    csv = ("InitiativeName,Reference,StartMonth,EndMonth,SOC\n"
           "Ops\u202eevil\x1b[2J,REF\x1b-1,2027-06,2027-06,1\n")
    state = client.post("/api/import", json={"csv": csv}).json()
    imported = next(i for i in state["initiatives"] if i["reference"])
    assert imported["name"] == "Opsevil[2J"
    assert imported["reference"] == "REF-1"
