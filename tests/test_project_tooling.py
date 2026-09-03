"""The `.claude/` tooling this repo versions must be correct, not just present.

Versioning a broken gate is worse than not having one: a Stop hook that blocks
on its own timeout every single time, or runs a pytest belonging to a different
checkout, teaches everyone to ignore it. And an ignore rule that allowlists the
whole of `.claude/` puts every file a future tool drops in there on the next
commit -- in a public repo with a recorded credential exposure.

These are cheap file/`git` assertions rather than behaviour tests, in the same
spirit as `test_no_cli_loop_sleeps_on_a_bare_interval`: the thing being guarded
is a line of configuration a reviewer will not re-read.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_CLAUDE = _ROOT / ".claude"

# The command CLAUDE.md mandates for this project's suite.
_MANDATED_PYTEST = [
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


def _tracked(pathspec: str) -> list[str]:
    """Paths git tracks under *pathspec*, as forward-slash relative strings."""
    listing = subprocess.run(
        ["git", "ls-files", "--", pathspec],
        capture_output=True,
        text=True,
        cwd=_ROOT,
        check=True,
    ).stdout
    return [line.strip() for line in listing.splitlines() if line.strip()]


def _settings() -> dict:
    return json.loads((_CLAUDE / "settings.json").read_text(encoding="utf-8"))


def _hook_commands(event: str) -> list[str]:
    return [
        hook["command"]
        for group in _settings()["hooks"].get(event, [])
        for hook in group["hooks"]
        if hook.get("type") == "command"
    ]


def _test_gate():
    """The Stop hook module, imported from its hyphenated filename."""
    path = _CLAUDE / "hooks" / "test-gate.py"
    spec = importlib.util.spec_from_file_location("_test_gate_hook", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestTheStopHookRunsTheSuiteTheProjectActuallyUses:
    def test_it_runs_the_mandated_command(self):
        """A bare `uv run pytest` installs neither extra, so it falls through to
        whatever pytest is on PATH -- whose editable install may point at a
        different checkout. That has already produced results describing the
        wrong source tree, which is why CLAIMS about this suite are only
        trustworthy from this exact command."""
        assert _test_gate().PYTEST_COMMAND == _MANDATED_PYTEST

    def test_its_timeout_is_above_the_real_runtime(self):
        """The gate shipped with `timeout=120` against a ~185s serial suite, so
        it blocked on its own timeout on every stop and never once reported a
        test result. Parallel runs measure 60-85s here; the bound has to leave
        room for a cold `uv sync` on a loaded machine."""
        assert _test_gate().TIMEOUT_SECONDS >= 300

    def test_it_does_not_fall_back_to_another_pytest(self):
        """ "No fallbacks unless explicitly requested" -- and a fallback that
        silently reports on a different source tree is worse than a gate that
        says it could not run."""
        source = (_CLAUDE / "hooks" / "test-gate.py").read_text(encoding="utf-8")

        assert '"-m", "pytest"' not in source
        assert 'python", "-m"' not in source

    def test_the_interpreter_is_invoked_portably(self):
        """`python3` is not a command on a stock Windows Python install, and
        this project's only checkout is on Windows -- so the hook that was
        supposed to gate every stop may never have fired at all."""
        stop = _hook_commands("Stop")

        assert stop, "the Stop hook disappeared"
        for command in stop:
            assert "python3" not in command, command
        assert any("uv run python" in command for command in stop)


def _runnable_lines(path: Path) -> list[str]:
    """Lines a reader or a shell would actually EXECUTE.

    For Markdown that means fenced blocks only, and for Python the non-comment
    lines: the prose and the comments around both say why a bare
    `uv run pytest` is forbidden and necessarily quote it, so scanning whole
    files would flag the warning along with the mistake.
    """
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".py":
        return [line for line in text.splitlines() if not line.lstrip().startswith("#")]
    if path.suffix != ".md":
        return text.splitlines()
    lines, inside = [], False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            inside = not inside
        elif inside:
            lines.append(line)
    return lines


def test_no_versioned_tooling_runs_a_bare_uv_run_pytest():
    """Scanned over the TRACKED set, not the directory: `.claude/` also holds
    ux-audit output and disposable worktrees, each of which is a whole second
    checkout of this repo -- walking those would make the test read thousands
    of files and depend on which scratch state happens to exist locally."""
    offenders = []
    for name in _tracked(".claude/"):
        path = _ROOT / name
        if path.suffix not in {".md", ".py", ".json"}:
            continue
        offenders += [
            f"{name}: {line.strip()}" for line in _runnable_lines(path) if "uv run pytest" in line
        ]

    assert not offenders, (
        f"versioned tooling runs a bare `uv run pytest`: {offenders} -- it must "
        f"carry `--extra dev --extra web`"
    )


class TestTheClaudeDirectoryIsAllowlisted:
    """`.gitignore` must ignore `.claude/` and un-ignore named paths, not ignore
    named paths and admit everything else. Asserted through `git check-ignore`
    rather than by re-implementing gitignore precedence in the test."""

    def _ignored(self, *paths: str) -> set[str]:
        """`--no-index` is what makes this question about `.gitignore`.

        Without it `git check-ignore` short-circuits on the index: a TRACKED
        path is reported as not-ignored whatever the rules say, which is every
        one of the five allowlisted paths. Deleting all five `!` lines from a
        scratch copy of `.gitignore` left both tests below green, so the
        allowlist they exist to guard was never being read.
        """
        result = subprocess.run(
            ["git", "check-ignore", "--no-index", "--", *paths],
            capture_output=True,
            text=True,
            cwd=_ROOT,
        )
        assert result.returncode in (0, 1), result.stderr
        return {
            line.strip().replace("\\", "/") for line in result.stdout.splitlines() if line.strip()
        }

    def test_machine_local_and_unknown_paths_are_ignored(self):
        candidates = [
            ".claude/settings.local.json",
            ".claude/ux-audit/",
            ".claude/worktrees/",
            ".claude/foo.txt",
            ".claude/some-future-tool/state.json",
        ]

        ignored = self._ignored(*candidates)

        for candidate in candidates:
            assert candidate in ignored, (
                f"{candidate} is NOT ignored -- the rule is a denylist again, so the "
                f"next file any tool drops into .claude/ is committed by default"
            )

    def test_the_versioned_tooling_is_not_ignored(self):
        allowed = [
            ".claude/settings.json",
            ".claude/hooks/test-gate.py",
            ".claude/agents/ux-reviewer.md",
            ".claude/commands/ux-audit.md",
            ".claude/skills/tdd/SKILL.md",
        ]

        assert self._ignored(*allowed) == set(), (
            "a versioned tooling path is ignored; a fresh clone would lose it"
        )

    def test_every_allowlisted_path_still_exists(self):
        """An un-ignore for a path nobody maintains is a hole with no owner."""
        for name in ("settings.json", "hooks", "agents", "commands", "skills"):
            assert (_CLAUDE / name).exists(), name


class TestTheFormatterHookOnlyRunsToolsTheProjectHas:
    def test_prettier_is_not_shelled_out_to(self):
        """`prettier` is in neither frontend/package.json nor any config here,
        so the call was either a silent no-op or an ad-hoc download whose
        opinions fight eslint's. eslint --fix is the project's formatter."""
        commands = _hook_commands("PostToolUse")

        assert not any("prettier" in command for command in commands), commands
        assert any("eslint --fix" in command for command in commands)

    def test_prettier_is_indeed_absent_from_the_frontend(self):
        manifest = json.loads((_ROOT / "frontend" / "package.json").read_text(encoding="utf-8"))
        declared = {**manifest.get("dependencies", {}), **manifest.get("devDependencies", {})}

        assert not [name for name in declared if "prettier" in name], declared


def test_the_generic_provisioning_skill_is_not_versioned():
    """It never mentioned this project and told its reader to write
    `provision-state.json` into the repo root, which nothing ignores."""
    tracked = _tracked(".claude/skills/")

    assert not [name for name in tracked if "provision" in name], tracked
