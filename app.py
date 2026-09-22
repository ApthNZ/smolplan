"""smolplan: a small capacity planner.

No auth, no users, no audit trail. It is meant to run on a laptop, or on a
private network behind whatever already guards that network. See SECURITY.md.
"""

from __future__ import annotations

import csv
import io
import os
from datetime import date

from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import db
import engine
import importer

MAX_FTE_H = 10000  # 100.00 FTE
MAX_OFFSET = 119  # ten years of profile

HERE = os.path.dirname(os.path.abspath(__file__))

# --- plumbing ----------------------------------------------------------------


def get_conn():
    conn = db.connect()
    try:
        yield conn
    finally:
        conn.close()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    conn = db.connect()
    try:
        db.init_db(conn)
        # `was_seeded`, not "are there any teams": *Reset to zero* leaves a
        # deliberately empty database behind, and reading that as a fresh
        # install would put the fixture back on the next restart — which, with
        # the deploy-on-commit hook, is often within the minute.
        if not db.was_seeded(conn):
            db.seed_fixture(conn)
    finally:
        conn.close()
    yield


app = FastAPI(title="smolplan", lifespan=lifespan)


def bad(message: str):
    raise HTTPException(status_code=400, detail=message)


def today_month() -> str:
    return date.today().strftime("%Y-%m")


def current_month(conn) -> str:
    """The clock, injectable: an empty override means use the real one."""
    return db.get_settings(conn)["current_month"] or today_month()


# --- validation --------------------------------------------------------------


def validate_month(month: str) -> str:
    if not engine.MONTH_RE.match(month or ""):
        raise ValueError(f"Not a month: {month!r}. Expected YYYY-MM.")
    return month


def validate_start_month(month: str, current_month: str) -> str:
    """R8: no starts in the past."""
    validate_month(month)
    if engine.month_index(month) < engine.month_index(current_month):
        raise ValueError(f"Start month {month} is before the current month {current_month}.")
    return month


def validate_fte(fte_h: int) -> int:
    if not isinstance(fte_h, int) or fte_h < 0 or fte_h > MAX_FTE_H:
        raise ValueError(f"FTE must be between 0 and {MAX_FTE_H / 100:.2f}.")
    return fte_h


def validate_span(from_month: str, to_month: str) -> list[str]:
    validate_month(from_month)
    validate_month(to_month)
    if engine.month_index(to_month) < engine.month_index(from_month):
        raise ValueError("The last month is before the first month.")
    months = engine.month_span(from_month, to_month)
    if len(months) > 240:
        raise ValueError("That span is longer than twenty years.")
    return months


def guarded(fn, *args):
    try:
        return fn(*args)
    except ValueError as exc:
        bad(str(exc))


# --- undo labels -------------------------------------------------------------

# Every checkpoint carries a sentence saying what it is about to undo, because
# "Undo" on its own is a question rather than an offer — after three drags and
# a supply edit, the only useful thing a button can say is which one it will
# take back. The names are read before the change, so a delete can still say
# what it deleted.


def _name_of(conn, table: str, row_id: int, fallback: str) -> str:
    # `table` is a literal from the call sites below, never from a request.
    row = conn.execute(f"SELECT name FROM {table} WHERE id = ?", (row_id,)).fetchone()
    return row["name"] if row else fallback


def _team_name(conn, team_id: int) -> str:
    return _name_of(conn, "team", team_id, "a team")


def _reserve_name(conn, reserve_id: int) -> str:
    return _name_of(conn, "reserve", reserve_id, "a reserve")


def _initiative_name(conn, initiative_id: int) -> str:
    return _name_of(conn, "initiative", initiative_id, "an initiative")


def _fill_label(what: str, conn, payload, months: list[str]) -> str:
    team = _team_name(conn, payload.team_id)
    span = months[0] if len(months) == 1 else f"{months[0]} to {months[-1]}"
    if payload.fte_h is None:
        return f"Cleared {what} for {team}, {span}."
    return f"Set {what} for {team} to {payload.fte_h / 100:.2f} FTE, {span}."


