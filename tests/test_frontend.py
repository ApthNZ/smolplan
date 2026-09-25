"""Static guards on the frontend.

There is no JavaScript test runner here, and adding one for a few hundred lines
of dependency-free code is not worth it. What is worth it is pinning the
mistakes that have actually been made, so they cannot be made again.
"""

import re
from pathlib import Path

APP_JS = Path(__file__).resolve().parents[1] / "static" / "app.js"


def test_nothing_calls_replace_children_directly():
    """`replaceChildren(null)` inserts the text "null" into the page.

    `el()` skips null children, so conditional content is written as
    `condition ? el(...) : null` all over this file — and passing that straight
    to replaceChildren renders the word "null" to the user. It has happened
    twice: once across the drawers, once in the converter's result panel.

    setChildren() filters the way el() does. Everything goes through it.
    """
    source = APP_JS.read_text()
    calls = re.findall(r"\.replaceChildren\(", source)

    # Exactly one: the call inside setChildren itself.
    assert len(calls) == 1, (
        f"{len(calls)} direct replaceChildren calls; use setChildren(node, ...) "
        "so null children are dropped rather than printed"
    )

    helper = re.search(
        r"function setChildren\(node, \.\.\.kids\) \{(.*?)\n\}", source, re.DOTALL
    )
    assert helper, "setChildren is missing"
    assert ".replaceChildren(" in helper.group(1), "the one allowed call is not the helper's"


def test_set_children_filters_null_and_false():
    """The filter is the whole point of the helper, so pin its shape."""
    source = APP_JS.read_text()
    helper = re.search(
        r"function setChildren\(node, \.\.\.kids\) \{(.*?)\n\}", source, re.DOTALL
    ).group(1)
    assert "filter(" in helper
    assert "!= null" in helper
    assert "!== false" in helper


def test_the_clipboard_fallback_is_still_there():
    """navigator.clipboard does not exist outside a secure context, and this
    app is normally served over plain HTTP on a LAN. Losing the fallback would
    break Copy everywhere except localhost, where it would still look fine."""
    source = APP_JS.read_text()
    assert "isSecureContext" in source
    assert "execCommand" in source


def test_the_column_width_is_set_on_the_root_not_the_plan():
    """The plan is rebuilt from scratch on every render, so a width written
    onto that node is lost the moment anything changes. `--side` is read by
    `.side` from the cascade, so setting it on documentElement outlives the
    re-render — and there is nothing to reapply afterwards."""
    source = APP_JS.read_text()
    assert 'document.documentElement.style.setProperty("--side"' in source

    setter = re.search(r"function setSideWidth\(.*?\n\}", source, re.DOTALL)
    assert setter, "setSideWidth is missing"
    assert ".plan" not in setter.group(0), "the width must not be written onto the plan node"


def test_the_resize_grip_is_wired_into_the_plan():
    source = APP_JS.read_text()
    assert "sideGrip()" in source
    plan = re.search(r'\{ class: "plan" \},(.*?)\);', source, re.DOTALL)
    assert plan and "sideGrip()" in plan.group(1), "the grip is not a child of the plan"


def test_the_remembered_width_is_clamped():
    """A width saved on a wide monitor must not hide the timeline on a laptop,
    so the clamp is applied on the way in as well as during the drag."""
    source = APP_JS.read_text()
    setter = re.search(r"function setSideWidth\(.*?\n\}", source, re.DOTALL).group(0)
    assert "Math.min" in setter and "Math.max" in setter
    assert "window.innerWidth" in setter
    assert re.search(r"function loadSideWidth\(.*?setSideWidth\(", source, re.DOTALL)


def test_every_localstorage_access_is_guarded():
    """A browser set to block site data throws on access rather than returning
    null, which would take the whole page down at load. Every read and write
    sits inside a try."""
    source = APP_JS.read_text()
    for match in re.finditer(r"localStorage\.(get|set)Item", source):
        before = source[: match.start()]
        assert before.rfind("try {") > before.rfind("\n}\n"), (
            f"unguarded localStorage access at offset {match.start()}"
        )


