"""Tests for the inbound Telegram action-gate approval round-trip.

Covers the F1 crux:
  * ``TelegramAdapter.send_action_gate_card`` renders an inline-keyboard
    card with ``ag:approve:<id>`` / ``ag:deny:<id>`` callback data.
  * The ``ag:`` branch in ``_handle_callback_query`` gates on owner-only
    authorization, then flips the ``pending_actions`` row via
    ``agent.action_gate.approve_action`` / ``deny_action`` and edits the
    card to show the decision.

Mirrors test_telegram_clarify_buttons.py for the Telegram mock + adapter
fixture. The action_gate DB is redirected to a temp HERMES_HOME so these
tests never touch the live ~/.hermes/action_gate.db.
"""

import os
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Ensure the repo root is importable
# ---------------------------------------------------------------------------
_repo = str(Path(__file__).resolve().parents[2])
if _repo not in sys.path:
    sys.path.insert(0, _repo)


# ---------------------------------------------------------------------------
# Minimal Telegram mock so TelegramAdapter can be imported (mirrors
# test_telegram_clarify_buttons.py)
# ---------------------------------------------------------------------------
def _ensure_telegram_mock():
    if "telegram" in sys.modules and hasattr(sys.modules["telegram"], "__file__"):
        return

    mod = MagicMock()
    mod.ext.ContextTypes.DEFAULT_TYPE = type(None)
    mod.constants.ParseMode.MARKDOWN = "Markdown"
    mod.constants.ParseMode.MARKDOWN_V2 = "MarkdownV2"
    mod.constants.ParseMode.HTML = "HTML"
    mod.constants.ChatType.PRIVATE = "private"
    mod.constants.ChatType.GROUP = "group"
    mod.constants.ChatType.SUPERGROUP = "supergroup"
    mod.constants.ChatType.CHANNEL = "channel"
    mod.error.NetworkError = type("NetworkError", (OSError,), {})
    mod.error.TimedOut = type("TimedOut", (OSError,), {})
    mod.error.BadRequest = type("BadRequest", (Exception,), {})

    for name in ("telegram", "telegram.ext", "telegram.constants", "telegram.request"):
        sys.modules.setdefault(name, mod)
    sys.modules.setdefault("telegram.error", mod.error)


_ensure_telegram_mock()

import gateway.platforms.telegram as tg
from gateway.platforms.telegram import TelegramAdapter
from gateway.config import PlatformConfig


class _FakeButton:
    """Lightweight stand-in so tests can read callback_data off a button.

    The shared Telegram mock turns InlineKeyboardButton into a MagicMock,
    which loses the callback_data attribute. These minimal classes let the
    render test assert on the real callback payload (ag:approve:<id> etc.).
    """

    def __init__(self, text, callback_data=None, **kw):
        self.text = text
        self.callback_data = callback_data


class _FakeMarkup:
    def __init__(self, inline_keyboard):
        self.inline_keyboard = inline_keyboard


def _make_adapter(extra=None):
    config = PlatformConfig(enabled=True, token="test-token", extra=extra or {})
    adapter = TelegramAdapter(config)
    adapter._bot = AsyncMock()
    adapter._app = MagicMock()
    return adapter