def _patch_label(row, fields: dict) -> str:
    """Name the one change that matters, in the order a user would notice it.

    A drag sends only start_month; the editor's Save sends the lot, so the
    checks are ordered by which is worth reporting rather than by which
    arrived.
    """
    if "start_month" in fields and fields["start_month"] != row["start_month"]:
        return f"Moved {row['name']} to {fields['start_month']}."
    if "archived" in fields and bool(fields["archived"]) != bool(row["archived"]):
        return f"{'Archived' if fields['archived'] else 'Restored'} {row['name']}."
    if "name" in fields and fields["name"].strip() != row["name"]:
        return f"Renamed {row['name']} to {fields['name'].strip()}."
    return f"Edited {row['name']}."


def _reorder_label(conn, ordered_ids: list[int]) -> str:
    rows = conn.execute('SELECT id, name FROM initiative ORDER BY "rank"').fetchall()
    before = [r["id"] for r in rows]
    if before == ordered_ids:
        return "Re-ranked the portfolio."
    # A drag moves one row; the one that travelled furthest is the one dragged.
    names = {r["id"]: r["name"] for r in rows}
    moved = max(ordered_ids, key=lambda i: abs(ordered_ids.index(i) - before.index(i)))
    return f"Moved {names[moved]} to rank {ordered_ids.index(moved) + 1}."


# --- state -------------------------------------------------------------------


def build_state(conn) -> dict:
    data = db.load_engine_inputs(conn)
    settings = db.get_settings(conn)
    current = settings["current_month"] or today_month()
    horizon = settings["horizon_months"]

    result = engine.allocate(
        teams=[t["id"] for t in data["teams"]],
        supply=data["supply"],
        reserves=data["reserves"],
        initiatives=data["initiatives"],
        demand=data["demand"],
        current_month=current,
        horizon_months=horizon,
    )

    demand_by_initiative = {}
    for line in data["demand"]:
        demand_by_initiative.setdefault(line["initiative_id"], []).append(
            {"team_id": line["team_id"], "offset": line["offset"], "fte_h": line["fte_h"]}
        )

    initiatives = []
    for initiative in data["initiatives"]:
        outcome = result["initiatives"][initiative["id"]]
        initiatives.append(
            {
                **initiative,
                "demand": sorted(
                    demand_by_initiative.get(initiative["id"], []),
                    key=lambda d: (d["team_id"], d["offset"]),
                ),
                "status": outcome["status"],
                "shortfalls": outcome["shortfalls"],
                "length": max(
                    (d["offset"] for d in demand_by_initiative.get(initiative["id"], [])),
                    default=0,
                )
                + 1,
            }
        )

    supply = {}
    for (team_id, month), fte_h in data["supply"].items():
        supply.setdefault(str(team_id), {})[month] = fte_h

    reserves = []
    for reserve in data["reserve_names"]:
        lines = {}
        for line in data["reserves"]:
            if line["reserve_id"] == reserve["id"]:
                lines.setdefault(str(line["team_id"]), {})[line["month"]] = line["fte_h"]
        reserves.append({**reserve, "lines": lines})

    cells = {
        f"{team_id}|{month}": cell for (team_id, month), cell in result["cells"].items()
    }

    # Display range: the horizon. Evaluation may reach past it, so months
    # outside the display window still exist in cells.
    display_months = [engine.month_add(current, n) for n in range(horizon)]

    return {
        "teams": data["teams"],
        "supply": supply,
        "reserves": reserves,
        "initiatives": initiatives,
        "cells": cells,
        "months": display_months,
        "undo": db.undo_state(conn),
        "settings": {
            "horizon_months": horizon,
            "current_month": current,
            "current_month_override": bool(settings["current_month"]),
            "today_month": today_month(),
        },
    }


@app.get("/health")
def health(conn=Depends(get_conn)):
    """Touches the database, so a container with an unreadable or unwritable
    volume fails the check instead of reporting green on a broken app."""
    teams = conn.execute("SELECT COUNT(*) AS n FROM team").fetchone()["n"]
    return {"status": "ok", "teams": teams}


@app.get("/api/state")
def read_state(conn=Depends(get_conn)):
    return build_state(conn)


# --- teams -------------------------------------------------------------------


class TeamIn(BaseModel):
    name: str = Field(min_length=1, max_length=60)


@app.post("/api/teams")
def create_team(payload: TeamIn, conn=Depends(get_conn)):
    exists = conn.execute("SELECT 1 FROM team WHERE name = ?", (payload.name,)).fetchone()
    if exists:
        bad(f"There is already a team called {payload.name}.")
    db.checkpoint(conn, f"Added the team {payload.name.strip()}.")
    conn.execute("INSERT INTO team (name) VALUES (?)", (payload.name.strip(),))
    conn.commit()
    return build_state(conn)


