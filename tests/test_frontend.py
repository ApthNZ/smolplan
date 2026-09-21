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
