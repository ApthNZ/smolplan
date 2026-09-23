"""SQLite storage. One file, no migrations, no ORM."""

from __future__ import annotations

import json
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
    -- Months the initiative runs, start inclusive. A duration rather than an
    -- end month, so that moving the start carries the end with it, exactly as
    -- the demand profile's offsets already do.
    duration_m            INTEGER NOT NULL DEFAULT 1,
    deadline_month        TEXT,
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

-- Undo is a stack of whole-plan snapshots rather than a log of inverse
-- operations. A plan is a few hundred rows, so a snapshot costs almost
-- nothing, and "put it back exactly" needs no inverse written for deleting a
-- team (which cascades through supply, reserves and demand) or for an import
-- that touched forty initiatives at once.
CREATE TABLE IF NOT EXISTS undo_snapshot (
    id          INTEGER PRIMARY KEY,
    made_at     TEXT NOT NULL,
    label       TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    payload     TEXT NOT NULL
);
"""

DEFAULT_SETTINGS = {"horizon_months": "24", "current_month": "", "seeded": ""}

# How far back Ctrl+Z reaches. Fifty is well past "what did I just drag?" and
# still a trivial amount of storage for a plan this size.
UNDO_DEPTH = 50

# Every table the undo stack captures, ordered so that inserting them in this
# order never lands a child row before its parent. Deletes run in reverse.
# `undo_snapshot` is deliberately absent: undoing must not rewrite the history
# it is walking back through.
UNDO_TABLES = ("setting", "team", "supply", "reserve", "reserve_line", "initiative", "demand")


def connect(path: str | None = None) -> sqlite3.Connection:
    # check_same_thread=False: a connection is opened per request and handed
    # back at the end of it, but FastAPI runs a sync dependency's setup, the
    # endpoint and the teardown as three separate threadpool jobs, which are
    # not guaranteed to land on the same worker thread. They are strictly
    # sequential, so one connection is still only ever used by one thread at a
    # time — the default guard rejects the handover rather than a real race.
    conn = sqlite3.connect(path or DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    _drop_requested_start_month(conn)
    _add_reference(conn)
    _add_duration(conn)
    _add_deadline(conn)
    # Partial index: an initiative created by hand has no reference, and any
    # number of them may coexist. Imported ones are unique on it, which is what
    # lets a re-import update in place rather than duplicate.
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS initiative_reference ON initiative(reference) "
        "WHERE reference IS NOT NULL AND reference != ''"
    )
    for key, value in DEFAULT_SETTINGS.items():
        conn.execute("INSERT OR IGNORE INTO setting (key, value) VALUES (?, ?)", (key, value))
    _mark_existing_as_seeded(conn)
    _drop_stale_snapshots(conn)
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


def _mark_existing_as_seeded(conn: sqlite3.Connection) -> None:
    """Set the `seeded` flag on a database that predates it.

    The flag exists so that *Reset to zero* survives a restart: without it the
    startup hook reads an empty team table as a fresh install and puts the
    fixture straight back. A database that already has teams was seeded long
    ago, so say so — otherwise this flag's first act would be to declare every
    existing plan fresh and overwrite it.
    """
    row = conn.execute("SELECT value FROM setting WHERE key = 'seeded'").fetchone()
    if row is not None and row["value"]:
        return
    if conn.execute("SELECT 1 FROM team LIMIT 1").fetchone():
        set_setting(conn, "seeded", "1")


def was_seeded(conn: sqlite3.Connection) -> bool:
    """Has this database ever been populated? An empty plan is not a new one."""
    row = conn.execute("SELECT value FROM setting WHERE key = 'seeded'").fetchone()
    return bool(row and row["value"])


def _add_reference(conn: sqlite3.Connection) -> None:
    """Add the reference column to a database created before CSV import existed."""
    columns = [r["name"] for r in conn.execute("PRAGMA table_info(initiative)")]
    if "reference" not in columns:
        conn.execute("ALTER TABLE initiative ADD COLUMN reference TEXT")
        conn.commit()


def _add_duration(conn: sqlite3.Connection) -> None:
    """Add the end of an initiative to a database created before it was stored.

    Until then the end was whatever the demand implied, so that is what each
    existing initiative keeps: its last offset plus one, or a single month
    when it has no demand at all. Nothing that was on screen moves.
    """
    columns = [r["name"] for r in conn.execute("PRAGMA table_info(initiative)")]
    if "duration_m" not in columns:
        conn.execute("ALTER TABLE initiative ADD COLUMN duration_m INTEGER NOT NULL DEFAULT 1")
        conn.execute(
            "UPDATE initiative SET duration_m = COALESCE("
            "(SELECT MAX(offset_m) + 1 FROM demand WHERE demand.initiative_id = initiative.id), 1)"
        )
        conn.commit()


def _add_deadline(conn: sqlite3.Connection) -> None:
    """Add the deadline column to a database created before deadlines existed.

    NULL is "no deadline", which is what every existing initiative had.
    """
    columns = [r["name"] for r in conn.execute("PRAGMA table_info(initiative)")]
    if "deadline_month" not in columns:
        conn.execute("ALTER TABLE initiative ADD COLUMN deadline_month TEXT")
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

    # duration_m becomes `duration`, as offset_m becomes `offset` below: the
    # engine's names, not the table's.
    initiatives = [
        dict(r)
        for r in conn.execute(
            'SELECT id, name, "rank", start_month, duration_m AS duration, '
            "deadline_month, reference, owner, notes, archived "
            'FROM initiative ORDER BY "rank"'
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


# --- undo --------------------------------------------------------------------

# A snapshot is only restorable by the schema that wrote it. Rather than parse
# fifty payloads at startup to find out, each one carries the shape it was
# taken with, and anything that no longer matches is dropped in one statement.
def _fingerprint(conn) -> str:
    shape = {
        table: [r["name"] for r in conn.execute(f"PRAGMA table_info({table})")]
        for table in UNDO_TABLES
    }
    return json.dumps(shape, sort_keys=True, separators=(",", ":"))


def _drop_stale_snapshots(conn) -> None:
    """Discard snapshots this schema could no longer put back.

    A migration that adds or removes a column makes older payloads unusable:
    the INSERT would fail halfway through a restore, which is far worse than
    the undo simply not reaching that far back.
    """
    conn.execute("DELETE FROM undo_snapshot WHERE fingerprint != ?", (_fingerprint(conn),))


def snapshot(conn) -> dict:
    """Every row of every plan table, as {table: {columns, rows}}."""
    data = {}
    for table in UNDO_TABLES:
        cursor = conn.execute(f"SELECT * FROM {table}")
        data[table] = {
            "columns": [d[0] for d in cursor.description],
            "rows": [list(row) for row in cursor.fetchall()],
        }
    return data


def checkpoint(conn, label: str, amend: bool = False) -> None:
    """Record the state *before* a change, so Ctrl+Z can put it back.

    Called at the top of every handler that writes. Nothing commits here: the
    snapshot rides the same transaction as the change it precedes, so a request
    that fails validation and raises leaves no checkpoint behind.

    `amend` folds a write into the checkpoint already on top of the stack
    instead of pushing another. The editor saves an initiative as a create or
    patch *and* a demand replacement, two requests for one button — without
    this, undoing that button would take two presses.
    """
    if amend and conn.execute("SELECT 1 FROM undo_snapshot LIMIT 1").fetchone():
        return
    payload = json.dumps(snapshot(conn), separators=(",", ":"))
    conn.execute(
        "INSERT INTO undo_snapshot (made_at, label, fingerprint, payload) VALUES (?, ?, ?, ?)",
        (now(), label, _fingerprint(conn), payload),
    )
    # Keep the newest UNDO_DEPTH and drop the tail.
    conn.execute(
        "DELETE FROM undo_snapshot WHERE id NOT IN "
        "(SELECT id FROM undo_snapshot ORDER BY id DESC LIMIT ?)",
        (UNDO_DEPTH,),
    )


def undo_state(conn) -> dict:
    """What the Undo button needs to render: how far back it reaches, and into what."""
    row = conn.execute(
        "SELECT label FROM undo_snapshot ORDER BY id DESC LIMIT 1"
    ).fetchone()
    depth = conn.execute("SELECT COUNT(*) AS n FROM undo_snapshot").fetchone()["n"]
    return {"depth": depth, "label": row["label"] if row else None}


def undo(conn) -> str | None:
    """Restore the most recent snapshot. Returns its label, or None if empty."""
    row = conn.execute(
        "SELECT id, label, payload FROM undo_snapshot ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return None

    data = json.loads(row["payload"])
    # Children first, so no delete is refused by a foreign key still pointing
    # at the row being removed.
    for table in reversed(UNDO_TABLES):
        conn.execute(f"DELETE FROM {table}")
    for table in UNDO_TABLES:
        block = data.get(table)
        if not block or not block["rows"]:
            continue
        # Quoted because one of these columns is "rank", which is a keyword.
        # The names come from the schema, never from a request.
        columns = ", ".join(f'"{c}"' for c in block["columns"])
        marks = ", ".join("?" for _ in block["columns"])
        conn.executemany(
            f"INSERT INTO {table} ({columns}) VALUES ({marks})", block["rows"]
        )

    conn.execute("DELETE FROM undo_snapshot WHERE id = ?", (row["id"],))
    conn.commit()
    return row["label"]


# --- reset -------------------------------------------------------------------


def reset_to_empty(conn) -> None:
    """Delete every team, reserve and initiative. Destructive.

    Settings are deliberately left alone: the horizon and the clock override
    are how you are looking at a plan, not part of one, and re-typing them
    after every reset would be a chore rather than a fresh start.

    The `seeded` flag stays set, which is the whole point — an empty database
    that has been seeded before is a deliberate blank page, and the startup
    hook must leave it alone rather than helpfully restoring the fixture.
    """
    for table in ("demand", "initiative", "reserve_line", "reserve", "supply", "team"):
        conn.execute(f"DELETE FROM {table}")
    set_setting(conn, "seeded", "1")
    conn.commit()


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
        ("A", 1, anchor, 6, [("SOC", range(3), 100), ("GRC", range(6), 50)]),
        ("B", 2, month_add(anchor, 1), 3, [("GRC", range(3), 50)]),
        ("C", 3, month_add(anchor, 3), 3, [("SOC", range(3), 50)]),
    ]
    for name, rank, start, duration, profile in plan:
        initiative_id = conn.execute(
            'INSERT INTO initiative (name, "rank", start_month, duration_m, '
            "owner, notes, archived, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, '', '', 0, ?, ?)",
            (name, rank, start, duration, stamp, stamp),
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
    set_setting(conn, "seeded", "1")
    conn.commit()