@app.patch("/api/teams/{team_id}")
def rename_team(team_id: int, payload: TeamIn, conn=Depends(get_conn)):
    db.checkpoint(conn, f"Renamed a team to {payload.name.strip()}.")
    conn.execute("UPDATE team SET name = ? WHERE id = ?", (payload.name.strip(), team_id))
    conn.commit()
    return build_state(conn)


@app.delete("/api/teams/{team_id}")
def delete_team(team_id: int, conn=Depends(get_conn)):
    db.checkpoint(conn, f"Deleted the team {_team_name(conn, team_id)}.")
    conn.execute("DELETE FROM team WHERE id = ?", (team_id,))
    conn.commit()
    return build_state(conn)


# --- supply and reserves -----------------------------------------------------


class FillIn(BaseModel):
    team_id: int
    from_month: str
    to_month: str
    fte_h: int | None = None  # null clears the rows, which means "no supply data"


@app.put("/api/supply")
def fill_supply(payload: FillIn, conn=Depends(get_conn)):
    months = guarded(validate_span, payload.from_month, payload.to_month)
    if payload.fte_h is not None:
        guarded(validate_fte, payload.fte_h)
    db.checkpoint(conn, _fill_label("supply", conn, payload, months))
    for month in months:
        if payload.fte_h is None:
            conn.execute(
                "DELETE FROM supply WHERE team_id = ? AND month = ?", (payload.team_id, month)
            )
        else:
            conn.execute(
                "INSERT INTO supply (team_id, month, fte_h) VALUES (?, ?, ?) "
                "ON CONFLICT(team_id, month) DO UPDATE SET fte_h = excluded.fte_h",
                (payload.team_id, month, payload.fte_h),
            )
    conn.commit()
    return build_state(conn)


class ReserveIn(BaseModel):
    name: str = Field(min_length=1, max_length=60)


@app.post("/api/reserves")
def create_reserve(payload: ReserveIn, conn=Depends(get_conn)):
    db.checkpoint(conn, f"Added the reserve {payload.name.strip()}.")
    order = conn.execute(
        "SELECT COALESCE(MAX(sort_order), 0) + 1 AS n FROM reserve"
    ).fetchone()["n"]
    conn.execute(
        "INSERT INTO reserve (name, sort_order) VALUES (?, ?)", (payload.name.strip(), order)
    )
    conn.commit()
    return build_state(conn)


@app.delete("/api/reserves/{reserve_id}")
def delete_reserve(reserve_id: int, conn=Depends(get_conn)):
    db.checkpoint(conn, f"Deleted the reserve {_reserve_name(conn, reserve_id)}.")
    conn.execute("DELETE FROM reserve WHERE id = ?", (reserve_id,))
    conn.commit()
    return build_state(conn)


@app.put("/api/reserves/{reserve_id}/lines")
def fill_reserve(reserve_id: int, payload: FillIn, conn=Depends(get_conn)):
    months = guarded(validate_span, payload.from_month, payload.to_month)
    if payload.fte_h:
        guarded(validate_fte, payload.fte_h)
    db.checkpoint(
        conn, _fill_label(_reserve_name(conn, reserve_id), conn, payload, months)
    )
    for month in months:
        if payload.fte_h is None or payload.fte_h == 0:
            conn.execute(
                "DELETE FROM reserve_line WHERE reserve_id = ? AND team_id = ? AND month = ?",
                (reserve_id, payload.team_id, month),
            )
        else:
            conn.execute(
                "INSERT INTO reserve_line (reserve_id, team_id, month, fte_h) "
                "VALUES (?, ?, ?, ?) ON CONFLICT(reserve_id, team_id, month) "
                "DO UPDATE SET fte_h = excluded.fte_h",
                (reserve_id, payload.team_id, month, payload.fte_h),
            )
    conn.commit()
    return build_state(conn)


# --- initiatives -------------------------------------------------------------


class InitiativeIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    start_month: str
    owner: str = ""
    notes: str = ""


class InitiativePatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    start_month: str | None = None
    owner: str | None = None
    notes: str | None = None
    archived: bool | None = None


