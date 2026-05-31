import asyncio
from pathlib import Path


from gateway.config import Platform
from gateway.run import GatewayRunner
from hermes_cli import kanban_db as kb


class RecordingAdapter:
    def __init__(self):
        self.sent = []

    async def send(self, chat_id, text, metadata=None):
        self.sent.append({"chat_id": chat_id, "text": text, "metadata": metadata or {}})


class DisconnectedAdapters(dict):
    """Expose a platform during collection, then simulate disconnect on get()."""

    def get(self, key, default=None):
        return None


async def _run_one_notifier_tick(monkeypatch, runner):
    real_sleep = asyncio.sleep

    async def fake_sleep(delay):
        if delay == 5:
            return None
        runner._running = False
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    await runner._kanban_notifier_watcher(interval=1)


def _make_runner(adapter):
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._kanban_sub_fail_counts = {}
    return runner


def _create_completed_subscription(summary="done once"):
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="notify once", assignee="worker")
        kb.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat-1")
        kb.complete_task(conn, tid, summary=summary)
        return tid
    finally:
        conn.close()


def _unseen_terminal_events(tid):
    conn = kb.connect()
    try:
        _, events = kb.unseen_events_for_sub(
            conn,
            task_id=tid,
            platform="telegram",
            chat_id="chat-1",
            kinds=["completed", "blocked", "gave_up", "crashed", "timed_out"],
        )
        return events
    finally:
        conn.close()


def test_kanban_notifier_dedupes_board_slugs_pointing_to_same_db(tmp_path, monkeypatch):
    db_path = tmp_path / "shared-kanban.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    kb.write_board_metadata("alias-a", name="Alias A")
    kb.write_board_metadata("alias-b", name="Alias B")

    tid = _create_completed_subscription()

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)

    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert len(adapter.sent) == 1
    assert "Kanban" in adapter.sent[0]["text"]
    assert tid in adapter.sent[0]["text"]


def test_kanban_notifier_claim_prevents_second_watcher_send(tmp_path, monkeypatch):
    db_path = tmp_path / "single-owner.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    tid = _create_completed_subscription()

    adapter1 = RecordingAdapter()
    adapter2 = RecordingAdapter()

    asyncio.run(_run_one_notifier_tick(monkeypatch, _make_runner(adapter1)))
    asyncio.run(_run_one_notifier_tick(monkeypatch, _make_runner(adapter2)))

    assert len(adapter1.sent) == 1
    assert adapter2.sent == []


def test_kanban_notifier_rewinds_claim_if_adapter_disconnects(tmp_path, monkeypatch):
    db_path = tmp_path / "adapter-disconnect.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    tid = _create_completed_subscription()

    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = DisconnectedAdapters({Platform.TELEGRAM: RecordingAdapter()})
    runner._kanban_sub_fail_counts = {}

    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert [ev.kind for ev in _unseen_terminal_events(tid)] == ["completed"]


def test_kanban_db_path_is_test_isolated_from_real_home():
    hermes_home = Path(kb.kanban_home())
    production_db = Path.home() / ".hermes" / "kanban.db"
    assert kb.kanban_db_path().resolve() != production_db.resolve()

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="x", assignee="worker")
        kb.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat-1")
    finally:
        conn.close()

    assert kb.kanban_db_path().resolve().is_relative_to(hermes_home.resolve())
    assert kb.kanban_db_path().resolve() != production_db.resolve()


class FailingAdapter:
    """Adapter whose send() always raises, simulating a transient send error."""

    def __init__(self):
        self.attempts = 0

    async def send(self, chat_id, text, metadata=None):
        self.attempts += 1
        raise RuntimeError("simulated send failure")


def test_kanban_notifier_rewinds_claim_on_send_exception(tmp_path, monkeypatch):
    """A raising adapter rewinds the claim so the next tick can retry.

    This is the second rewind path (distinct from the adapter-disconnect path
    in test_kanban_notifier_rewinds_claim_if_adapter_disconnects). Here the
    adapter is connected and the send call actually fires; the claim must
    still rewind so the event isn't lost when send() raises mid-tick.
    """
    db_path = tmp_path / "send-failure.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    tid = _create_completed_subscription()

    adapter = FailingAdapter()
    runner = _make_runner(adapter)

    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    # Send was attempted (so we exercised the failure path, not just the
    # disconnect path) and the claim was rewound — the unseen-events query
    # still returns the event for retry on the next tick.
    assert adapter.attempts >= 1, "send should have been attempted at least once"
    assert [ev.kind for ev in _unseen_terminal_events(tid)] == ["completed"]


