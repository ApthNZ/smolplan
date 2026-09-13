"""SQLite storage. One file, no migrations, no ORM."""

from __future__ import annotations

import os
import sqlite3
from datetime import date, datetime, timezone

DB_PATH = os.environ.get("SMOLPLAN_DB", os.path.join(os.path.dirname(__file__), "smolplan.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS team (
    id   INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS supply (
    team_id INTEGER NOT NULL REFERENCES team(id) ON DELETE CASCADE,
    month   TEXT NOT NULL,
    fte_h   INTEGER NOT NULL,
    PRIMARY KEY (team_id, month)
);

CREATE TABLE IF NOT EXISTS reserve (
    id         INTEGER PRIMARY KEY,
    name       TEXT NOT NULL,
    sort_order INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS reserve_line (
    reserve_id INTEGER NOT NULL REFERENCES reserve(id) ON DELETE CASCADE,
    team_id    INTEGER NOT NULL REFERENCES team(id) ON DELETE CASCADE,
    month      TEXT NOT NULL,
    fte_h      INTEGER NOT NULL,
    PRIMARY KEY (reserve_id, team_id, month)
);

CREATE TABLE IF NOT EXISTS initiative (
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

CREATE TABLE IF NOT EXISTS demand (
    initiative_id INTEGER NOT NULL REFERENCES initiative(id) ON DELETE CASCADE,
    team_id       INTEGER NOT NULL REFERENCES team(id) ON DELETE CASCADE,
    offset_m      INTEGER NOT NULL,
    fte_h         INTEGER NOT NULL,
    PRIMARY KEY (initiative_id, team_id, offset_m)
);

CREATE TABLE IF NOT EXISTS setting (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""

DEFAULT_SETTINGS = {"horizon_months": "24", "current_month": ""}


def connect(path: str | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(path or DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    _drop_requested_start_month(conn)
    _add_reference(conn)
    # Partial index: an initiative created by hand has no reference, and any
    # number of them may coexist. Imported ones are unique on it, which is what
    # lets a re-import update in place rather than duplicate.
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS initiative_reference ON initiative(reference) "
        "WHERE reference IS NOT NULL AND reference != ''"
    )
    for key, value in DEFAULT_SETTINGS.items():
        conn.execute("INSERT OR IGNORE INTO setting (key, value) VALUES (?, ?)", (key, value))
    conn.commit()


def _drop_requested_start_month(conn: sqlite3.Connection) -> None:
    """Drop the retired requested_start_month column from an existing database.

    It implied a requested_finish_month, and a negotiation between a requester
    and a planner, which this is not. Idempotent: new databases never have it.
    """
    columns = [r["name"] for r in conn.execute("PRAGMA table_info(initiative)")]
    if "requested_start_month" in columns:
        conn.execute("ALTER TABLE initiative DROP COLUMN requested_start_month")
        conn.commit()


def _add_reference(conn: sqlite3.Connection) -> None:
    """Add the reference column to a database created before CSV import existed."""
    columns = [r["name"] for r in conn.execute("PRAGMA table_info(initiative)")]
    if "reference" not in columns:
        conn.execute("ALTER TABLE initiative ADD COLUMN reference TEXT")
        conn.commit()


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --- reads -------------------------------------------------------------------


def get_settings(conn) -> dict:
    rows = conn.execute("SELECT key, value FROM setting").fetchall()
    values = {r["key"]: r["value"] for r in rows}
    return {
        "horizon_months": int(values.get("horizon_months") or 24),
        "current_month": values.get("current_month") or "",
    }


def set_setting(conn, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO setting (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def load_engine_inputs(conn) -> dict:
    """Everything the engine needs, in the shapes it expects."""
    teams = [dict(r) for r in conn.execute("SELECT id, name FROM team ORDER BY name")]

    supply = {
        (r["team_id"], r["month"]): r["fte_h"]
        for r in conn.execute("SELECT team_id, month, fte_h FROM supply")
    }

    reserves = [
        {
            "reserve_id": r["reserve_id"],
            "name": r["name"],
            "team_id": r["team_id"],
            "month": r["month"],
            "fte_h": r["fte_h"],
        }
        for r in conn.execute(
            "SELECT l.reserve_id, r.name, l.team_id, l.month, l.fte_h "
            "FROM reserve_line l JOIN reserve r ON r.id = l.reserve_id"
        )
    ]

    initiatives = [
        dict(r)
        for r in conn.execute(
            'SELECT id, name, "rank", start_month, reference, owner, '
            "notes, archived FROM initiative ORDER BY \"rank\""
        )
    ]
    for initiative in initiatives:
        initiative["archived"] = bool(initiative["archived"])

    demand = [
        {
            "initiative_id": r["initiative_id"],
            "team_id": r["team_id"],
            "offset": r["offset_m"],
            "fte_h": r["fte_h"],
        }
        for r in conn.execute(
            "SELECT initiative_id, team_id, offset_m, fte_h FROM demand ORDER BY offset_m"
        )
    ]

    return {
        "teams": teams,
        "supply": supply,
        "reserves": reserves,
        "initiatives": initiatives,
        "demand": demand,
        "reserve_names": [
            dict(r)
            for r in conn.execute("SELECT id, name FROM reserve ORDER BY sort_order, id")
        ],
    }


# --- ranks -------------------------------------------------------------------


def renumber_ranks(conn, ordered_ids: list[int]) -> None:
    """Rewrite ranks as 1..n.

    Ranks carry no UNIQUE constraint precisely so this can be a plain sweep:
    SQLite checks uniqueness per row, not per statement, so an incremental
    shift would trip over itself halfway through.
    """
    for position, initiative_id in enumerate(ordered_ids, start=1):
        conn.execute(
            'UPDATE initiative SET "rank" = ?, updated_at = ? WHERE id = ?',
            (position, now(), initiative_id),
        )


def next_rank(conn) -> int:
    row = conn.execute('SELECT COALESCE(MAX("rank"), 0) AS m FROM initiative').fetchone()
    return row["m"] + 1


# --- seed --------------------------------------------------------------------


def seed_fixture(conn, anchor: str | None = None) -> None:
    """Reset to the spec's section 10 fixture. Destructive.

    The fixture is anchored at `anchor`, defaulting to the real current month,
    so a fresh install opens on today rather than on whichever month the spec
    happened to be written for. Passing an explicit anchor also pins the clock
    there, which is what the tests want; the default leaves the clock real.
    """
    from engine import month_add, month_span

    pinned = anchor is not None
    anchor = anchor or date.today().strftime("%Y-%m")

    for table in ("demand", "initiative", "reserve_line", "reserve", "supply", "team"):
        conn.execute(f"DELETE FROM {table}")

    team_ids = {}
    for name in ("SOC", "GRC"):
        cur = conn.execute("INSERT INTO team (name) VALUES (?)", (name,))
        team_ids[name] = cur.lastrowid

    months = month_span(anchor, month_add(anchor, 23))
    for month in months:
        conn.execute(
            "INSERT INTO supply (team_id, month, fte_h) VALUES (?, ?, ?)",
            (team_ids["SOC"], month, 200),
        )
        conn.execute(
            "INSERT INTO supply (team_id, month, fte_h) VALUES (?, ?, ?)",
            (team_ids["GRC"], month, 150),
        )

    bau = conn.execute(
        "INSERT INTO reserve (name, sort_order) VALUES ('BAU', 1)"
    ).lastrowid
    unplanned = conn.execute(
        "INSERT INTO reserve (name, sort_order) VALUES ('Unplanned', 2)"
    ).lastrowid
    for month in months:
        conn.execute(
            "INSERT INTO reserve_line (reserve_id, team_id, month, fte_h) VALUES (?, ?, ?, ?)",
            (bau, team_ids["SOC"], month, 100),
        )
        conn.execute(
            "INSERT INTO reserve_line (reserve_id, team_id, month, fte_h) VALUES (?, ?, ?, ?)",
            (bau, team_ids["GRC"], month, 50),
        )
        conn.execute(
            "INSERT INTO reserve_line (reserve_id, team_id, month, fte_h) VALUES (?, ?, ?, ?)",
            (unplanned, team_ids["GRC"], month, 25),
        )

    stamp = now()
    plan = [
        ("A", 1, anchor, [("SOC", range(3), 100), ("GRC", range(6), 50)]),
        ("B", 2, month_add(anchor, 1), [("GRC", range(3), 50)]),
        ("C", 3, month_add(anchor, 3), [("SOC", range(3), 50)]),
    ]
    for name, rank, start, profile in plan:
        initiative_id = conn.execute(
            'INSERT INTO initiative (name, "rank", start_month, '
            "owner, notes, archived, created_at, updated_at) "
            "VALUES (?, ?, ?, '', '', 0, ?, ?)",
            (name, rank, start, stamp, stamp),
        ).lastrowid
        for team, offsets, fte in profile:
            for offset in offsets:
                conn.execute(
                    "INSERT INTO demand (initiative_id, team_id, offset_m, fte_h) "
                    "VALUES (?, ?, ?, ?)",
                    (initiative_id, team_ids[team], offset, fte),
                )

    set_setting(conn, "current_month", anchor if pinned else "")
    set_setting(conn, "horizon_months", "24")
    conn.commit()
