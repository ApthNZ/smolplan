"""CSV import parsing.

Pure module: no database, no web framework. It turns CSV text plus the set of
known teams into a list of rows ready to write and a list of every problem
found. A forty-row export should tell you everything wrong with it in one go,
not one error per attempt. When there are problems the rows are only the ones
that parsed cleanly, handed back so the caller can check them against the plan
too, and nothing may be written.

Expected shape, matched by header name so column order does not matter:

    InitiativeName,Reference,StartMonth,EndMonth,SOC,GRC
    Project123,PRO-001,2026-09,2026-12,1,0.5

An optional Deadline column carries each initiative's deadline month. Every
other column that is not one of the known headers is a team name, and its
value is that team's FTE for every month from StartMonth to EndMonth inclusive.
That is why a team cannot be called Deadline here: its column would be read as
the deadline.

StartMonth, EndMonth and Deadline may also arrive as full dates — see month_of.

A Jira CSV export is recognised by its Team Capacity field and read on its own
terms — see the Jira section below.
"""

from __future__ import annotations

import csv
import io
import re
from decimal import Decimal, InvalidOperation

from engine import MONTH_RE, month_index, month_span

NAME = "initiativename"
REFERENCE = "reference"
START = "startmonth"
END = "endmonth"
DEADLINE = "deadline"  # optional
RESERVED = {NAME, REFERENCE, START, END, DEADLINE}

MAX_FTE_H = 10000  # 100.00 FTE, matching the API
MAX_SPAN = 120  # months; demand offsets run 0-119
MAX_NAME = 120
MAX_REFERENCE = 60
MAX_ROWS = 500


def normalise(value: str | None) -> str:
    return (value or "").strip()


def key(value: str | None) -> str:
    return normalise(value).lower()


# A spreadsheet asked for a month will hand back a timestamp: "1/07/2026 0:00"
# out of Excel, "2026-07-01T00:00:00" out of an issue tracker. Both forms are
# matched here and reduced to their month.
ISO_DATE_RE = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})(?:[T ].*)?$")
SLASH_DATE_RE = re.compile(r"^(\d{1,2})[/-](\d{1,2})[/-](\d{4})(?:[T ].*)?$")
# Jira's own date format, "01/Jan/27 12:00 AM", before a spreadsheet rewrites
# it. The month is a name, so a two-digit year is not ambiguous here as it is
# in 1/2/27.
NAMED_DATE_RE = re.compile(r"^(\d{1,2})[/ -]([A-Za-z]{3,9})[/ -](\d{2}|\d{4})(?:[T ].*)?$")
MONTH_NAMES = [
    "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december",
]


def month_named(name: str) -> int:
    """1-12 for a month's name or its abbreviation (Jan, Sept), else 0."""
    name = name.lower()
    for number, full in enumerate(MONTH_NAMES, start=1):
        if len(name) >= 3 and full.startswith(name):
            return number
    return 0