def test_every_month_column_carries_its_year():
    """A plan that spans two or three years is the normal case, and a column
    labelled only "Mar" means counting back to the last January to place it."""
    source = APP_JS.read_text()
    label = re.search(r"function monthLabel\(m\) \{(.*?)\n\}", source, re.DOTALL)
    assert label, "monthLabel is missing"
    body = label.group(1)
    assert "year.slice(2)" in body
    assert "Number(mo) === 1" not in body, "the year belongs on every month, not only January"


def test_the_timeline_header_sets_the_year_apart():
    """46px per column only fits month and year because the year is smaller and
    dimmer. Losing the .yr rule would not overflow, it would just go noisy."""
    source = APP_JS.read_text()
    assert "monthParts" in source
    assert 'el("i", { class: "yr" }' in source

    css = (APP_JS.parent / "app.css").read_text()
    assert ".tl-head .yr" in css


def test_the_shortfall_list_names_what_holds_the_capacity():
    """"GRC 2027-02: short 0.25" says a plan does not fit, not what it is
    competing with. The cause line is the answer, so pin its shape."""
    source = APP_JS.read_text()
    cause = re.search(r"function takenBy\(shortfall\) \{(.*?)\n\}", source, re.DOTALL)
    assert cause, "takenBy is missing"
    body = cause.group(1)
    assert "taken_by" in body
    assert "(reserve)" in body, "a reserve must read differently from an initiative"
    assert "no_supply_data" in body, "with no supply row there is no total to quote"
    assert "openShortfalls" in source and "takenBy(s)" in source


def test_ctrl_z_stands_aside_inside_a_text_box():
    """This page is mostly number boxes. Taking Ctrl+Z away from the field the
    cursor is in, to undo the plan instead, would be worse than having no
    shortcut: the keystroke would silently do something far away from where the
    user is looking, and the half-typed number would be beyond recall."""
    source = APP_JS.read_text()
    guard = re.search(r"function isTyping\(node\) \{(.*?)\n\}", source, re.DOTALL)
    assert guard, "isTyping is missing"
    body = guard.group(1)
    assert "input, textarea, select" in body
    assert "isContentEditable" in body

    handler = re.search(
        r'document\.addEventListener\("keydown", \(event\) => \{(.*?)\n\}\);',
        source,
        re.DOTALL,
    )
    assert handler, "the Ctrl+Z handler is missing"
    assert "isTyping(event.target)" in handler.group(1)
    # Shift+Ctrl+Z is redo, which does not exist here. Undoing on it would be a
    # surprise, not a convenience.
    assert "event.shiftKey" in handler.group(1)


def test_the_undo_button_exists_and_says_how_far_back_it_reaches():
    """Ctrl+Z is invisible. The button is the only thing that says undo exists,
    and the count is the only thing that says whether it reaches past the last
    change."""
    source = APP_JS.read_text()
    html = (APP_JS.parent / "index.html").read_text()
    assert 'id="undo"' in html, "no undo button in the header"

    render = re.search(r"function renderUndo\(\) \{(.*?)\n\}", source, re.DOTALL)
    assert render, "renderUndo is missing"
    body = render.group(1)
    assert "state.undo" in body
    assert "depth" in body and "label" in body
    assert "button.disabled" in body, "an undo button that cannot undo must say so"
    assert "renderUndo()" in re.search(
        r"function render\(\) \{(.*?)\n\}", source, re.DOTALL
    ).group(1), "renderUndo is never called from render"


def test_the_editor_folds_its_second_request_into_one_undo():
    """Saving an initiative is a create-or-patch and then a demand
    replacement — two requests for one button. Without amend, undoing that
    button takes two presses, and the first one leaves a half-saved
    initiative on screen."""
    source = APP_JS.read_text()
    assert "/demand?amend=1" in source, "the editor's demand save is not amended"


def test_the_export_panel_shows_what_the_server_rendered():
    """Rebuilding the CSV here would be a second definition of what an export
    is, and the two would drift over exactly the details that matter — which
    rows are left out, how a leading "=" is escaped."""
    source = APP_JS.read_text()
    panel = re.search(r"function exportPanel\(\) \{(.*?)\n\}", source, re.DOTALL)
    assert panel, "exportPanel is missing"
    body = panel.group(1)
    assert "/api/export.csv" in body, "the panel does not fetch the server's export"
    assert "InitiativeName" not in body, "the panel is building its own CSV"


