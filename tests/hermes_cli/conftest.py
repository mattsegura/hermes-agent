"""Fixtures shared across hermes_cli kanban tests."""

from __future__ import annotations

import pytest


import sys


def _is_hermes_module(name: str) -> bool:
    return (
        name == "hermes_constants"
        or name.startswith("hermes_cli")
        or name.startswith("hermes_state")
    )


# Canonical hermes module objects captured before any test mutates
# ``sys.modules``. Populated by the session-scoped snapshot fixture below.
_ORIGINAL_HERMES_MODULES: dict[str, object] = {}


@pytest.fixture(scope="session", autouse=True)
def _snapshot_hermes_modules():
    """Capture the canonical hermes_cli/hermes_state/hermes_constants modules.

    Several kanban fixtures (``test_kanban_cli_dispatch_passthrough``,
    ``test_kanban_cross_business_priors``, and the upstream
    ``test_kanban_per_profile_cap`` / ``test_kanban_default_assignee``
    added by merge 3f11ae1f) reset the environment by deleting every
    ``hermes_cli*`` / ``hermes_state*`` / ``hermes_constants`` entry from
    ``sys.modules`` — but they never restore them. After such a nuke, a
    later ``from hermes_cli import ...`` builds a *new* module object while
    every test module's collection-time ``from hermes_cli import kanban_db
    as kb`` still references the *old* one. That split-brain has two
    symptoms in the post-merge tree:

      * ``mock.patch("hermes_cli.profiles.list_profiles")`` patches the new
        module while product code under test uses the old one (or vice
        versa) → patches silently no-op (e.g. decompose routes every
        assignee to ``default``).
      * The new ``connect()`` flock lives in the new module's ``_LOCK_FDS``;
        a re-entrant ``connect()`` through the old module opens a *second*
        fd on the same ``.kanban.lock`` and self-deadlocks on ``flock``
        (30s timeout) because the re-entrancy guard can't see the other
        module's fd.

    Snapshotting here (session start, after collection, before the first
    test body) records the canonical objects so the per-test restorer can
    undo the damage.
    """
    _ORIGINAL_HERMES_MODULES.clear()
    for name, mod in list(sys.modules.items()):
        if mod is not None and _is_hermes_module(name):
            _ORIGINAL_HERMES_MODULES[name] = mod
    yield


@pytest.fixture(autouse=True)
def _isolate_hermes_module_and_lock_state():
    """Undo per-test ``sys.modules`` nuking and release leaked DB locks.

    Runs after every test. First releases any cross-process kanban DB
    flocks the test leaked (the bare ``with kb.connect() as conn:`` idiom
    commits but does not release the lock — only ``connect_closing()`` /
    explicit ``.close()`` do), then restores the canonical hermes module
    objects so the next test's collection-time imports stay coherent.

    This is a TEST-ISOLATION fix only: it touches nothing in the product
    locking model and never weakens the cross-process single-owner
    guarantee. It only unwinds process-global state the test harness itself
    perturbed.
    """
    yield

    import os as _os

    # 1) Release locks held by whatever kanban_db module objects exist
    #    (there can be more than one after a sys.modules nuke).
    seen: set[int] = set()
    for mod_name, mod in list(sys.modules.items()):
        if mod is None or not mod_name.endswith("kanban_db"):
            continue
        if id(mod) in seen:
            continue
        seen.add(id(mod))
        lock_fds = getattr(mod, "_LOCK_FDS", None)
        if isinstance(lock_fds, dict) and lock_fds:
            fcntl = getattr(mod, "fcntl", None)
            for _resolved, fd in list(lock_fds.items()):
                try:
                    if fcntl is not None:
                        fcntl.flock(fd, fcntl.LOCK_UN)
                except OSError:
                    pass
                try:
                    _os.close(fd)
                except OSError:
                    pass
            lock_fds.clear()
        lock_refs = getattr(mod, "_LOCK_REFS", None)
        if isinstance(lock_refs, dict) and lock_refs:
            lock_refs.clear()
        initialized = getattr(mod, "_INITIALIZED_PATHS", None)
        if isinstance(initialized, set):
            initialized.clear()

    # 2) Restore the canonical hermes module graph. Delete every currently
    #    loaded hermes module (including reimported duplicates and any new
    #    submodules pulled in during the test), then reinstate the exact
    #    snapshot. A partial overwrite is not enough: leftover reimported
    #    submodules keep cross-references (e.g. kanban -> config,
    #    gateway -> hermes_cli) tangled across the old/new split. A full
    #    swap to the canonical set keeps the graph internally consistent.
    if _ORIGINAL_HERMES_MODULES:
        current = sys.modules.get("hermes_cli")
        if current is not _ORIGINAL_HERMES_MODULES.get("hermes_cli") or any(
            sys.modules.get(n) is not m for n, m in _ORIGINAL_HERMES_MODULES.items()
        ):
            for name in [n for n in sys.modules if _is_hermes_module(n)]:
                del sys.modules[name]
            sys.modules.update(_ORIGINAL_HERMES_MODULES)


@pytest.fixture
def all_assignees_spawnable(monkeypatch):
    """Pretend every assignee maps to a real Hermes profile.

    Most dispatcher tests use synthetic assignees ("alice", "bob") that
    don't correspond to actual profile directories on disk. Without this
    patch, the dispatcher's profile-exists guard (PR #20105) routes
    those tasks into ``skipped_nonspawnable`` instead of spawning, which
    would break tests that assert spawn behavior.
    """
    from hermes_cli import profiles
    monkeypatch.setattr(profiles, "profile_exists", lambda name: True)


@pytest.fixture(autouse=True)
def _suppress_concurrent_hermes_gate(request, monkeypatch):
    """Default ``_detect_concurrent_hermes_instances`` to ``[]`` for every test.

    The Windows update path now refuses to proceed when another
    ``hermes.exe`` is detected (issue #26670). On a developer's Windows
    machine running the test suite via ``hermes`` itself, this would
    flag the running agent as a concurrent instance and abort every
    ``cmd_update`` test. Tests that want to exercise the gate explicitly
    re-patch ``_detect_concurrent_hermes_instances`` with their own
    return value — autouse here gives a clean default without touching
    the rest of the suite.

    Tests that need to call the REAL function (e.g. unit tests for the
    helper itself) opt out with ``@pytest.mark.real_concurrent_gate``.
    """
    if request.node.get_closest_marker("real_concurrent_gate"):
        return
    try:
        from hermes_cli import main as _cli_main
    except Exception:
        return
    monkeypatch.setattr(
        _cli_main, "_detect_concurrent_hermes_instances", lambda *_a, **_k: []
    )
