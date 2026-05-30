"""Operator-friendly error boundary for the `hermes kanban` CLI.

An unexpected exception (e.g. a corrupt board DB raising
``sqlite3.DatabaseError``) must surface as a clear message + actionable hint +
nonzero exit -- never a raw Python traceback -- while ``--debug`` (or the
``HERMES_DEBUG`` env var) re-raises the full traceback for diagnosis.

Regression: the ``boards`` dispatch path returned before the only existing
try/except in kanban_command(), so `kanban boards contract amendment status`
against an older/corrupt board dumped a stack trace at the operator.
"""
from __future__ import annotations

import argparse
import sqlite3

import pytest

from hermes_cli import kanban as kb_cli


def test_debug_enabled_from_flag_and_env(monkeypatch):
    monkeypatch.delenv("HERMES_DEBUG", raising=False)
    assert kb_cli._kanban_debug_enabled(argparse.Namespace(debug=False)) is False
    assert kb_cli._kanban_debug_enabled(argparse.Namespace(debug=True)) is True

    plain = argparse.Namespace(debug=False)
    for falsy in ("", "0", "false", "no", "off"):
        monkeypatch.setenv("HERMES_DEBUG", falsy)
        assert kb_cli._kanban_debug_enabled(plain) is False
    for truthy in ("1", "true", "yes", "on"):
        monkeypatch.setenv("HERMES_DEBUG", truthy)
        assert kb_cli._kanban_debug_enabled(plain) is True


def test_operator_error_message_tailors_hint():
    locked = kb_cli._operator_error_message(
        "list", sqlite3.OperationalError("database is locked")
    )
    assert "locked by another process" in locked

    corrupt = kb_cli._operator_error_message(
        "boards", sqlite3.DatabaseError("file is not a database")
    )
    assert "doctor" in corrupt

    missing = kb_cli._operator_error_message("show", FileNotFoundError("nope"))
    assert "HERMES_HOME" in missing

    generic = kb_cli._operator_error_message("list", RuntimeError("boom"))
    assert "boom" in generic
    # Every rendering tells the operator how to get the full traceback.
    for msg in (locked, corrupt, missing, generic):
        assert "--debug" in msg


def test_boards_path_unexpected_error_is_wrapped(monkeypatch, capsys):
    """A crash inside the boards dispatch becomes a clean message + exit 1."""
    def boom(_args):
        raise sqlite3.DatabaseError("file is not a database")

    monkeypatch.setattr(kb_cli, "_dispatch_boards", boom)
    monkeypatch.delenv("HERMES_DEBUG", raising=False)

    args = argparse.Namespace(kanban_action="boards", debug=False)
    rc = kb_cli.kanban_command(args)

    assert rc == 1
    err = capsys.readouterr().err
    assert "unexpected error while running `kanban boards`" in err
    assert "doctor" in err  # tailored hint
    assert "Traceback" not in err  # NOT a raw traceback


def test_debug_flag_reraises_full_traceback(monkeypatch):
    def boom(_args):
        raise sqlite3.DatabaseError("file is not a database")

    monkeypatch.setattr(kb_cli, "_dispatch_boards", boom)

    args = argparse.Namespace(kanban_action="boards", debug=True)
    with pytest.raises(sqlite3.DatabaseError):
        kb_cli.kanban_command(args)


def test_keyboard_interrupt_is_not_swallowed(monkeypatch):
    def interrupt(_args):
        raise KeyboardInterrupt()

    monkeypatch.setattr(kb_cli, "_dispatch_boards", interrupt)
    args = argparse.Namespace(kanban_action="boards", debug=False)
    with pytest.raises(KeyboardInterrupt):
        kb_cli.kanban_command(args)
