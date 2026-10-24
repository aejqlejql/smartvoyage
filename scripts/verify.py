"""Reproducible local checks; temporary databases stay inside a unique project folder."""

import argparse
import subprocess
import sys
import tempfile
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--product-only", action="store_true", help="Only test the lightweight product layer")
    args = parser.parse_args()
    project = Path(__file__).resolve().parents[1]
    scratch = project / ".test-runs"
    scratch.mkdir(exist_ok=True)
    commands = [
        [sys.executable, "-m", "ruff", "check", str(project)],
        [
            sys.executable,
            "-m",
            "compileall",
            "-q",
            "-x",
            r"[/\\](\.venv|\.test-runs|artifacts|data|logs)[/\\]",
            str(project),
        ],
    ]
    for command in commands:
        result = subprocess.run(command, cwd=project.parent)
        if result.returncode:
            return result.returncode
    # pytest may remove its basetemp; only hand it a newly-created, task-owned directory.
    with tempfile.TemporaryDirectory(prefix="verify-", dir=scratch) as owned:
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                str(project / "tests" / "test_product.py" if args.product_only else project / "tests"),
                "-q",
                "--basetemp",
                str(Path(owned) / "pytest"),
            ],
            cwd=project.parent,
        )
        return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