def test_replacing_the_plan_goes_through_one_place():
    """The export preview is fetched from the server and kept, so it has to be
    dropped whenever the plan it was built from moves on. Six call sites
    replace `state`; remembering at all six is how a stale preview ships. They
    all go through setState, and only setState assigns."""
    source = APP_JS.read_text()
    setters = re.findall(r"^\s*state = .*$", source, re.MULTILINE)
    assert setters == ["  state = next;"], (
        f"state is assigned outside setState: {setters}"
    )

    helper = re.search(r"function setState\(next\) \{(.*?)\n\}", source, re.DOTALL)
    assert helper, "setState is missing"
    assert "exportCsv = null" in helper.group(1), (
        "setState does not drop the cached export"
    )


def _function(source, name):
    """The body of a top-level `function name(...) {`, up to its closing brace."""
    match = re.search(rf"^function {name}\(.*?^\}}", source, re.DOTALL | re.MULTILINE)
    assert match, f"{name} is missing"
    return match.group(0)


def test_the_editor_grid_is_as_long_as_the_initiative():
    """The grid used to open at a guess of six to twenty-four columns and grow
    three at a time. Now it is the duration, and Save sends only the columns on
    screen — so any cap on the column count would quietly delete every month
    of demand past it. The "+3 months" button is gone because the end month
    replaced it."""
    source = APP_JS.read_text()
    editor = _function(source, "openEditor")
    assert '"+3 months"' not in source
    assert "let columns = data.length;" in editor, "the grid is not sized by the duration"
    assert not re.search(r"columns = Math\.min\(\d+", editor), "the grid has a column cap"


def test_save_sends_the_dates_and_only_the_columns_on_screen():
    """Values typed into columns that a shorter end then cut off are kept, so
    moving the end back out restores them — but they must not be sent, or the
    demand PUT is refused for running past the end."""
    source = APP_JS.read_text()
    editor = _function(source, "openEditor")
    save = re.search(r"async function save\(\) \{(.*?)\n  \}", editor, re.DOTALL)
    assert save, "the editor's save is missing"
    body = save.group(1)
    assert ".filter((line) => line.offset < columns)" in body
    assert "end_month: end()" in body
    assert "deadline_month: deadline" in body, "a cleared deadline must be sent as null"
    assert body.count("...dates") == 2, "create and edit must both send the dates"


def test_the_deadline_clamp_is_worked_out_from_the_length_asked_for():
    """The clamp used to take months off `columns` for good. A start moved
    into the deadline and then moved back left the initiative shorter, the
    note gone, and Save writing the shorter end; stepping the start with the
    arrow keys reported one month cut when three had gone. The length asked
    for is kept apart, and only the End list changes it."""
    editor = _function(APP_JS.read_text(), "openEditor")
    clamp = re.search(r"function clampToDeadline\(\) \{(.*?)\n  \}", editor, re.DOTALL)
    assert clamp, "clampToDeadline is missing"
    body = clamp.group(1)
    assert "columns -=" not in body, "the clamp is one-way again"
    assert "Math.min(wanted," in body and "clamped = wanted - columns" in body

    end_change = re.search(r'endSelect\.addEventListener\("change", \(\) => \{(.*?)\}\);', editor, re.DOTALL)
    assert end_change and "wanted = columns =" in end_change.group(1)
    for other in ("startSelect", "deadlineSelect"):
        handler = re.search(rf'{other}\.addEventListener\("change", \(\) => \{{(.*?)\}}\);', editor, re.DOTALL)
        assert handler and "wanted" not in handler.group(1), f"{other} must not change the length asked for"


def test_the_note_never_advises_moving_the_end_past_the_deadline():
    """The End list stops at the deadline, so with the end on it "move the end
    month out again" is advice the form refuses to let anyone take."""
    editor = _function(APP_JS.read_text(), "openEditor")
    note = re.search(r"function drawNote\(\) \{(.*?)\n  \}", editor, re.DOTALL)
    assert note, "drawNote is missing"
    body = note.group(1)
    assert "mIndex(end()) >= mIndex(deadline)" in body
    assert "move the deadline out to keep it" in body
    assert "move the deadline out, then the end month" in body
    assert "${advice}.`" in body, "the advice is not chosen by the deadline"


