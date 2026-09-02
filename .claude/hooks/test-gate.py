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
            ["uv", "run", "pytest", "-x", "--tb=short", "-q"],
            capture_output=True,
            text=True,
            cwd=project_root,
            timeout=120,
        )
    except FileNotFoundError:
        # uv not available, try plain pytest
        try:
            result = subprocess.run(
                ["python", "-m", "pytest", "-x", "--tb=short", "-q"],
                capture_output=True,
                text=True,
                cwd=project_root,
                timeout=120,
            )
        except FileNotFoundError:
            # No pytest available, skip
            sys.exit(0)
    except subprocess.TimeoutExpired:
        print(json.dumps({
            "decision": "block",
            "reason": "Test suite timed out after 120 seconds"
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
