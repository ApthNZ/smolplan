"""smolplan: a small capacity planner.

No auth, no users, no audit trail. It is meant to run on a laptop, or on a
private network behind whatever already guards that network. See SECURITY.md.
"""

from __future__ import annotations

import os
from datetime import date

from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import db
import engine

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
        if not conn.execute("SELECT 1 FROM team LIMIT 1").fetchone():
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
    conn.execute("INSERT INTO team (name) VALUES (?)", (payload.name.strip(),))
    conn.commit()
    return build_state(conn)


@app.patch("/api/teams/{team_id}")
def rename_team(team_id: int, payload: TeamIn, conn=Depends(get_conn)):
    conn.execute("UPDATE team SET name = ? WHERE id = ?", (payload.name.strip(), team_id))
    conn.commit()
    return build_state(conn)


@app.delete("/api/teams/{team_id}")
def delete_team(team_id: int, conn=Depends(get_conn)):
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
    conn.execute("DELETE FROM reserve WHERE id = ?", (reserve_id,))
    conn.commit()
    return build_state(conn)


@app.put("/api/reserves/{reserve_id}/lines")
def fill_reserve(reserve_id: int, payload: FillIn, conn=Depends(get_conn)):
    months = guarded(validate_span, payload.from_month, payload.to_month)
    if payload.fte_h:
        guarded(validate_fte, payload.fte_h)
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
        "SELECT start_month FROM initiative WHERE id = ?", (initiative_id,)
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="No such initiative.")

    current = current_month(conn)
    fields = payload.model_dump(exclude_unset=True)

    if "start_month" in fields and fields["start_month"] != row["start_month"]:
        # R8: a start cannot be moved into the past. Whether the initiative has
        # already started makes no difference — it can always be pushed out.
        guarded(validate_start_month, fields["start_month"], current)

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
def replace_demand(initiative_id: int, payload: DemandIn, conn=Depends(get_conn)):
    seen = set()
    for line in payload.lines:
        guarded(validate_fte, line.fte_h)
        if line.offset < 0 or line.offset > MAX_OFFSET:
            bad(f"Offset must be between 0 and {MAX_OFFSET}.")
        if (line.team_id, line.offset) in seen:
            bad("Duplicate team and offset in the demand grid.")
        seen.add((line.team_id, line.offset))

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
    if payload.horizon_months is not None:
        db.set_setting(conn, "horizon_months", str(payload.horizon_months))
    if payload.current_month is not None:
        if payload.current_month:
            guarded(validate_month, payload.current_month)
        db.set_setting(conn, "current_month", payload.current_month)
    conn.commit()
    return build_state(conn)


@app.post("/api/seed")
def reseed(conn=Depends(get_conn)):
    db.seed_fixture(conn)
    return build_state(conn)


# --- static ------------------------------------------------------------------


@app.get("/")
def index():
    return FileResponse(os.path.join(HERE, "static", "index.html"))


app.mount("/static", StaticFiles(directory=os.path.join(HERE, "static")), name="static")