@app.post("/api/initiatives")
def create_initiative(payload: InitiativeIn, conn=Depends(get_conn)):
    current = current_month(conn)
    guarded(validate_start_month, payload.start_month, current)
    db.checkpoint(conn, f"Created {payload.name.strip()}.")
    stamp = db.now()
    conn.execute(
        'INSERT INTO initiative (name, "rank", start_month, '
        "owner, notes, archived, created_at, updated_at) VALUES (?, ?, ?, ?, ?, 0, ?, ?)",
        (
            payload.name.strip(),
            db.next_rank(conn),
            payload.start_month,
            payload.owner.strip(),
            payload.notes.strip(),
            stamp,
            stamp,
        ),
    )
    conn.commit()
    return build_state(conn)


@app.patch("/api/initiatives/{initiative_id}")
def update_initiative(initiative_id: int, payload: InitiativePatch, conn=Depends(get_conn)):
    row = conn.execute(
        "SELECT name, start_month, archived FROM initiative WHERE id = ?", (initiative_id,)
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="No such initiative.")

    current = current_month(conn)
    fields = payload.model_dump(exclude_unset=True)

    if "start_month" in fields and fields["start_month"] != row["start_month"]:
        # R8: a start cannot be moved into the past. Whether the initiative has
        # already started makes no difference — it can always be pushed out.
        guarded(validate_start_month, fields["start_month"], current)

    db.checkpoint(conn, _patch_label(row, fields))

    editable = {"name", "start_month", "owner", "notes", "archived"}
    for key, value in fields.items():
        if key not in editable:
            bad(f"Cannot edit {key}.")
        if isinstance(value, str):
            value = value.strip()
        if key == "archived":
            value = int(bool(value))
        conn.execute(f"UPDATE initiative SET {key} = ? WHERE id = ?", (value, initiative_id))
    conn.execute(
        "UPDATE initiative SET updated_at = ? WHERE id = ?", (db.now(), initiative_id)
    )
    conn.commit()
    return build_state(conn)


@app.delete("/api/initiatives/{initiative_id}")
def delete_initiative(initiative_id: int, conn=Depends(get_conn)):
    db.checkpoint(conn, f"Deleted {_initiative_name(conn, initiative_id)}.")
    conn.execute("DELETE FROM initiative WHERE id = ?", (initiative_id,))
    remaining = [
        r["id"]
        for r in conn.execute('SELECT id FROM initiative ORDER BY "rank"').fetchall()
    ]
    db.renumber_ranks(conn, remaining)
    conn.commit()
    return build_state(conn)


class ReorderIn(BaseModel):
    ordered_ids: list[int]


@app.post("/api/initiatives/reorder")
def reorder(payload: ReorderIn, conn=Depends(get_conn)):
    known = {r["id"] for r in conn.execute("SELECT id FROM initiative").fetchall()}
    if set(payload.ordered_ids) != known:
        bad("The reorder must list every initiative exactly once.")
    db.checkpoint(conn, _reorder_label(conn, payload.ordered_ids))
    db.renumber_ranks(conn, payload.ordered_ids)
    conn.commit()
    return build_state(conn)


class DemandLine(BaseModel):
    team_id: int
    offset: int
    fte_h: int


class DemandIn(BaseModel):
    lines: list[DemandLine]


@app.put("/api/initiatives/{initiative_id}/demand")
def replace_demand(
    initiative_id: int, payload: DemandIn, amend: bool = False, conn=Depends(get_conn)
):
    """Replace an initiative's demand profile.

    `amend=1` says this is the second half of one user action — the editor
    saves a create-or-patch and then the grid — and folds it into the
    checkpoint that request already took, so one Save is one Ctrl+Z.
    """
    seen = set()
    for line in payload.lines:
        guarded(validate_fte, line.fte_h)
        if line.offset < 0 or line.offset > MAX_OFFSET:
            bad(f"Offset must be between 0 and {MAX_OFFSET}.")
        if (line.team_id, line.offset) in seen:
            bad("Duplicate team and offset in the demand grid.")
        seen.add((line.team_id, line.offset))

    db.checkpoint(
        conn, f"Changed the demand for {_initiative_name(conn, initiative_id)}.", amend=amend
    )
    conn.execute("DELETE FROM demand WHERE initiative_id = ?", (initiative_id,))
    for line in payload.lines:
        if line.fte_h > 0:
            conn.execute(
                "INSERT INTO demand (initiative_id, team_id, offset_m, fte_h) "
                "VALUES (?, ?, ?, ?)",
                (initiative_id, line.team_id, line.offset, line.fte_h),
            )
    conn.execute(
        "UPDATE initiative SET updated_at = ? WHERE id = ?", (db.now(), initiative_id)
    )
    conn.commit()
    return build_state(conn)