def month_of(value: str | None) -> str:
    """A month, from a month or from a full date.

    The day and the time in a full date say nothing a plan cares about, so they
    are dropped rather than rejected — 1/07/2026 and 31/07/2026 are both
    2026-07. Anything that is not a date is returned unchanged, for the caller
    to reject with the text the file actually contained.

    Slashed dates are read day-first, the form the spreadsheets feeding this
    tool export. A first number above 12 is a day under either reading; a
    second number above 12 is read month-first, because nothing else works.
    """
    value = normalise(value)
    if MONTH_RE.match(value):
        return value

    iso = ISO_DATE_RE.match(value)
    named = NAMED_DATE_RE.match(value)
    if iso:
        year, month, day = (int(part) for part in iso.groups())
    elif named:
        day, month, year = int(named.group(1)), month_named(named.group(2)), int(named.group(3))
        if year < 100:
            year += 2000
    else:
        slashed = SLASH_DATE_RE.match(value)
        if not slashed:
            return value
        day, month, year = (int(part) for part in slashed.groups())
        if month > 12 and day <= 12:
            day, month = month, day

    if not (1 <= month <= 12 and 1 <= day <= 31):
        return value
    return f"{year:04d}-{month:02d}"


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

    A problem with a row still leaves the other rows in `rows`, so the caller
    can check those against the plan in the same pass and report both kinds of
    problem at once, rather than the second only once the first is fixed. A
    problem with the file as a whole (its headers, its size) returns no rows.

    A row carries `deadline_month` only when the file has a Deadline column,
    and then a blank cell is None. The difference matters to an update: no
    column leaves the deadline as it was, a blank cell clears it.
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
    fields = [jira_field(h) for h in lower]
    jira = CAPACITY in fields

    errors: list[str] = []
    team_columns: list[tuple[int, str]] = []

    if jira:
        # Jira exports every field it has, and repeats some (one Sprint column
        # per sprint), so only the columns read here are checked, and the rest
        # are ignored rather than taken for teams.
        columns = [JIRA_COLUMNS.get(f, f) for f in fields]
        read = RESERVED | {CAPACITY}
        label = {c: headers[columns.index(c)] for c in read if c in columns}

        duplicates = sorted({headers[i] for i, c in enumerate(columns) if c in read and columns.count(c) > 1})
        if duplicates:
            errors.append(f"Duplicate columns: {', '.join(duplicates)}.")

        missing = [want for want in (NAME, REFERENCE, START, END) if want not in columns]
        if missing:
            wanted = {NAME: "Summary", REFERENCE: "Issue key", START: "Target start", END: "Target end"}
            errors.append(
                "This looks like a Jira export, but it is missing the "
                + ", ".join(wanted[m] for m in missing)
                + f" field{'s' if len(missing) > 1 else ''}. Add "
                + ("them" if len(missing) > 1 else "it")
                + " to the export's columns."
            )
    else:
        columns = lower
        label = {NAME: "InitiativeName", REFERENCE: "Reference", START: "StartMonth",
                 END: "EndMonth", DEADLINE: "Deadline"}

        if "issue key" in fields and NAME not in lower:
            # Every Jira column would otherwise be reported as an unknown team.
            return [], [
                "This looks like a Jira export, but it has no Team Capacity field, which is "
                "where each team's FTE is read from. Add it to the export's columns."
            ]

        duplicates = sorted({h for h in lower if lower.count(h) > 1 and h})
        if duplicates:
            errors.append(f"Duplicate columns: {', '.join(duplicates)}.")

        missing = [want for want in (NAME, REFERENCE, START, END) if want not in lower]
        if missing:
            errors.append(
                "Missing required columns: "
                + ", ".join(label[m] for m in missing)
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

    index = {name: columns.index(name) for name in (NAME, REFERENCE, START, END)}
    has_deadline = DEADLINE in columns
    if has_deadline:
        index[DEADLINE] = columns.index(DEADLINE)
    if jira:
        index[CAPACITY] = columns.index(CAPACITY)
    rows: list[dict] = []
    seen: dict[str, int] = {}
    unknown_teams: dict[str, list[int]] = {}  # Jira only: named with FTE, not in the plan

    for offset, raw in enumerate(data_rows):
        line = offset + 2  # header is line 1
        before = len(errors)

        def cell(position: int) -> str:
            return normalise(raw[position]) if position < len(raw) else ""

        name = cell(index[NAME])
        reference = cell(index[REFERENCE])
        start = month_of(cell(index[START]))
        end = month_of(cell(index[END]))

        # A Jira row spans as many lines of the file as its Team Capacity has,
        # so the issue key is the easier thing to find it by.
        where = f"Line {line} ({reference})" if jira and reference else f"Line {line}"

        if not name:
            errors.append(f"{where}: {label[NAME]} is empty.")
        elif len(name) > MAX_NAME:
            errors.append(f"{where}: {label[NAME]} is longer than {MAX_NAME} characters.")

        if not reference:
            errors.append(f"{where}: {label[REFERENCE]} is empty. It is what a re-import matches on.")
        elif len(reference) > MAX_REFERENCE:
            errors.append(f"{where}: {label[REFERENCE]} is longer than {MAX_REFERENCE} characters.")
        else:
            previous = seen.get(reference.lower())
            if previous:
                errors.append(
                    f"{where}: {label[REFERENCE]} {reference} already appears on line {previous}."
                )
            else:
                seen[reference.lower()] = line

        months: list[str] = []
        if not MONTH_RE.match(start):
            errors.append(f"{where}: {label[START]} {start!r} is not a month in YYYY-MM form.")
        if not MONTH_RE.match(end):
            errors.append(f"{where}: {label[END]} {end!r} is not a month in YYYY-MM form.")
        if MONTH_RE.match(start) and MONTH_RE.match(end):
            if month_index(end) < month_index(start):
                errors.append(f"{where}: {label[END]} {end} is before {label[START]} {start}.")
            else:
                months = month_span(start, end)
                if len(months) > MAX_SPAN:
                    errors.append(
                        f"{where}: {start} to {end} is {len(months)} months, "
                        f"more than the limit of {MAX_SPAN}."
                    )
                    months = []

        deadline = None  # a blank cell is no deadline, and clears one on an update
        if has_deadline and cell(index[DEADLINE]):
            deadline = month_of(cell(index[DEADLINE]))
            if not MONTH_RE.match(deadline):
                errors.append(f"{where}: {label[DEADLINE]} {deadline!r} is not a month in YYYY-MM form.")
            elif MONTH_RE.match(end) and month_index(end) > month_index(deadline):
                errors.append(f"{where}: {label[END]} {end} is after the {label[DEADLINE]} {deadline}.")

        demand: dict[int, int] = {}
        for position, heading in team_columns:
            value = cell(position)
            if not value:
                continue  # blank means this team is not needed
            try:
                fte_h = parse_fte(value)
            except ValueError as exc:
                errors.append(f"{where}, column {heading}: {exc}.")
                continue
            if fte_h:
                demand[by_name[key(heading)]["id"]] = fte_h

        unknown_here: list[str] = []
        if jira:
            demand, problems, unknown_here = parse_capacity(cell(index[CAPACITY]), teams)
            errors.extend(f"{where}, {label[CAPACITY]}: {p}." for p in problems)
            for team in unknown_here:
                unknown_teams.setdefault(team, []).append(line)

        if months and not demand and not unknown_here:
            errors.append(
                f"{where}: no team has any FTE, so this initiative would need nothing."
            )

        if len(errors) == before and months:
            row = {
                "line": line,
                "name": name,
                "reference": reference,
                "start_month": start,
                "months": len(months),
                "demand": demand,
            }
            if has_deadline:
                row["deadline_month"] = deadline
            rows.append(row)

    if unknown_teams:
        # Once for the file, not once per row: forty rows naming a team the
        # plan lacks is one thing to fix, not forty.
        named = ", ".join(
            f"{team} (line{'s' if len(lines) > 1 else ''} {', '.join(map(str, lines))})"
            for team, lines in sorted(unknown_teams.items(), key=lambda item: key(item[0]))
        )
        errors.append(
            f"{label[CAPACITY]} gives FTE to team{'s' if len(unknown_teams) > 1 else ''} "
            f"this plan does not have: {named}. Known teams are: {known}. "
            "Add the team first, or correct the name in Jira."
        )

    return rows, errors


# --- Jira exports --------------------------------------------------------------
#
# A Jira CSV export names its columns after Jira's fields, wrapping custom ones
# as "Custom field (Target start)", and carries the teams not as columns but as
# free text in one field, typed by hand and so never quite the same twice:
#
#     Teams required to resource: Ent IT / DevOps
#     Estimated Team FTE:
#     SOC: 0.5
#     GRC: 0
#
# The file is recognised as Jira by that Team Capacity field. Every other column
# is ignored, since Jira exports dozens and none of them is a team.

CAPACITY = "team capacity"
JIRA_COLUMNS = {"summary": NAME, "issue key": REFERENCE, "target start": START, "target end": END}
CUSTOM_FIELD_RE = re.compile(r"^custom field \((.*)\)$")

# One entry of the capacity text. Lines are also split on | and ; for anyone who
# writes the teams on one line; never on a comma, which would turn a decimal
# comma's 0,5 into a silent 0.
CAPACITY_SPLIT_RE = re.compile(r"[\r\n|;]")
ENTRY_RE = re.compile(r"^([^:=]*?)\s*[:=]\s*(.*)$")
BARE_ENTRY_RE = re.compile(r"^(.*?[^\s\-–])\s*[-–]?\s*(\d*\.?\d+)\s*(?:fte)?$", re.IGNORECASE)
NUMBER_RE = re.compile(r"^\d*\.?\d+$")
FTE_UNIT_RE = re.compile(r"\s*fte$", re.IGNORECASE)
NOT_NEEDED = {"-", "–", "n/a", "na", "none", "nil"}
MARKUP = " \t*_•·-–"  # list bullets and Jira's *bold* / _italic_ around a name
EMPHASIS = " \t*_"  # around a number; never a dash, which would make -0.5 read as 0.5


def jira_field(header: str) -> str:
    """"custom field (target start)" -> "target start"; anything else unchanged."""
    wrapped = CUSTOM_FIELD_RE.match(header)
    return wrapped.group(1).strip() if wrapped else header


def squash(name: str) -> str:
    """A team name with case, spacing and punctuation gone: Sec-Eng is SecEng."""
    return re.sub(r"[^a-z0-9]", "", key(name))


def parse_capacity(text: str, teams: list[dict]) -> tuple[dict[int, int], list[str], list[str]]:
    """Read team FTE out of a Jira Team Capacity field.

    Returns (demand, problems, unknown): demand as {team_id: hundredths}, the
    problems with teams the plan has, and the names of teams it does not have
    that were given FTE.

    The field is free text, so what is not a "Team: number" entry is prose and
    is skipped — a heading, a list of who else is involved. The line is drawn at
    the teams: an entry that names one of the plan's teams must hold a number
    the import can use, or it is an error, because skipping it would drop that
    team's demand without a word. A team the plan does not have is reported
    only when it is given FTE; at zero it needs nothing and nothing is lost.
    """
    by_exact = {key(t["name"]): t for t in teams}
    by_squash = {squash(t["name"]): t for t in teams}

    def team_named(name: str) -> dict | None:
        return by_exact.get(key(name)) or by_squash.get(squash(name))

    demand: dict[int, int] = {}
    problems: list[str] = []
    unknown: list[str] = []
    given: set[int] = set()

    for segment in CAPACITY_SPLIT_RE.split(text or ""):
        segment = segment.replace('"', "").strip()
        entry = ENTRY_RE.match(segment)
        if entry:
            name, value = entry.groups()
        else:
            # "SOC 0.5" or "SOC - 0.5" with no colon: an entry only if it names
            # a team, since otherwise any sentence ending in a number would be.
            bare = BARE_ENTRY_RE.match(segment)
            if not bare or not team_named(bare.group(1).strip(MARKUP)):
                continue
            name, value = bare.groups()

        name = name.strip(MARKUP)
        value = FTE_UNIT_RE.sub("", value.strip(EMPHASIS)).strip(EMPHASIS)
        if not name or not value:
            continue  # "Estimated Team FTE:" is a heading, "SOC:" a blank cell

        team = team_named(name)
        if team is None:
            if NUMBER_RE.match(value) and Decimal(value) > 0 and name not in unknown:
                unknown.append(name)
            continue

        if team["id"] in given:
            problems.append(f"{team['name']} is given more than once")
            continue
        given.add(team["id"])

        if value.lower() in NOT_NEEDED:
            continue
        try:
            fte_h = parse_fte(value)
        except ValueError as exc:
            problems.append(f"{team['name']}: {exc}")
            continue
        if fte_h:
            demand[team["id"]] = fte_h

    return demand, problems, unknown


# --- the demand-summary converter --------------------------------------------


def parse_demand_summary(text: str, teams: list[dict] | None = None) -> dict:
    """Turn "GRC: 1" lines into the two CSV rows an import needs.

        GRC: 1          ->      GRC,SOC,ENG
        SOC:2                   1,2,0.5
          ENG : 0.5

    Values are validated with the same parse_fte the import uses, so anything
    this emits is something the import will accept. They are echoed as written
    rather than normalised — 0.5 stays 0.5 — because the point is to save
    typing, not to reformat.

    Double quotes are dropped wherever they appear. A summary pasted out of a
    spreadsheet or a CSV cell arrives as "GRC: 1", or GRC: "1", and the quotes
    are punctuation from the copy rather than anything the user typed.

    Returns {"names", "values", "csv", "errors", "unknown"}. Unknown team names
    are a warning, not an error: a summary may be built for another instance.
    """
    known = {key(t["name"]): t["name"] for t in (teams or [])}

    names: list[str] = []
    values: list[str] = []
    errors: list[str] = []
    seen: dict[str, int] = {}

    for offset, raw in enumerate(text.splitlines()):
        line = offset + 1
        raw = raw.replace('"', "")  # see the docstring: quotes come from the paste
        if not normalise(raw):
            continue  # blank lines are just spacing

        if ":" not in raw:
            errors.append(f"Line {line}: no colon. Each line should read \"Team: FTE\".")
            continue

        # Split on the first colon only, so a stray one in a value is caught by
        # the number check rather than silently truncating the line.
        name, _, value = raw.partition(":")
        name = normalise(name)
        value = normalise(value)

        if not name:
            errors.append(f"Line {line}: no team name before the colon.")
            continue
        if not value:
            errors.append(f"Line {line}: no FTE after the colon for {name}.")
            continue

        previous = seen.get(key(name))
        if previous:
            errors.append(f"Line {line}: {name} already appears on line {previous}.")
            continue
        seen[key(name)] = line

        try:
            parse_fte(value)
        except ValueError as exc:
            errors.append(f"Line {line}, {name}: {exc}.")
            continue

        names.append(known.get(key(name), name))
        values.append(value)

    if not errors and not names:
        errors.append("Nothing to convert. Enter one line per team, as \"Team: FTE\".")

    unknown = [n for n in names if known and key(n) not in known]
    return {
        "names": names,
        "values": values,
        "csv": ",".join(names) + "\n" + ",".join(values) if names and not errors else "",
        "errors": errors,
        "unknown": unknown,
    }