def test_a_rerender_keeps_the_timeline_where_it_was_scrolled():
    """render() replaces the timeline's scroller, and a new one starts at the
    first month. The search re-renders on every keystroke, so a plan scrolled
    out to next year jumped back to this month at each letter typed."""
    body = _function(APP_JS.read_text(), "render")
    assert '$("#view .tl-scroll")' in body
    read = body.index(".scrollLeft")
    rebuild = body.index('setChildren($("#view")')
    assert read < rebuild < body.index("scroller.scrollLeft = scrolled"), (
        "the scroll has to be read off the old scroller and set on the new one"
    )


def test_a_bar_off_the_window_is_not_drawn_as_ended():
    """With the month window narrowed, work after it — or before it and still
    running — used to be drawn as grey "ended", titled "Ended before the
    current month": untrue, and a red initiative lost its colour with it."""
    row = _function(APP_JS.read_text(), "timelineRow")
    branch = re.search(r"if \(cells <= 0\) \{(.*?)\n    return row;\n  \}", row, re.DOTALL)
    assert branch, "the off-screen branch is missing"
    body = branch.group(1)
    assert "const later = offset >= months.length;" in body
    assert 'class: `bar ${initiative.status}`' in body, "the chip must keep the status colour"
    assert 'const ended = initiative.status === "past";' in body
    assert re.search(r'ended\s*\? "Ended before the current month"', body)
    assert '"later"' in body and '"earlier"' in body


def test_month_dropdowns_are_labelled_with_names():
    """A dropdown is read, so it shows "Sep 2026"; its value stays YYYY-MM,
    because that is what the API takes. One helper builds every month list so
    the two cannot come apart."""
    source = APP_JS.read_text()
    helper = _function(source, "monthOptions")
    assert "{ value: m" in helper and "monthLong(m)" in helper

    editor = _function(source, "openEditor")
    for select in ("startSelect", "endSelect", "deadlineSelect"):
        assert re.search(rf"setChildren\(\s*{select},[^;]*monthOptions\(", editor), (
            f"{select} is not built from monthOptions"
        )
    assert "monthOptions(all, chosen)" in _function(source, "renderRange")

    # An option whose text is the raw month string is the mistake itself.
    assert not re.search(r'el\("option",[^\n]*\},\s*[\w.]*(?:\bm|month)\)', source)


def test_no_month_is_shown_to_a_person_as_yyyy_mm():
    """Every month a person reads goes through monthLong, monthLabel or
    monthParts. The sweep that introduced month names found six places quoting
    the raw string — the header clock, drawer headings, tooltips, the
    shortfall list — and each looked fine on its own."""
    source = APP_JS.read_text()
    raw = []
    for match in re.finditer(r"\$\{[\w.]*month\}", source, re.IGNORECASE):
        line = source[source.rfind("\n", 0, match.start()) : source.find("\n", match.end())]
        if "state.cells[" in line:
            continue  # the cells key, which is the API's format, not text
        raw.append(line.strip())
    # A bare month handed to el() as a child is the other way to print one.
    raw += re.findall(r"el\([^;\n]*,\s*[\w.]*(?:_month|\.month)\)", source)
    assert not raw, f"months shown as YYYY-MM: {raw}"


def test_search_keeps_focus_and_caret_across_the_rerender():
    """render() rebuilds the whole view, input box included. Without putting
    the focus and the caret back, each keystroke would leave the cursor
    nowhere and the second letter would be lost."""
    source = APP_JS.read_text()
    body = _function(source, "setSearch")
    for step in ("render()", ".focus()", "setSelectionRange("):
        assert step in body, f"setSearch no longer calls {step}"
    assert body.index("render()") < body.index(".focus()") < body.index("setSelectionRange("), (
        "the focus has to be restored after the rebuild, on the new box"
    )

    box = _function(source, "searchBox")
    assert 'type: "search"' in box
    assert "selectionStart" in box and "selectionEnd" in box
    assert "searchBox()" in _function(source, "renderPortfolio")