@app.get("/api/initiatives/{initiative_id}/fit")
def fit(initiative_id: int, conn=Depends(get_conn)):
    data = db.load_engine_inputs(conn)
    settings = db.get_settings(conn)
    hints = engine.fit_hints(
        teams=[t["id"] for t in data["teams"]],
        supply=data["supply"],
        reserves=data["reserves"],
        initiatives=data["initiatives"],
        demand=data["demand"],
        current_month=settings["current_month"] or today_month(),
        initiative_id=initiative_id,
        horizon_months=settings["horizon_months"],
    )
    return {"hints": hints}


# --- settings ----------------------------------------------------------------


class SettingsIn(BaseModel):
    horizon_months: int | None = Field(default=None, ge=1, le=120)
    current_month: str | None = None  # "" restores the real clock


@app.put("/api/settings")
def update_settings(payload: SettingsIn, conn=Depends(get_conn)):
    db.checkpoint(conn, "Changed the settings.")
    if payload.horizon_months is not None:
        db.set_setting(conn, "horizon_months", str(payload.horizon_months))
    if payload.current_month is not None:
        if payload.current_month:
            guarded(validate_month, payload.current_month)
        db.set_setting(conn, "current_month", payload.current_month)
    conn.commit()
    return build_state(conn)


class ImportIn(BaseModel):
    csv: str = Field(min_length=1, max_length=4_000_000)


@app.post("/api/import")
def import_csv(payload: ImportIn, conn=Depends(get_conn)):
    """Create or update initiatives from CSV, matching on Reference.

    All or nothing: the file is validated completely before anything is
    written, and every problem is reported at once. Start months in the past
    are accepted here — unlike the editor — because an export of work already
    under way is the normal case, and R7 means only the remaining months are
    evaluated anyway.
    """
    data = db.load_engine_inputs(conn)
    rows, errors = importer.parse(payload.csv, data["teams"])
    if errors:
        raise HTTPException(
            status_code=400,
            detail={
                "message": f"Nothing was imported. {len(errors)} problem"
                + ("s" if len(errors) != 1 else "")
                + " found:",
                "errors": errors,
            },
        )

    before = {i["id"]: i["status"] for i in build_state(conn)["initiatives"]}
    db.checkpoint(conn, f"Imported {len(rows)} row{'s' if len(rows) != 1 else ''}.")

    existing = {
        (r["reference"] or "").lower(): r["id"]
        for r in conn.execute(
            "SELECT id, reference FROM initiative "
            "WHERE reference IS NOT NULL AND reference != ''"
        )
    }
    stamp = db.now()
    created, updated = [], []

    try:
        for row in rows:
            ref_key = row["reference"].lower()
            initiative_id = existing.get(ref_key)
            if initiative_id:
                # Rank and archived are left alone: the file says what the work
                # is, not where it sits in the plan or whether you set it aside.
                conn.execute(
                    "UPDATE initiative SET name = ?, start_month = ?, reference = ?, "
                    "updated_at = ? WHERE id = ?",
                    (row["name"], row["start_month"], row["reference"], stamp, initiative_id),
                )
                updated.append(row["name"])
            else:
                initiative_id = conn.execute(
                    'INSERT INTO initiative (name, "rank", start_month, reference, owner, '
                    "notes, archived, created_at, updated_at) VALUES (?, ?, ?, ?, '', '', 0, ?, ?)",
                    (row["name"], db.next_rank(conn), row["start_month"], row["reference"], stamp, stamp),
                ).lastrowid
                existing[ref_key] = initiative_id
                created.append(row["name"])

            conn.execute("DELETE FROM demand WHERE initiative_id = ?", (initiative_id,))
            for team_id, fte_h in row["demand"].items():
                for offset in range(row["months"]):
                    conn.execute(
                        "INSERT INTO demand (initiative_id, team_id, offset_m, fte_h) "
                        "VALUES (?, ?, ?, ?)",
                        (initiative_id, team_id, offset, fte_h),
                    )
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    state = build_state(conn)
    changed = [
        {"name": i["name"], "from": before[i["id"]], "to": i["status"]}
        for i in state["initiatives"]
        if i["id"] in before and before[i["id"]] != i["status"]
    ]
    return {
        **state,
        "import": {"created": created, "updated": updated, "changed": changed},
    }


