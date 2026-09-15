"""`newdid` is appended, not prepended -- a house rule, consistently applied.

All 23 village-scoped loads in the 2026-09-15 traffic capture put the village
selector last, across four different page types::

    /karte.php?zoom=1&newdid=61837
    /build.php?id=30&gid=17&t=2&newdid=64215
    /dorf1.php?id=14&gid=1&as=ccI9Tbn0kPZtXJmk&newdid=30540

**The game's own markup emits the other order as well**, which the first version
of this file did not know and asserted the opposite of. From a live /dorf1.php,
same day, in the sidebar::

    /build.php?newdid=64215&id=39&&tt=1

So this is not a detection fix and the docstring no longer claims to be one.
Twenty-three observations of one form were never evidence that the other form
cannot occur, and the markup that settles it was a free read away.

The rule stays for the reason it should have been introduced with: four call
sites put `newdid` first and four put it last. That is not a decision, it is
drift, and drift is what makes a later real finding impossible to see. Matching
the form the observed traffic uses costs nothing and makes the codebase say one
thing.

A source scan rather than a behavioural test, because the failure mode is a NEW
call site written the other way, and no behavioural test covers a call site that
does not exist yet.
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
