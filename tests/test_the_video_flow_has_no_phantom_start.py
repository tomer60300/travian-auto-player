"""We do not POST /api/v1/videofeature/start, because the game does not.

Recorded end to end from a real watch, 2026-09-15, the operator watching an ad
for a building speed-up::

     +0.0s  GET  /api/v1/videofeature/open/buildingUpgrade   200
     +0.0s  GET  /js/en-US/videoFeature.json
     +0.1s  GET  /fallback/v1/request-ad?game_id=<uuid>      200
     +0.1s  GET  [ih.adscale.de]/map                         (iframe)
    +33.6s  POST /fallback/v1/reward                         200
     +0.2s  POST /api/v1/videofeature/ends                   200
     +0.0s  GET  /dorf2.php?id=22&gid=22&action=build&checksum=...

A complete, successful, rewarded watch -- and no `start` anywhere in it, nor in
the 1,631-request session captured the same day. So the endpoint is gone or
vestigial, and either way calling it is a request that identifies us: nothing a
browser does produces it, and it sits in the middle of the one flow the server
is most certainly watching, because it is the flow that gives away free
upgrades.

Removing it cannot break a claim that works today -- the recorded watch was
granted without it.

When this file was written, two further divergences were known and knowingly
unfixed -- `videofeature/open` being a GET we sent as a POST, and an ad provider
that had moved off ATG. A second capture, bodies included, closed both, and the
service was rebuilt on the recorded protocol. The module docstring there carries
it byte for byte.
"""

import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "travian_api"

# A path in a string literal, not a path in prose. The findings above are
# written down in several docstrings and comments, and describing a request is
# not making one.
_CALLS_START = re.compile(r"""["']/api/v1/videofeature/start["']""")


def _offenders():
    for path in sorted(SRC.rglob("*.py")):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if _CALLS_START.search(line):
                yield f"{path.relative_to(SRC)}:{number}: {line.strip()}"


def test_nothing_names_the_start_endpoint_as_a_url():
    offenders = list(_offenders())
    assert not offenders, "a real, granted watch never calls /videofeature/start:\n" + "\n".join(
        offenders
    )


def test_the_endpoints_the_flow_does_use_are_still_there():
    """The guard must not be satisfiable by deleting the whole feature."""
    from travian_api.constants import API_ENDPOINTS

    assert API_ENDPOINTS["video_open"] == "/api/v1/videofeature/open/buildingUpgrade"
    assert API_ENDPOINTS["video_end"] == "/api/v1/videofeature/ends"
    assert "video_start" not in API_ENDPOINTS


def test_the_claim_still_ends_the_video():
    source = (SRC / "services" / "video_reward_service.py").read_text(encoding="utf-8")

    assert '"/api/v1/videofeature/ends"' in source
    assert '"/api/v1/videofeature/open/' in source


def test_the_capture_is_written_down_where_the_next_reader_looks():
    """A finding nobody recorded is a finding that gets re-derived from scratch.

    When this file was written, two divergences were known-wrong and knowingly
    unfixed, and the docstring's job was to say so. A second recorded watch --
    bodies included -- closed both, so its job is now to carry the protocol
    itself: the module docstring holds the capture byte for byte, and it is the
    first thing anyone touching this flow reads.
    """
    from travian_api.services import video_reward_service

    doc = video_reward_service.__doc__ or ""
    assert "/fallback/v1/request-ad" in doc
    assert "conversionId" in doc and "signature" in doc
    assert "videofeature/start" in doc, "why the call is absent has to outlive the removal"