@pytest.fixture()
def gate(tmp_path, monkeypatch):
    """Redirect the action_gate DB into a temp HERMES_HOME and reset module state."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    import importlib
    from agent import action_gate as ag
    importlib.reload(ag)
    # Ensure the cached path picks up the temp HERMES_HOME for this test.
    ag._QUEUE_DB_PATH = None
    yield ag


def _insert_pending(ag, tool_name="send_message", description="Send SMS to seller",
                    profile="land-worker", tool_args='{"platform": "sms"}',
                    classification="check"):
    conn = ag._ensure_queue_db()
    now = int(time.time())
    cur = conn.execute(
        """INSERT INTO pending_actions
           (profile, tool_name, tool_args, description, classification,
            status, created_at, expires_at)
           VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)""",
        (profile, tool_name, tool_args, description, classification, now, now + 300),
    )
    conn.commit()
    rid = cur.lastrowid
    conn.close()
    return rid


# ===========================================================================
# send_action_gate_card — render
# ===========================================================================

class TestSendActionGateCard:
    @pytest.mark.asyncio
    async def test_renders_card_with_approve_deny_callbacks(self, monkeypatch):
        adapter = _make_adapter()
        captured = {}

        async def _fake_send(**kwargs):
            captured.update(kwargs)
            m = MagicMock()
            m.message_id = 555
            return m

        adapter._send_message_with_thread_fallback = _fake_send
        # Use real keyboard classes so we can inspect callback_data.
        monkeypatch.setattr(tg, "InlineKeyboardButton", _FakeButton)
        monkeypatch.setattr(tg, "InlineKeyboardMarkup", _FakeMarkup)

        result = await adapter.send_action_gate_card(
            chat_id="1843543011",
            action_id=42,
            tool_name="send_message",
            description="Send SMS to seller +15551234567",
            profile="land-worker",
            args_preview='{"platform": "sms", "text": "hi"}',
            classification="check",
        )

        assert result.success is True
        assert result.message_id == "555"
        assert captured["chat_id"] == 1843543011
        assert "Approval Required" in captured["text"]
        assert "send_message" in captured["text"]
        assert "land-worker" in captured["text"]
        # Keyboard present and carries the row id in callback data.
        markup = captured["reply_markup"]
        assert markup is not None
        flat = []
        for row in markup.inline_keyboard:
            flat.extend(row)
        cbs = {btn.callback_data for btn in flat}
        assert "ag:approve:42" in cbs
        assert "ag:deny:42" in cbs

    @pytest.mark.asyncio
    async def test_not_connected(self):
        adapter = _make_adapter()
        adapter._bot = None
        result = await adapter.send_action_gate_card(
            chat_id="1843543011",
            action_id=1,
            tool_name="t",
            description="d",
        )
        assert result.success is False

    @pytest.mark.asyncio
    async def test_html_escapes_description(self):
        adapter = _make_adapter()
        captured = {}

        async def _fake_send(**kwargs):
            captured.update(kwargs)
            m = MagicMock()
            m.message_id = 1
            return m

        adapter._send_message_with_thread_fallback = _fake_send
        await adapter.send_action_gate_card(
            chat_id="1843543011",
            action_id=7,
            tool_name="terminal",
            description="<script>alert(1)</script>",
        )
        assert "<script>" not in captured["text"]
        assert "&lt;script&gt;" in captured["text"]


# ===========================================================================
# Callback dispatch — ag: branch resolving a pending_actions row
# ===========================================================================

class TestActionGateCallback:
    @pytest.mark.asyncio
    async def test_approve_flips_pending_row(self, gate):
        rid = _insert_pending(gate)
        adapter = _make_adapter()

        query = AsyncMock()
        query.data = f"ag:approve:{rid}"
        query.message = MagicMock()
        query.message.chat_id = 1843543011
        query.message.text = "Approval Required"
        query.from_user = MagicMock()
        query.from_user.id = "1843543011"
        query.from_user.first_name = "Michael"
        query.answer = AsyncMock()
        query.edit_message_text = AsyncMock()

        update = MagicMock()
        update.callback_query = query
        context = MagicMock()

        with patch.dict(os.environ, {"TELEGRAM_ALLOWED_USERS": "1843543011"}, clear=False):
            await adapter._handle_callback_query(update, context)

        # Row flipped to decided/approve — this is exactly what the worker's
        # 2s SQLite poll observes to proceed.
        conn = gate._ensure_queue_db()
        row = conn.execute(
            "SELECT status, decision, decided_by FROM pending_actions WHERE id=?",
            (rid,),
        ).fetchone()
        conn.close()
        assert row["status"] == "decided"
        assert row["decision"] == "approve"
        assert row["decided_by"] == "owner"
        query.answer.assert_called_once()
        query.edit_message_text.assert_called_once()

    @pytest.mark.asyncio
    async def test_deny_flips_pending_row(self, gate):
        rid = _insert_pending(gate)
        adapter = _make_adapter()

        query = AsyncMock()
        query.data = f"ag:deny:{rid}"
        query.message = MagicMock()
        query.message.chat_id = 1843543011
        query.message.text = "Approval Required"
        query.from_user = MagicMock()
        query.from_user.id = "1843543011"
        query.from_user.first_name = "Michael"
        query.answer = AsyncMock()
        query.edit_message_text = AsyncMock()

        update = MagicMock()
        update.callback_query = query
        context = MagicMock()

        with patch.dict(os.environ, {"TELEGRAM_ALLOWED_USERS": "1843543011"}, clear=False):
            await adapter._handle_callback_query(update, context)

        conn = gate._ensure_queue_db()
        row = conn.execute(
            "SELECT status, decision FROM pending_actions WHERE id=?", (rid,)
        ).fetchone()
        conn.close()
        assert row["status"] == "decided"
        assert row["decision"] == "deny"
        query.answer.assert_called_once()

    @pytest.mark.asyncio
    async def test_unauthorized_user_cannot_resolve(self, gate):
        rid = _insert_pending(gate)
        adapter = _make_adapter()

        # Runner that denies all authorization.
        class _DenyRunner:
            async def _handle_message(self, event):
                return None

            def _is_user_authorized(self, source):
                return False

        adapter._message_handler = _DenyRunner()._handle_message

        query = AsyncMock()
        query.data = f"ag:approve:{rid}"
        query.message = MagicMock()
        query.message.chat_id = 1843543011
        query.message.chat.type = "private"
        query.message.text = "Approval Required"
        query.from_user = MagicMock()
        query.from_user.id = "999999"  # not the owner
        query.from_user.first_name = "Mallory"
        query.answer = AsyncMock()
        query.edit_message_text = AsyncMock()

        update = MagicMock()
        update.callback_query = query
        context = MagicMock()

        await adapter._handle_callback_query(update, context)

        # Row must remain pending — unauthorized caller cannot decide.
        conn = gate._ensure_queue_db()
        row = conn.execute(
            "SELECT status FROM pending_actions WHERE id=?", (rid,)
        ).fetchone()
        conn.close()
        assert row["status"] == "pending"
        query.answer.assert_called_once()
        assert "not authorized" in query.answer.call_args[1]["text"].lower()

    @pytest.mark.asyncio
    async def test_already_resolved_row(self, gate):
        rid = _insert_pending(gate)
        # Pre-resolve the row out of band.
        assert gate.approve_action(rid) is True

        adapter = _make_adapter()
        query = AsyncMock()
        query.data = f"ag:approve:{rid}"
        query.message = MagicMock()
        query.message.chat_id = 1843543011
        query.message.text = "Approval Required"
        query.from_user = MagicMock()
        query.from_user.id = "1843543011"
        query.from_user.first_name = "Michael"
        query.answer = AsyncMock()
        query.edit_message_text = AsyncMock()
        query.edit_message_reply_markup = AsyncMock()

        update = MagicMock()
        update.callback_query = query
        context = MagicMock()

        with patch.dict(os.environ, {"TELEGRAM_ALLOWED_USERS": "1843543011"}, clear=False):
            await adapter._handle_callback_query(update, context)

        query.answer.assert_called_once()
        assert "already" in query.answer.call_args[1]["text"].lower()

    @pytest.mark.asyncio
    async def test_invalid_action_id(self, gate):
        adapter = _make_adapter()
        query = AsyncMock()
        query.data = "ag:approve:not-a-number"
        query.message = MagicMock()
        query.message.chat_id = 1843543011
        query.from_user = MagicMock()
        query.from_user.id = "1843543011"
        query.from_user.first_name = "Michael"
        query.answer = AsyncMock()

        update = MagicMock()
        update.callback_query = query
        context = MagicMock()

        with patch.dict(os.environ, {"TELEGRAM_ALLOWED_USERS": "1843543011"}, clear=False):
            await adapter._handle_callback_query(update, context)

        query.answer.assert_called_once()
        assert "invalid" in query.answer.call_args[1]["text"].lower()