def test_escape_in_the_search_box_does_one_thing_at_a_time():
    """Escape clears the search, and Escape closes the drawer. With text in
    the box, the press is the search's and must not also reach the document
    and close the drawer; with the box empty, it must reach the document, or
    the drawer can no longer be closed from there."""
    source = APP_JS.read_text()
    box = _function(source, "searchBox")
    keydown = re.search(r"onkeydown: \(e\) => \{(.*?)\n    \},", box, re.DOTALL)
    assert keydown, "the search box has no Escape handling"
    body = keydown.group(1)
    assert '"Escape"' in body
    assert "!e.target.value) return" in body, "an empty box must let Escape through"
    assert body.index("return") < body.index("stopPropagation()")
    assert 'e.key === "Escape" && closeDrawer()' in source, "the drawer's Escape is gone"


def test_search_hides_initiatives_and_nothing_else():
    """Reserves and the capacity rows belong to teams, not to an initiative.
    Filtering them by an initiative's name would make the supply figures
    change as you type, which reads as the plan changing."""
    source = APP_JS.read_text()
    portfolio = _function(source, "renderPortfolio")
    visible = re.search(r"const visible = state\.initiatives\.filter\((.*?)\);", portfolio, re.DOTALL)
    assert visible and "matchesSearch(i)" in visible.group(1)
    reserves = re.search(r"const reserves = state\.reserves\.filter\((.*?)\);", portfolio, re.DOTALL)
    assert reserves and "earch" not in reserves.group(1)
    assert "No initiative matches" in portfolio

    match = re.search(r"const matchesSearch = .*?;\n", source, re.DOTALL)
    assert match and "reference" in match.group(0), "the reference is searched as well as the name"


def test_glows_skip_clipped_edges_and_are_said_in_words():
    """A bar that starts before the first month on screen has no start edge
    to light, and a glow there would sit on the edge of the window instead.
    And colour is never the only signal: the tooltip and the drawer say what
    each glow says."""
    source = APP_JS.read_text()
    row = _function(source, "timelineRow")
    assert "const startShown = offset >= 0;" in row
    assert "const endShown = offset + span <= months.length;" in row
    assert re.search(r"startShown && initiative\.earliest_start \? \"glow-start\"", row)
    assert re.search(r"endShown && initiative\.end_limit === \"deadline\" \? \"glow-deadline\"", row)
    assert re.search(r"endShown && initiative\.end_limit === \"capacity\" \? \"glow-capacity\"", row)

    assert "edgeNotes(initiative)" in _function(source, "barTitle")
    assert "edgeNotes(initiative)" in _function(source, "openShortfalls")

    css = (APP_JS.parent / "app.css").read_text()
    for glow in ("glow-start", "glow-capacity", "glow-deadline"):
        assert f".{glow} {{" in css, f"no rule for .{glow}"
        assert css.count(f"--{glow}:") == 2, f"--{glow} needs a light and a dark value"


def test_a_bar_tooltip_names_its_months():
    """On a long list the month header is a long way up from a bar, so the
    tooltip of any bar that is not red says where it sits: "A — Sep 2026 to
    Feb 2027", or "A — Sep 2026" for one month. A red bar keeps its shortfall
    teams, an archived one says so, and the glow sentences go on their own
    lines rather than running on after a full stop."""
    source = APP_JS.read_text()
    span = re.search(r"const monthSpan = .*?;\n", source, re.DOTALL)
    assert span, "monthSpan is missing"
    assert "start === end ? monthLong(start)" in span.group(0), "one month is said once"
    assert "${monthLong(start)} to ${monthLong(end)}" in span.group(0)

    title = _function(source, "barTitle")
    assert "monthSpan(initiative.start_month, initiative.end_month)" in title
    assert "shortfalls in: ${teams.join" in title
    assert "— archived" in title
    assert '...edgeNotes(initiative)].join("\\n")' in title
    assert "drag to move the start" not in title


def test_a_drag_stops_at_the_deadline():
    """The server refuses an end past the deadline, but a bar that follows the
    pointer there first promises a drop that only bounces back with an error."""
    source = APP_JS.read_text()
    drag = _function(source, "startBarDrag")
    assert re.search(r"const latest = initiative\.deadline_month", drag)
    assert re.search(r"target = Math\.max\(0, Math\.min\([^;]*latest\)\)", drag)
    assert "target > latest" in drag, "a bar with nowhere legal to go must not be sent"


