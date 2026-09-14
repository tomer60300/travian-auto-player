"""`newdid` is appended, never prepended -- on every URL, from every caller.

All 23 village-scoped loads in the 2026-09-15 capture put the village selector
last, across four different page types::

    /karte.php?zoom=1&newdid=61837
    /build.php?id=30&gid=17&t=2&newdid=64215
    /dorf1.php?id=14&gid=1&as=ccI9Tbn0kPZtXJmk&newdid=30540

Which is what the game's own construction implies: the markup renders the link
for the page, and the village selector appends itself to whatever that link
already was. Leading with `newdid` is a shape that markup cannot produce.

Four call sites did it anyway -- `/build.php?newdid=123&id=30` -- while four
others put it last, so the codebase was not even internally consistent. The
parameters are order-independent to the SERVER, which is exactly why this was
free to get wrong: nothing breaks, nothing logs, and "same parameters, unusual
order" is the cheapest clustering feature there is and one of the very few that
survives every layer of timing noise underneath it.

A source scan rather than a behavioural test, deliberately. The failure mode is
a NEW call site written the old way, and no behavioural test covers a call site
that does not exist yet.
"""

import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "travian_api"

# `?newdid=` opens a query string, so anything after it is a parameter that
# should have come first. `&newdid=` followed by another parameter is the same
# mistake made mid-string.
_SELECTOR_NOT_LAST = re.compile(r"[?&]newdid=\{?[a-z_]*\}?\d*&")


def _offending_lines():
    for path in sorted(SRC.rglob("*.py")):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            # A comment line is where the mistake gets DESCRIBED, including in
            # the note that explains this rule. Only code can emit a URL.
            if line.lstrip().startswith("#"):
                continue
            if _SELECTOR_NOT_LAST.search(line):
                yield f"{path.relative_to(SRC)}:{number}: {line.strip()}"


def test_no_url_puts_a_parameter_after_the_village_selector():
    offenders = list(_offending_lines())
    assert not offenders, "the village selector must be appended last:\n" + "\n".join(offenders)


def test_the_check_would_catch_the_shape_it_was_written_for():
    """The regression this file exists for, as it was actually written."""
    assert _SELECTOR_NOT_LAST.search('f"/build.php?newdid={village_id}&id={slot_id}"')
    assert _SELECTOR_NOT_LAST.search('"/build.php?newdid=123&gid=16&tt=2"')


def test_the_check_passes_the_shape_the_game_emits():
    assert not _SELECTOR_NOT_LAST.search('f"/build.php?id={slot_id}&newdid={village_id}"')
    assert not _SELECTOR_NOT_LAST.search('"/karte.php?zoom=1&newdid=61837"')
    assert not _SELECTOR_NOT_LAST.search('f"/dorf1.php?newdid={village_id}"')
