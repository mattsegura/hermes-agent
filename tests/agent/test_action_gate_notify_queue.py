"""Tests for the action-gate notification queue helpers + escalation heartbeat.

Covers the report-mode / non-breaking additions backing the inbound
Telegram approval round-trip (F1):

  * ``notified`` column migration (fresh + legacy DBs).
  * ``fetch_unnotified_pending`` / ``mark_notified`` dedup semantics used by
    the gateway action-gate card watcher.
  * ``_heartbeat_during_wait`` bridges to the kanban worker heartbeat so a
    long owner wait in the escalation poll loop doesn't trip the inactivity
    watchdog — and is a no-op when not a dispatcher-spawned worker.
"""

from __future__ import annotations

import importlib
import sqlite3
import time

import pytest


def _reload_action_gate(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from agent import action_gate as ag
    importlib.reload(ag)
    ag._QUEUE_DB_PATH = None
    return ag


def _insert_pending(ag, **overrides):
    conn = ag._ensure_queue_db()
    now = int(time.time())
    cur = conn.execute(
        """INSERT INTO pending_actions
           (profile, tool_name, tool_args, description, classification,
            status, created_at, expires_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            overrides.get("profile", "land-worker"),
            overrides.get("tool_name", "send_message"),
            overrides.get("tool_args", "{}"),
            overrides.get("description", "desc"),
            overrides.get("classification", "check"),
            overrides.get("status", "pending"),
            now,
            now + 300,
        ),
    )
    conn.commit()
    rid = cur.lastrowid
    conn.close()
    return rid


def test_fresh_db_has_notified_column(monkeypatch, tmp_path):
    ag = _reload_action_gate(monkeypatch, tmp_path)
    conn = ag._ensure_queue_db()
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(pending_actions)")}
    conn.close()
    assert "notified" in cols


def test_legacy_db_gets_notified_column_via_migration(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    dbp = tmp_path / "action_gate.db"
    c = sqlite3.connect(str(dbp))
    c.execute(
        """CREATE TABLE pending_actions (
            id INTEGER PRIMARY KEY AUTOINCREMENT, profile TEXT NOT NULL,
            tool_name TEXT NOT NULL, tool_args TEXT NOT NULL,
            description TEXT NOT NULL, classification TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending', decision TEXT,
            decided_by TEXT, decided_at INTEGER, created_at INTEGER NOT NULL,
            expires_at INTEGER NOT NULL, session_id TEXT, task_id TEXT)"""
    )
    now = int(time.time())
    c.execute(
        "INSERT INTO pending_actions (profile,tool_name,tool_args,description,"
        "classification,status,created_at,expires_at) VALUES "
        "('p','t','{}','d','check','pending',?,?)",
        (now, now + 300),
    )
    c.commit()
    c.close()

    from agent import action_gate as ag
    importlib.reload(ag)
    ag._QUEUE_DB_PATH = None
    conn = ag._ensure_queue_db()
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(pending_actions)")}
    conn.close()
    assert "notified" in cols
    # Legacy NULL notified treated as un-notified via COALESCE.
    rows = ag.fetch_unnotified_pending()
    assert len(rows) == 1


def test_fetch_unnotified_then_mark(monkeypatch, tmp_path):
    ag = _reload_action_gate(monkeypatch, tmp_path)
    rid = _insert_pending(ag, tool_name="terminal", description="run thing")

    rows = ag.fetch_unnotified_pending()
    assert len(rows) == 1
    assert rows[0]["id"] == rid
    assert rows[0]["tool_name"] == "terminal"
    assert rows[0]["description"] == "run thing"

    assert ag.mark_notified(rid) is True
    # Now excluded — no duplicate cards.
    assert ag.fetch_unnotified_pending() == []
    # Re-marking is idempotent in effect: the row stays notified=1 and is
    # still excluded from the unnotified feed (the watcher's dedup converges
    # on the feed being empty, not on this return value).
    ag.mark_notified(rid)
    assert ag.fetch_unnotified_pending() == []


def test_decided_rows_are_not_fetched(monkeypatch, tmp_path):
    ag = _reload_action_gate(monkeypatch, tmp_path)
    rid = _insert_pending(ag)
    assert ag.approve_action(rid) is True
    # Decided rows never appear in the unnotified-pending feed.
    assert ag.fetch_unnotified_pending() == []


def test_fetch_orders_oldest_first(monkeypatch, tmp_path):
    ag = _reload_action_gate(monkeypatch, tmp_path)
    first = _insert_pending(ag, description="first")
    second = _insert_pending(ag, description="second")
    rows = ag.fetch_unnotified_pending()
    ids = [r["id"] for r in rows]
    assert ids == [first, second]


def test_heartbeat_during_wait_is_noop_without_kanban_env(monkeypatch, tmp_path):
    ag = _reload_action_gate(monkeypatch, tmp_path)
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    # Should never raise, even with no kanban worker identity.
    ag._heartbeat_during_wait()


def test_heartbeat_during_wait_bridges_to_kanban(monkeypatch, tmp_path):
    ag = _reload_action_gate(monkeypatch, tmp_path)
    calls = []
    import tools.kanban_tools as kt

    monkeypatch.setattr(
        kt, "heartbeat_current_worker_from_env",
        lambda: calls.append(True) or True,
    )
    ag._heartbeat_during_wait()
    assert calls == [True]


def test_heartbeat_during_wait_swallows_errors(monkeypatch, tmp_path):
    ag = _reload_action_gate(monkeypatch, tmp_path)
    import tools.kanban_tools as kt

    def _boom():
        raise RuntimeError("db locked")

    monkeypatch.setattr(kt, "heartbeat_current_worker_from_env", _boom)
    # Must not propagate — the escalation poll loop relies on this.
    ag._heartbeat_during_wait()