def test_the_deadline_tick_sits_under_its_bar():
    """The tick is drawn at the end of the deadline month, which is exactly
    where a bar on its deadline ends. Appended after the bar it would paint on
    top and take the pointer, and a drag started on the bar's end would grab
    the tick instead."""
    source = APP_JS.read_text()
    row = _function(source, "timelineRow")
    assert "deadline-tick" in row
    assert row.index("deadline-tick") < row.index("row.append(bar)")
    assert "Deadline: ${monthLong(deadline)}" in row
    assert "!initiative.archived" in row and 'initiative.status !== "past"' in row


def test_the_rules_are_numbered_the_same_in_both_copies():
    """The rules live in two places, the Rules tab and the README, phrased for
    different readers on purpose. A rule added to one and not the other is the
    mistake that arrangement invites."""
    source = APP_JS.read_text()
    readme = (APP_JS.parents[1] / "README.md").read_text()
    in_app = re.findall(r'^  \["(R\d+)", ', source, re.MULTILINE)
    in_readme = re.findall(r"^- \*\*(R\d+)\*\*", readme, re.MULTILINE)
    assert in_app == [f"R{n}" for n in range(1, len(in_app) + 1)], in_app
    assert in_app == in_readme, f"Rules tab has {in_app}, README has {in_readme}"


def test_dark_theme_overrides_come_after_what_they_override():
    """A dark block has the same specificity as the light rule it overrides,
    so source order decides — and a dark block placed before its light rule
    silently loses. That has shipped twice: the team tags, then the capacity
    ramp, where dark mode kept pale backgrounds under light text."""
    css = (APP_JS.parent / "app.css").read_text()
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)
    opener = "@media (prefers-color-scheme: dark) {"

    blocks = []
    for match in re.finditer(re.escape(opener), css):
        depth, at = 1, match.end()
        while depth:
            depth += {"{": 1, "}": -1}.get(css[at], 0)
            at += 1
        blocks.append((match.start(), at, css[match.end() : at - 1]))
    assert blocks, "no dark-theme blocks found"

    def light_rule_before(selector, position):
        for rule in re.finditer(r"([^{}]+)\{[^{}]*\}", css[:position]):
            if any(start <= rule.start() < end for start, end, _ in blocks):
                continue
            if selector in (s.strip() for s in rule.group(1).split(",")):
                return True
        return False

    for start, _, body in blocks:
        for rule in re.finditer(r"([^{}]+)\{[^{}]*\}", body):
            for selector in (s.strip() for s in rule.group(1).split(",")):
                assert light_rule_before(selector, start), (
                    f"dark override for {selector!r} comes before its light rule"
                )


def test_the_editor_uses_the_id_the_server_made():
    """The new initiative's id was taken as the highest id in the plan. With a
    second tab open, that can be someone else's new initiative, and the demand
    grid was written to it."""
    source = APP_JS.read_text()
    save = source[source.index("async function save()"):]
    save = save[:save.index("\n  }\n")]
    assert "created.created_id" in save
    assert "Math.max" not in save


def test_a_retried_save_does_not_create_twice():
    """If the create lands and the demand after it fails, the retry must patch
    the row already made, not make a second."""
    source = APP_JS.read_text()
    assert "let createdId = null;" in source
    assert "initiative ? initiative.id : createdId" in source


def test_styles_are_set_through_the_cssom_not_as_attributes():
    """The CSP refuses inline style attributes. `el()` routes `style` through
    `node.style.cssText`, which a CSP does not govern; anything that sets the
    attribute directly would render unstyled with only a console warning."""
    source = APP_JS.read_text()
    assert 'key === "style") node.style.cssText = value' in source
    assert not re.search(r"setAttribute\(\s*[\"']style", source)
    assert "<script>" not in (APP_JS.parent / "index.html").read_text()
    assert not re.search(r"\son\w+=", (APP_JS.parent / "index.html").read_text()), \
        "inline handlers are inline script"
