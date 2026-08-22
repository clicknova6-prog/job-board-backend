"""PostToolUse hook: run ruff on ONLY the file the tool just touched.

Claude Code and Codex both deliver the tool call to a hook as JSON on stdin;
there is no environment variable carrying the edited path (``CLAUDE_TOOL_INPUT``
does not exist). The path lives at ``tool_input.file_path``, so this script
parses stdin rather than reading the environment.

Previously both hook configs passed the repository root to ruff, which
reformatted every file in the repo on every single edit and buried real work in
unrelated churn. Scoping to one file keeps a hook run proportional to the edit
that triggered it.

Always exits 0: a formatting hook must never block the tool call that ran it.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

# Extensions ruff understands. Anything else (JSON, Markdown, TOML) is skipped.
RUFF_SUFFIXES = frozenset({".py", ".pyi", ".ipynb"})

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def _ruff_executable() -> Path | None:
    """Locate the project venv's ruff, preferring it over any ruff on PATH."""
    for relative in ("Scripts/ruff.exe", "bin/ruff"):
        candidate = REPOSITORY_ROOT / ".venv" / relative
        if candidate.exists():
            return candidate
    return None


def _target_path(payload: dict[str, object]) -> Path | None:
    """Return the file this tool call touched, or None if it is not ruff's job."""
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return None

    raw_path = tool_input.get("file_path")
    if not isinstance(raw_path, str) or not raw_path.strip():
        return None

    path = Path(raw_path)
    if not path.is_absolute():
        path = REPOSITORY_ROOT / path

    try:
        path = path.resolve()
    except OSError:
        return None

    if path.suffix.lower() not in RUFF_SUFFIXES:
        return None
    # An Edit may be followed by a delete, and a hook must not resurrect paths.
    if not path.is_file():
        return None
    # Never touch files outside this repository.
    if not path.is_relative_to(REPOSITORY_ROOT):
        return None

    return path


def main() -> int:
    """Format and lint-fix exactly one file, reporting anything ruff says."""
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0
    if not isinstance(payload, dict):
        return 0

    target = _target_path(payload)
    if target is None:
        return 0

    ruff = _ruff_executable()
    if ruff is None:
        print("ruff hook: .venv ruff not found; skipping", file=sys.stderr)
        return 0

    for arguments in (["format"], ["check", "--fix"]):
        completed = subprocess.run(
            [str(ruff), *arguments, str(target)],
            cwd=REPOSITORY_ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
        )
        output = completed.stdout.strip()
        if output:
            print(output)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
