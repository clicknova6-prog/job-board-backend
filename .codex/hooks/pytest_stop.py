"""Run pytest for the Codex Stop hook and emit only valid JSON on stdout."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

_MAX_FAILURE_OUTPUT_CHARS = 4_000


def main() -> int:
    """Run the test suite and report its outcome using the Stop hook schema."""
    repository_root = Path(__file__).resolve().parents[2]

    try:
        completed = subprocess.run(
            [sys.executable, "-m", "pytest", "-q"],
            cwd=repository_root,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
        )
        if completed.returncode == 0:
            payload: dict[str, object] = {}
        else:
            pytest_output = completed.stdout.strip()
            if len(pytest_output) > _MAX_FAILURE_OUTPUT_CHARS:
                pytest_output = pytest_output[-_MAX_FAILURE_OUTPUT_CHARS:]
                pytest_output = "... pytest output truncated ...\n" + pytest_output
            payload = {
                "continue": True,
                "systemMessage": (
                    f"pytest -q exited with code {completed.returncode}."
                    + (f"\n{pytest_output}" if pytest_output else "")
                ),
            }
    except (OSError, subprocess.SubprocessError) as error:
        payload = {
            "continue": True,
            "systemMessage": f"The pytest Stop hook could not run: {error}",
        }

    print(json.dumps(payload))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