def test_notifier_redelivers_same_kind_on_dispatch_cycle(tmp_path, monkeypatch):
    """A retry cycle (crashed → reclaimed → crashed) notifies the user twice.

    Before #21398 the notifier auto-unsubscribed on any terminal event kind
    (gave_up / crashed / timed_out), so the second crash in a respawn cycle
    silently dropped — the subscription was already gone. This test pins the
    new contract: subscription survives non-final terminal events; the
    cursor handles dedup.

    Two crashes ten seconds apart on the same task — both should land on
    the adapter.
    """
    db_path = tmp_path / "redeliver-cycle.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="cycle test", assignee="worker")
        kb.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat-1")
        # First crash — fired by the dispatcher when the worker PID dies.
        kb._append_event(conn, tid, kind="crashed")
    finally:
        conn.close()

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    # First crash delivered.
    assert len(adapter.sent) == 1
    assert "crashed" in adapter.sent[0]["text"].lower()

    # Subscription survives — the cursor advanced past event #1, but the
    # row is still there.
    conn = kb.connect()
    try:
        subs = kb.list_notify_subs(conn, tid)
        assert len(subs) == 1, (
            "Subscription must survive a crashed event so a respawn-cycle "
            "second crash also notifies the user (issue #21398)."
        )

        # Second crash — same task, same dispatcher (or a respawn). Append
        # another event to simulate the dispatcher firing crashed a second
        # time during retry.
        kb._append_event(conn, tid, kind="crashed")
    finally:
        conn.close()

    # New tick: the second event has a fresh id past the cursor advance,
    # so it gets claimed and delivered.
    runner = _make_runner(adapter)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert len(adapter.sent) == 2, (
        f"Second crashed event should also notify; got {len(adapter.sent)} "
        f"deliveries (texts: {[d['text'] for d in adapter.sent]})"
    )
    assert "crashed" in adapter.sent[1]["text"].lower()


# --------------------------------------------------------------------------- #
# S4: proactive board-health watcher (default-OFF owner DM on blocked boards)
# --------------------------------------------------------------------------- #

import importlib.util as _ilu
import pathlib as _pl

_crt_spec = _ilu.spec_from_file_location(
    "_crt_helpers_notifier",
    _pl.Path(__file__).resolve().parents[1]
    / "hermes_cli" / "test_kanban_contract_runtime.py",
)
_crt = _ilu.module_from_spec(_crt_spec)
_crt_spec.loader.exec_module(_crt)
_managed_contract = _crt._contract


def _blocked_managed_board(slug="health-blocked"):
    """A managed board left unapproved -> board_dispatch_gate is closed, and a
    real deduped dispatch_blocked signal is recorded (the same way the
    dispatcher's recompute_ready pass records it)."""
    kb.review_business_launch_contract(slug, contract=_managed_contract(), create_if_missing=True)
    with kb.connect(board=slug) as conn:
        promoted = kb.recompute_ready(conn)  # gate closed -> emits dispatch_blocked
        assert promoted == 0
        rows = conn.execute(
            "SELECT COUNT(*) AS n FROM board_signals WHERE primitive_kind='dispatch_blocked'",
        ).fetchone()
        assert rows["n"] >= 1
    return slug


def _make_health_runner(adapter):
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._board_health_seen = set()
    return runner


def test_board_health_collect_emits_one_card_then_dedupes(tmp_path, monkeypatch):
    """S4 EFFECT: a blocked managed board yields exactly ONE card on the first
    pass and ZERO on a second pass of the SAME state (no spam)."""
    db_path = tmp_path / "health-1.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    slug = _blocked_managed_board("health-blocked")

    runner = _make_health_runner(adapter=RecordingAdapter())
    seen = runner._board_health_seen

    first = runner._collect_board_health_cards(kb, seen)
    boards_in_first = {c["board"] for c in first if c["kind"] == "dispatch_blocked"}
    assert slug in boards_in_first, first
    # Exactly one dispatch_blocked card for this board.
    assert sum(1 for c in first if c["board"] == slug and c["kind"] == "dispatch_blocked") == 1

    # Second pass, identical board state => no new cards for this board.
    second = runner._collect_board_health_cards(kb, seen)
    assert all(c["board"] != slug for c in second), second


def test_board_health_watcher_flag_off_sends_zero(tmp_path, monkeypatch):
    """S4: flag OFF (unset) => the watcher exits immediately, ZERO sends."""
    db_path = tmp_path / "health-off.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.delenv("HERMES_BOARD_HEALTH_CARDS", raising=False)
    monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", "owner-1")
    kb.init_db()
    _blocked_managed_board("health-blocked-off")

    adapter = RecordingAdapter()
    runner = _make_health_runner(adapter)
    asyncio.run(runner._board_health_watcher(interval=5))
    assert adapter.sent == [], "flag-off watcher must send nothing"


def test_board_health_watcher_flag_on_sends_one_per_transition(tmp_path, monkeypatch):
    """S4: flag ON => exactly ONE owner DM per new blocked transition; a
    second tick of the same state sends zero more."""
    db_path = tmp_path / "health-on.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.setenv("HERMES_BOARD_HEALTH_CARDS", "1")
    monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", "owner-1")
    kb.init_db()
    slug = _blocked_managed_board("health-blocked-on")

    adapter = RecordingAdapter()
    runner = _make_health_runner(adapter)

    real_sleep = asyncio.sleep

    # First tick only: let the 5s warmup pass, run one loop body, then stop.
    async def fake_sleep_one(delay):
        if delay == 5:
            return None
        runner._running = False
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep_one)
    asyncio.run(runner._board_health_watcher(interval=5))

    sent_for_board = [d for d in adapter.sent if slug in d["text"]]
    assert len(sent_for_board) == 1, adapter.sent
    assert "blocked" in sent_for_board[0]["text"].lower()

    # Second tick, same state, same runner seen-set => no new send.
    runner._running = True
    before = len(adapter.sent)
    monkeypatch.setattr(asyncio, "sleep", fake_sleep_one)
    asyncio.run(runner._board_health_watcher(interval=5))
    new_for_board = [d for d in adapter.sent[before:] if slug in d["text"]]
    assert new_for_board == [], adapter.sent[before:]
