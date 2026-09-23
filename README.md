# smolplan

> ### ⚠️ This was written by an AI. Don't run it in production.
>
> Every line of this repository — the engine, the API, the UI, the tests, and
> this README — was written by Claude (Anthropic's Opus 5) in a single
> afternoon's conversation, working from a specification. A human reviewed the
> behaviour and steered the design; a human did not audit the code.
>
> It has **no authentication of any kind**. Anyone who can reach the page can
> edit the plan. It has never been load-tested, pen-tested, or run by anyone
> other than its author. It stores everything in one SQLite file with no
> migrations to speak of.
>
> It is a toy for thinking about capacity with, and it is shared in case the
> approach is useful to someone. Treat it as a worked example, not as software
> you would put in front of a team that depends on it.

A small capacity planner. Given ranked initiatives and finite team capacity, it
answers three questions: what can we deliver, when can we deliver it, and what
gets displaced when priorities change?

![The portfolio view: a ranked list of initiatives beside a monthly timeline, with capacity rows above and below showing supply, what reserves take, and what is left](docs/portfolio.png)

The loop is deliberately simple:

1. Each team has an FTE supply per month.
2. Reserves — business as usual, unplanned work — come off the top.
3. Initiatives are allocated in strict rank order.
4. An initiative that can't be **fully** staffed turns red and consumes nothing.
5. A human drags the red one somewhere it fits.

That fourth rule is the whole idea. A blocked initiative doesn't half-start and
soak up capacity; it consumes zero, so lower-ranked work fits around it and you
can see honestly what the plan delivers.

## Try it

```sh
pip install -r requirements.txt
uvicorn app:app --reload        # http://127.0.0.1:8000
```

Or with Docker:

```sh
docker compose up -d --build    # http://127.0.0.1:8107
```

The compose file publishes port 8107 by default; set `SMOLPLAN_PORT` to change
it. On a Windows laptop, see [docs/windows.md](docs/windows.md).

The database is created on first run and seeded with a demo anchored on the
current month: teams SOC and GRC, initiatives A, B and C, and B short of GRC
capacity so there's a red bar to play with immediately. Settings → *Reset to
fixture* puts it back, and *Reset to zero* clears it out for a real plan.

## Importing from a spreadsheet

The **Import and convert** tab has a CSV import, for pulling initiatives out of
an issue tracker rather than typing them in:

```
InitiativeName,Reference,StartMonth,EndMonth,SOC,GRC
Project123,PRO-001,2026-09,2026-12,1,0.5
ProjectABC,PRO-002,2026-10,2027-03,2,0.25
```

Every column that isn't one of the known headers is a team name, and its
value is that team's FTE for **every** month from start to end inclusive. A
blank cell means that team isn't needed. Column order and capitalisation don't
matter, because columns are matched by header name. `EndMonth` becomes the
initiative's end month.

`StartMonth` and `EndMonth` are months, but a spreadsheet asked for a month
hands back a timestamp, so a full date is accepted and reduced to its month:
`1/07/2026 0:00` and `2026-07-31` are both `2026-07`. Slashed dates are read
day-first.

An optional `Deadline` column carries each initiative's deadline (R11), as a
month or a full date. What it does on a re-import depends on whether the column
is there at all:

- **No `Deadline` column:** existing initiatives keep whatever deadline they
  have, and new ones get none. A row whose `EndMonth` would carry an initiative
  past the deadline it already has is an error — add the column to move the
  deadline, or end the row sooner.
- **A `Deadline` column:** the file says what every deadline is, as it does for
  every team column. A blank cell means no deadline, and clears one on an
  update.

Because `Deadline` is a known header, a team called Deadline can't have an
import column: it would be read as the deadline instead. Rename the team if you
need one.

Rows are matched on `Reference` — an issue key, typically — so re-importing an
updated export **updates those initiatives in place** rather than duplicating
them. Rank and archived state are left alone: the file says what the work is,
not where it sits in your plan.

Two deliberate departures from the rules above:

- **A start in the past is accepted here**, though R8 forbids it everywhere
  else. An export of work already under way is the normal case, and R7 means
  only the remaining months get evaluated.
- **Nothing is ever deleted.** An initiative missing from the file is left
  exactly as it was.

If anything is wrong — an unknown team, a malformed month, an end before a
start, an end past a deadline, a duplicated reference — the whole file is
rejected, nothing is written, and you get every problem at once rather than one
per attempt.

### Building the team columns

The same tab converts a demand summary into the two rows the import needs, so
you don't have to line the columns up by hand:

```
GRC: 1          ->      GRC,SOC,ENG
SOC:2                   1,2,0.5
  ENG : 0.5
```

Spacing is irrelevant, double quotes are dropped wherever they appear — a
summary pasted out of a spreadsheet arrives full of them — and the numbers go
through the same validation the import applies, so anything it produces will be
accepted. A team that doesn't exist yet
is flagged as a warning rather than an error — the summary might be for another
instance — but the import will reject it until you add the team.

## Exporting

The same tab exports the plan as CSV: one row per initiative, five columns.

```
InitiativeName,Reference,StartMonth,EndMonth,Deadline
Project123,PRO-001,2026-09,2026-12,
ProjectABC,PRO-002,2026-10,2027-03,2027-06
```

`EndMonth` is the initiative's end month, and `Deadline` is blank where there
isn't one. Rows come out in rank order, and archived initiatives are left out,
because "set aside" is not one of the five columns and exporting them would
present them as live work.

**There are deliberately no team columns, and no FTE.** An import gives a team
one figure for the whole span; a profile dialled in month by month cannot be
written that way. Rather than flatten it and export a number you never entered,
the FTE is left out entirely. What comes out is what identifies a piece of work
and when it runs — which is what another tracker wants to be told.

**It is not a backup.** Teams, supply, reserves, rank, owner, notes and archived
state are not in those five columns. Feeding the file back into the import is
refused, because it has no team columns — and that refusal is the safe answer,
since an import replaces the demand of every row it matches.

`GET /api/export.csv` returns the same file, for scripting it.

## Undo

**Ctrl+Z** steps back through the last fifty changes — drags, edits, deletions,
imports, a reset. The header shows what the next press would take back and how
far back it reaches.

The stack lives on the server, as whole-plan snapshots rather than a log of
inverse operations. A plan is a few hundred rows, so a snapshot costs nothing,
and "put it back exactly" needs no inverse written for deleting a team — which
cascades through its supply, reserves and demand — or for an import that touched
forty initiatives at once. It also means a second tab, or a reloaded page, undoes
the same history rather than keeping its own.

Inside a text box, Ctrl+Z is left to the browser: taking it away to undo the plan
instead would move something far from where you are looking and lose the
half-typed number. Shift+Ctrl+Z is conventionally redo, which doesn't exist here,
so it does nothing rather than quietly undoing again.

## What's on screen

- **Portfolio** — the ranked list beside a monthly timeline. Drag a row to
  re-rank, drag a bar to move a start, and the months where it would fit shade
  while you drag. Hovering a bar names what you would otherwise trace up to the
  header for: a green bar's start and end months ("Sep 2026 to Feb 2027"), a
  red bar's short teams. Capacity rows bracket the plan: supply, what the reserves
  take, and what is left after the green initiatives — which is the headroom
  you can actually move something into. Tags on each row show which teams it
  draws on, and clicking one filters the view to that team. A search box finds
  initiatives by name or reference, on top of the team filter; it hides only
  initiative rows, never the reserves or the capacity rows.
- **Edge glows** — a bar's ends say which way it can move. Blue on the start
  edge: it could start as early as the month in its tooltip and still be
  green. Yellow on the end edge: a month later would leave it short. Red on the
  end edge: it ends on its deadline and cannot slip at all, whatever capacity
  there is. The tooltip says the same in words, so no glow depends on colour
  alone. Like the drag shading, the blue glow reads the plan as the work
  ranked *above* leaves it, so starting sooner can still turn lower-ranked work
  red.
- **The editor** — click an initiative's name. It has a start month, an end
  month and an optional deadline, and the demand grid spans start to end.
  Moving the start carries the end with it; shortening the end drops the demand
  in the months cut off. A thin red tick on the row marks the deadline month.
- **Team capacity** — every team by month, shaded green at idle through to red
  when full. Click a cell to see exactly what is consuming it.
- **Supply and reserves** — editable grids with a bulk fill, e.g. set SOC to
  2.00 FTE from one month to another. Leaving the FTE box empty clears those
  months, which means "no supply data", not zero.
- **Import and convert** — CSV import, an export of the plan's initiatives,
  and a converter that turns a `GRC: 1` style summary into the team columns an
  import needs.
- **Rules** — the allocation rules, what each colour means, and the behaviour
  that surprises people.
- **Settings** — the horizon, a current-month override for experimenting, and
  two ways to start again: reset to the demo fixture, or reset to zero for an
  empty plan to build from scratch. Neither touches the horizon or the clock
  override, and both are undoable.

The month range in the header narrows all three data views at once. It only
changes what you see: the plan is always worked out over the whole horizon, so
hiding a month cannot change a status.

## The rules

- **R1** Plan in calendar months. FTE is stored as integer hundredths
  (0.50 FTE = 50) so the arithmetic is exact.
- **R2** Reserves are allocated before any initiative.
- **R3** Initiatives are allocated in strict rank order.
- **R4** All or nothing: an initiative is green only if every evaluated month is
  fully met for every team. A red initiative consumes zero capacity.
- **R5** Higher rank always wins. Adding or re-ranking can turn lower-ranked
  work red, including work that has already started.
- **R6** No pause. When work stops, shorten the initiative and create a new one
  for the remainder.
- **R7** Only the current and future months are evaluated. An initiative lying
  wholly in the past — its end month included, not just its last month of
  demand — reports `past` and can never turn red.
- **R8** No starts in the past. Anything can be pushed out, including work that
  has already begun; nothing can be dragged behind the clock.
- **R9** A team/month with no supply row has zero supply. Those shortfalls are
  labelled `no_supply_data`, separately from genuine over-allocation.
- **R10** Reserves may exceed supply. The cell is flagged and available capacity
  clamps to zero.
- **R11** A deadline is a hard limit: nothing can end after it. The editor, a
  drag and an import that would take an initiative past its deadline are all
  refused by the server, and the drag shading never offers such a month. A bar
  that already ends on its deadline glows red at its end, because it cannot
  slip at all, whatever capacity there is.

## How it's put together

```
engine.py   the allocation engine: pure, no framework, no database
db.py       SQLite schema, queries and the fixture seed
app.py      FastAPI routes, validation, and the state the UI renders from
static/     one HTML page, one stylesheet, one script, no build step
```

`engine.allocate()` takes teams, supply, reserves, initiatives, demand and the
current month, and returns a status per initiative plus a cell per team/month
with supply, reserved, allocated, free, warnings and the list of consumers.
`engine.fit_hints()` re-runs allocation over the initiatives ranked *above* one
initiative and reports the start months where it would be green, stopping
short of any that would carry it past its deadline — that's what shades the
timeline while you drag a bar.

The edge glows ask the same question without re-running anything. Allocation
goes in rank order, so at the moment an initiative is reached the cells hold
exactly the reserves and the higher-ranked green work — the base `fit_hints`
builds. The earlier starts and the one-month slip are tested against the cells
right then, before the initiative takes its own share, with the same fit test
the drag shading uses.

There is no build step. The frontend is about 900 lines of dependency-free
JavaScript that renders from a single state object fetched after every change.

![The team capacity view: teams as rows, months as columns, each cell shaded green to red by utilisation and showing the free FTE](docs/heatmap.png)

## Design notes

A few decisions that look like omissions but aren't:

- **Ranks carry no `UNIQUE` constraint.** SQLite checks uniqueness per row, not
  per statement, so an incremental shift trips over itself halfway through.
  Re-ranking rewrites every row as 1..n in one transaction instead.
- **FTE is integers, not floats.** `0.10 + 0.20` against `0.30` available fails
  under float arithmetic — the second initiative comes up short by 1 part in
  10¹⁷ and turns red. There's a test for exactly this.
- **Red consumes nothing** (R4), so the heatmap never lists a red initiative as
  a consumer of a cell.
- **The clock is injectable.** Settings has a current-month override so you can
  push time around and watch R7 and R8 behave; blank restores the real clock.
- **There's no decision log or approval flow.** The original specification had
  both. They were cut deliberately: this is a tool for thinking, not for
  governing.

## Tests

```sh
pip install -r requirements-dev.txt
pytest -q
```

284 tests in five suites:

- `tests/test_engine.py` — the specification's acceptance tests, T1 to T7,
  against the pure engine: baseline, drag-to-fit, fit hints, a displacement
  cascade, reserves exceeding supply, past months, and integer precision. Also
  the edge glows and the deadline, which share the drag shading's fit test.
- `tests/test_api.py` — the same scenarios through the HTTP API on a throwaway
  database, plus end months, every route past a deadline, undo, and migrating
  a database from before end months existed.
- `tests/test_import.py` — the CSV import, full dates, the optional `Deadline`
  column, all-or-nothing failure, and the demand-summary converter.
- `tests/test_security.py` — injection through every field the API accepts,
  path traversal, and input validation. See [SECURITY.md](SECURITY.md).
- `tests/test_frontend.py` — static guards on `static/app.js`, pinning the
  mistakes that have actually been made so they cannot be made again.

## Licence

Public domain, via [the Unlicense](LICENSE). No copyright is claimed and no
attribution is required — copy it, change it, sell it, do whatever you like.

It comes with no warranty, and given the warning at the top of this file, that
disclaimer is meant sincerely rather than as boilerplate.
