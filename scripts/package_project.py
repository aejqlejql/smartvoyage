"""Build a shareable source archive; never include local credentials, accounts or orders."""

import zipfile
from pathlib import Path


def main():
    project = Path(__file__).resolve().parents[1]
    target_dir = project / "artifacts"
    target_dir.mkdir(exist_ok=True)
    target = target_dir / "SmartVoyage-personal-project.zip"
    excluded = {
        ".git",
        ".idea",
        ".venv",
        "venv",
        "__pycache__",
        ".pytest_cache",
        ".ruff_cache",
        ".test-runs",
        "artifacts",
        "data",
        "logs",
        "node_modules",
    }
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        for file in sorted(project.rglob("*")):
            if not file.is_file() or any(part in excluded for part in file.relative_to(project).parts):
                continue
            if file.name.startswith(".env") or file.suffix in {".pyc", ".log", ".sqlite3", ".db"}:
                continue
            archive.write(file, str(Path(project.name) / file.relative_to(project)))
    print(f"Source package: {target}")
    print("Local .env files, runtime databases, logs and browser artifacts excluded.")


if __name__ == "__main__":
    main()
