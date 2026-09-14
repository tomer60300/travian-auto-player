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

Two divergences from that capture are deliberately NOT fixed, and this file
does not pretend otherwise:

* `videofeature/open` is a GET there and a POST here.
* The ad provider moved to `/fallback/v1/*` + ih.adscale.de; this service still
  talks to ATG's fc.php/xs.php.

Both are shape-level findings. The request and response BODIES were not
recorded, and rewriting a reward claim against guessed bodies is the risk this
work exists to remove. The class docstring carries the detail.
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


def test_the_known_divergences_are_written_down_where_the_next_reader_looks():
    """A finding nobody recorded is a finding that gets re-derived from scratch.

    These two are known-wrong and knowingly unfixed, so the docstring at the
    top of the service has to say so -- it is the first thing anyone touching
    this flow reads.
    """
    from travian_api.services.video_reward_service import VideoRewardService

    doc = VideoRewardService.__doc__ or ""
    assert "GET" in doc and "ih.adscale.de" in doc
    assert "fallback/v1" in doc
