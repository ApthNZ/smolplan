"""Text as the plan stores it: what is stripped from a name, and why.

Names travel further than the page. The page renders them with textContent, so
nothing in a name can become markup there — but the same names go out in
`/api/export.csv`, into a spreadsheet and sometimes straight into a terminal
with curl. Two kinds of character do damage on that trip:

- **Controls** (NUL, ESC and the rest of Cc). An ANSI escape in a team name is
  live the moment the export is printed to a terminal.
- **Bidirectional overrides and isolates.** They exist to make text read in an
  order other than the one it is stored in, which in a cell of a spreadsheet is
  a way to make a name say something it does not.

Both are stripped rather than rejected: something pasted from elsewhere should
lose what a name cannot carry, not fail. The rule is smoltask's `clean_line`,
kept the same on purpose.
"""

from __future__ import annotations

import re
import unicodedata

BIDI_CONTROLS = frozenset(chr(c) for c in (*range(0x202A, 0x202F), *range(0x2066, 0x206A)))

# Controls (Cc), lone surrogates (Cs) and private use (Co). Deliberately not all
# of category C: format characters (Cf) include the zero-width joiner a family
# emoji is held together by, and unassigned code points (Cn) are unassigned only
# in *this* Python's tables — a newer emoji would vanish on an older interpreter.
STRIPPED_CATEGORIES = frozenset({"Cc", "Cs", "Co"})


def _kept(ch: str) -> bool:
    return unicodedata.category(ch) not in STRIPPED_CATEGORIES and ch not in BIDI_CONTROLS


def clean_line(raw: str | None) -> str:
    """One line: controls stripped, tabs and newlines become spaces, runs of
    whitespace collapse to one, and the ends are trimmed."""
    text = "".join(" " if ch in "\t\n\r" else ch for ch in (raw or "")
                   if ch in "\t\n\r" or _kept(ch))
    return re.sub(r"\s+", " ", text).strip()


def clean_text(raw: str | None) -> str:
    """Several lines, for notes: the same stripping, but a newline is kept as a
    newline (CRLF and CR normalised to it) and tabs survive."""
    text = (raw or "").replace("\r\n", "\n").replace("\r", "\n")
    return "".join(ch for ch in text if ch in "\t\n" or _kept(ch)).strip()
