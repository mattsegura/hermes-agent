from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

from hermes_cli import kanban_db as kb


def test_connect_initialization_is_thread_safe(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    db_path = kb.kanban_db_path(board="default")
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))

    errors: list[BaseException] = []
    barrier = threading.Barrier(8)

    def worker() -> None:
        try:
            barrier.wait(timeout=5)
            conn = kb.connect(board="default")
            conn.close()
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert errors == []
    with kb.connect(board="default") as conn:
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(tasks)")}
    assert "max_retries" in cols


def test_connect_opens_legacy_board_signals_without_dedupe_key(tmp_path, monkeypatch):
    """Opening a board whose board_signals predates the dedupe_key column must
    migrate cleanly rather than crash.

    Regression: the partial UNIQUE index over board_signals(board, dedupe_key)
    used to live in SCHEMA_SQL, which connect() runs via executescript() BEFORE
    the additive column migration. A legacy board_signals table lacks the
    dedupe_key column, so bootstrapping crashed with
    ``sqlite3.OperationalError: no such column: dedupe_key`` -- every older board
    became unopenable (e.g. ``kanban boards contract amendment status``). The
    index now lives in the migration alongside the column it depends on.
    """
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    kb.create_board("legacy", name="Legacy")
    db_path = kb.kanban_db_path(board="legacy")

    # Recreate board_signals as it looked before the dedupe_key migration:
    # no dedupe_key column and no partial UNIQUE index.
    raw = sqlite3.connect(db_path)
    try:
        raw.execute("DROP INDEX IF EXISTS idx_board_signals_dedupe")
        raw.execute("DROP TABLE board_signals")
        raw.execute(
            "CREATE TABLE board_signals ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, board TEXT NOT NULL, "
            "primitive_kind TEXT, primitive_key TEXT, entity_ref TEXT, "
            "action TEXT, ts INTEGER)"
        )
        raw.commit()
    finally:
        raw.close()

    # Force connect() to re-run schema bootstrap + migration on this path.
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))

    with kb.connect(board="legacy") as conn:
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(board_signals)")}
        idx = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND name='idx_board_signals_dedupe'"
        ).fetchone()

    assert "dedupe_key" in cols, "migration must add the dedupe_key column"
    assert idx is not None, "migration must (re)create the dedupe partial index"