class ConvertIn(BaseModel):
    text: str = Field(max_length=100_000)


@app.post("/api/convert")
def convert_summary(payload: ConvertIn, conn=Depends(get_conn)):
    """Turn "GRC: 1" lines into the two CSV rows an import needs.

    Shares parse_fte with the importer, so anything this returns is something
    the import will accept. Touches no data.
    """
    data = db.load_engine_inputs(conn)
    result = importer.parse_demand_summary(payload.text, data["teams"])
    if result["errors"]:
        raise HTTPException(
            status_code=400,
            detail={"message": "Could not convert:", "errors": result["errors"]},
        )
    return result


@app.post("/api/seed")
def reseed(conn=Depends(get_conn)):
    db.checkpoint(conn, "Reset to the fixture.")
    db.seed_fixture(conn)
    return build_state(conn)


@app.post("/api/reset")
def reset(conn=Depends(get_conn)):
    """Empty the plan completely, for setting one up from scratch.

    Checkpointed like any other change, so an accidental reset is one Ctrl+Z
    away for as long as the page stays open.
    """
    db.checkpoint(conn, "Reset to an empty plan.")
    db.reset_to_empty(conn)
    return build_state(conn)


@app.post("/api/undo")
def undo(conn=Depends(get_conn)):
    label = db.undo(conn)
    if label is None:
        bad("There is nothing left to undo.")
    return {**build_state(conn), "undone": label}


# --- export ------------------------------------------------------------------

EXPORT_COLUMNS = ["InitiativeName", "Reference", "StartMonth", "EndMonth"]

# A cell opened in a spreadsheet and starting with one of these is a formula,
# not text, so a name is prefixed with an apostrophe before it can become one.
# Excel and LibreOffice both show the text and drop the apostrophe. No ordinary
# initiative name starts with any of them, so nothing legitimate is altered.
FORMULA_LEADERS = ("=", "+", "-", "@", "\t", "\r")


def csv_safe(value: str) -> str:
    return f"'{value}" if value.startswith(FORMULA_LEADERS) else value


def export_rows(conn) -> list[list[str]]:
    """Every live initiative as name, reference, start and end.

    Four columns and no more. The import's team columns carry one FTE for the
    whole span, and a profile dialled in month by month cannot be written that
    way without quietly flattening it — so the FTE is not exported at all
    rather than exported wrong. What comes out is what identifies a piece of
    work and when it runs, which is what another tracker wants to be told.

    Archived initiatives are left out: "set aside" is not one of these four
    columns, so exporting them would present them as live work.
    """
    initiatives = conn.execute(
        'SELECT id, name, reference, start_month FROM initiative '
        'WHERE archived = 0 ORDER BY "rank"'
    ).fetchall()
    # An initiative with no demand at all still occupies its start month.
    lengths = {
        r["initiative_id"]: r["last"] + 1
        for r in conn.execute(
            "SELECT initiative_id, MAX(offset_m) AS last FROM demand GROUP BY initiative_id"
        )
    }
    return [
        [
            csv_safe(row["name"]),
            csv_safe(row["reference"] or ""),
            row["start_month"],
            engine.month_add(row["start_month"], lengths.get(row["id"], 1) - 1),
        ]
        for row in initiatives
    ]


def export_csv(conn) -> str:
    out = io.StringIO()
    writer = csv.writer(out)  # CRLF by default, which is what a spreadsheet wants
    writer.writerow(EXPORT_COLUMNS)
    writer.writerows(export_rows(conn))
    return out.getvalue()


@app.get("/api/export.csv")
def export(conn=Depends(get_conn)):
    """Download the plan's initiatives as CSV. Touches no data."""
    return Response(
        content=export_csv(conn),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition":
                f'attachment; filename="smolplan-{date.today().isoformat()}.csv"'
        },
    )


# --- static ------------------------------------------------------------------


@app.get("/")
def index():
    return FileResponse(os.path.join(HERE, "static", "index.html"))


app.mount("/static", StaticFiles(directory=os.path.join(HERE, "static")), name="static")
