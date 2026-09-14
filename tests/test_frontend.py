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
