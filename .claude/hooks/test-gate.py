#!/usr/bin/env python3
"""Stop hook test gate: blocks Claude from completing if tests fail.

Reads hook input from stdin. Runs pytest on the backend test suite.
Returns JSON with {"decision": "block", "reason": "..."} if tests fail,
or exits 0 silently if tests pass.

The stop_hook_active check prevents infinite loops when Claude tries
to fix failing tests triggered by this hook.
"""

import json
import subprocess
import sys
import os


# The command CLAUDE.md mandates, verbatim. Three parts of it are load-bearing:
#
# * `--extra dev --extra web` -- a BARE `uv run pytest` does not install the
#   optional extras, so it falls through to a global pytest whose editable
#   install may point at a different checkout. That has already produced test
#   results describing the wrong source tree.
# * `-n 8` -- every worker gets its own tmp DB, tmp trace dir, scrubbed env and
#   the live-writes pin, so they cannot collide. Serial is ~185s; -n 8 is ~60-85s.
# * no `-x` -- with xdist, `-x` stops the run at the first failure any worker
#   happens to reach, which hides the rest of the picture the gate exists to show.
PYTEST_COMMAND = [
    "uv",
    "run",
    "--extra",
    "dev",
    "--extra",
    "web",
    "pytest",
    "-q",
    "-n",
    "8",
    "--tb=short",
]

# Comfortably above the real runtime rather than under it. The previous 120s sat
# BELOW the ~185s serial runtime, so the gate blocked on its own timeout on every
# single stop and never once reported a test result. -n 8 measures 60-85s on this
# machine; 600s leaves room for a cold `uv sync` and a loaded machine.
TIMEOUT_SECONDS = 600


def main():
    # Read hook input from stdin
    try:
        hook_input = json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError):
        hook_input = {}

    # Prevent infinite loops: if the stop hook is already active, skip
    if hook_input.get("stop_hook_active"):
        sys.exit(0)

    # Find the project root (where pyproject.toml lives)
    project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

    # Check if we're in the right project
    if not os.path.exists(os.path.join(project_root, "pyproject.toml")):
        sys.exit(0)

    # Check if tests directory exists
    tests_dir = os.path.join(project_root, "tests")
    if not os.path.isdir(tests_dir):
        sys.exit(0)

    try:
        result = subprocess.run(
            PYTEST_COMMAND,
            capture_output=True,
            text=True,
            cwd=project_root,
            timeout=TIMEOUT_SECONDS,
        )
    except FileNotFoundError:
        # No silent pass and no second-best pytest. A gate that cannot run has
        # verified nothing, and the previous fallback to a bare `python -m
        # pytest` is the very command CLAUDE.md forbids: it resolves to
        # whatever pytest is on PATH, whose editable install may point at a
        # different checkout, so it can report green about another source tree.
        print(json.dumps({
            "decision": "block",
            "reason": (
                "The test gate could not run: `uv` is not on PATH. This project "
                "is verified only through `uv run --extra dev --extra web`; "
                "install uv or run the suite by hand before stopping."
            ),
        }))
        sys.exit(0)
    except subprocess.TimeoutExpired:
        print(json.dumps({
            "decision": "block",
            "reason": f"Test suite timed out after {TIMEOUT_SECONDS} seconds"
        }))
        sys.exit(0)

    if result.returncode != 0:
        # Truncate output to last 500 chars to fit in hook response
        output = result.stdout[-500:] if result.stdout else ""
        stderr = result.stderr[-200:] if result.stderr else ""
        reason = f"Tests failing:\n{output}"
        if stderr:
            reason += f"\n\nStderr:\n{stderr}"

        print(json.dumps({
            "decision": "block",
            "reason": reason
        }))

    # Tests passed — exit 0 silently
    sys.exit(0)


if __name__ == "__main__":
    main()
