"""SQLite online backup, including committed WAL content. Output contains private user data."""

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from ..infrastructure.config import _load_local_env
from ..product.store import Store


def main():
    _load_local_env()
    store = Store()
    directory = Path(store.path).resolve().parent / "backups"
    directory.mkdir(exist_ok=True)
    name = datetime.now(timezone.utc).strftime("smartvoyage-%Y%m%dT%H%M%S%fZ.sqlite3")
    target = directory / name
    with store.connection() as source, sqlite3.connect(target) as destination:
        source.backup(destination)
    print(f"Private application backup saved: {target}")


if __name__ == "__main__":
    main()
