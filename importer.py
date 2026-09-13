"""CSV import parsing.

Pure module: no database, no web framework. It turns CSV text plus the set of
known teams into either a list of rows ready to write, or a list of every
problem found — never a partial result. A forty-row export should tell you
everything wrong with it in one go, not one error per attempt.

Expected shape, matched by header name so column order does not matter:

    InitiativeName,Reference,StartMonth,EndMonth,SOC,GRC
    Project123,PRO-001,2026-09,2026-12,1,0.5

Every column that is not one of the four known headers is a team name, and its
value is that team's FTE for every month from StartMonth to EndMonth inclusive.
"""

from __future__ import annotations

import csv
import io
from decimal import Decimal, InvalidOperation

from engine import MONTH_RE, month_index, month_span

NAME = "initiativename"
REFERENCE = "reference"
START = "startmonth"
END = "endmonth"
RESERVED = {NAME, REFERENCE, START, END}

MAX_FTE_H = 10000  # 100.00 FTE, matching the API
MAX_SPAN = 120  # months; demand offsets run 0-119
MAX_NAME = 120
MAX_REFERENCE = 60
MAX_ROWS = 500


def normalise(value: str | None) -> str:
    return (value or "").strip()


def key(value: str | None) -> str:
    return normalise(value).lower()


def parse_fte(text: str) -> int:
    """FTE as integer hundredths. Raises ValueError with a readable message.

    More than two decimal places is an error rather than a silent round: the
    import should never quietly store a number the file did not contain.
    """
    cleaned = normalise(text)
    try:
        amount = Decimal(cleaned)
    except InvalidOperation:
        raise ValueError(f"{cleaned!r} is not a number")
    if not amount.is_finite():  # Decimal("nan") and Decimal("inf") parse happily
        raise ValueError(f"{cleaned!r} is not a number")
    if amount < 0:
        raise ValueError(f"{cleaned} is negative")
    if amount.as_tuple().exponent < -2:
        raise ValueError(f"{cleaned} has more than two decimal places")
    hundredths = int(amount.scaleb(2).to_integral_value())
    if hundredths > MAX_FTE_H:
        raise ValueError(f"{cleaned} is above the maximum of {MAX_FTE_H / 100:.2f} FTE")
    return hundredths


def parse(text: str, teams: list[dict]) -> tuple[list[dict], list[str]]:
    """Parse CSV text against the known teams.

    `teams` is the list of {"id", "name"} dicts the API already holds. Returns
    (rows, errors); if errors is non-empty the caller must write nothing.
    """
    by_name = {key(t["name"]): t for t in teams}
    known = ", ".join(sorted(t["name"] for t in teams)) or "none — add a team first"

    # utf-8-sig: exports from Jira and Excel routinely carry a byte-order mark,
    # which would otherwise become part of the first header's name.
    if isinstance(text, bytes):
        text = text.decode("utf-8-sig", errors="replace")
    text = text.lstrip("﻿")

    reader = csv.reader(io.StringIO(text))
    try:
        raw_rows = [row for row in reader if any(normalise(cell) for cell in row)]
    except csv.Error as exc:
        return [], [f"The file could not be read as CSV: {exc}"]

    if not raw_rows:
        return [], ["The file is empty."]

    headers = [normalise(h) for h in raw_rows[0]]
    lower = [h.lower() for h in headers]

    errors: list[str] = []

    duplicates = sorted({h for h in lower if lower.count(h) > 1 and h})
    if duplicates:
        errors.append(f"Duplicate columns: {', '.join(duplicates)}.")

    missing = [want for want in (NAME, REFERENCE, START, END) if want not in lower]
    if missing:
        wanted = {NAME: "InitiativeName", REFERENCE: "Reference", START: "StartMonth", END: "EndMonth"}
        errors.append(
            "Missing required columns: "
            + ", ".join(wanted[m] for m in missing)
            + f". Found: {', '.join(headers) or 'nothing'}."
        )

    team_columns = [(i, h) for i, h in enumerate(headers) if h.lower() not in RESERVED and h]
    unknown = [h for _, h in team_columns if key(h) not in by_name]
    if unknown:
        errors.append(
            f"Unknown team{'s' if len(unknown) > 1 else ''}: {', '.join(unknown)}. "
            f"Known teams are: {known}. Add the team first, or correct the column heading."
        )

    if not team_columns and not errors:
        errors.append("No team columns found — add one column per team, headed with the team name.")

    if errors:
        return [], errors

    data_rows = raw_rows[1:]
    if not data_rows:
        return [], ["The file has a header but no rows."]
    if len(data_rows) > MAX_ROWS:
        return [], [f"{len(data_rows)} rows is more than the limit of {MAX_ROWS}."]

    index = {name: lower.index(name) for name in (NAME, REFERENCE, START, END)}
    rows: list[dict] = []
    seen: dict[str, int] = {}

    for offset, raw in enumerate(data_rows):
        line = offset + 2  # header is line 1
        before = len(errors)

        def cell(position: int) -> str:
            return normalise(raw[position]) if position < len(raw) else ""

        name = cell(index[NAME])
        reference = cell(index[REFERENCE])
        start = cell(index[START])
        end = cell(index[END])

        if not name:
            errors.append(f"Line {line}: InitiativeName is empty.")
        elif len(name) > MAX_NAME:
            errors.append(f"Line {line}: InitiativeName is longer than {MAX_NAME} characters.")

        if not reference:
            errors.append(f"Line {line}: Reference is empty. It is what a re-import matches on.")
        elif len(reference) > MAX_REFERENCE:
            errors.append(f"Line {line}: Reference is longer than {MAX_REFERENCE} characters.")
        else:
            previous = seen.get(reference.lower())
            if previous:
                errors.append(
                    f"Line {line}: Reference {reference} already appears on line {previous}."
                )
            else:
                seen[reference.lower()] = line

        months: list[str] = []
        if not MONTH_RE.match(start):
            errors.append(f"Line {line}: StartMonth {start!r} is not a month in YYYY-MM form.")
        if not MONTH_RE.match(end):
            errors.append(f"Line {line}: EndMonth {end!r} is not a month in YYYY-MM form.")
        if MONTH_RE.match(start) and MONTH_RE.match(end):
            if month_index(end) < month_index(start):
                errors.append(f"Line {line}: EndMonth {end} is before StartMonth {start}.")
            else:
                months = month_span(start, end)
                if len(months) > MAX_SPAN:
                    errors.append(
                        f"Line {line}: {start} to {end} is {len(months)} months, "
                        f"more than the limit of {MAX_SPAN}."
                    )
                    months = []

        demand: dict[int, int] = {}
        for position, heading in team_columns:
            value = cell(position)
            if not value:
                continue  # blank means this team is not needed
            try:
                fte_h = parse_fte(value)
            except ValueError as exc:
                errors.append(f"Line {line}, column {heading}: {exc}.")
                continue
            if fte_h:
                demand[by_name[key(heading)]["id"]] = fte_h

        if months and not demand:
            errors.append(
                f"Line {line}: no team has any FTE, so this initiative would need nothing."
            )

        if len(errors) == before and months:
            rows.append(
                {
                    "line": line,
                    "name": name,
                    "reference": reference,
                    "start_month": start,
                    "months": len(months),
                    "demand": demand,
                }
            )

    if errors:
        return [], errors
    return rows, []
