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
