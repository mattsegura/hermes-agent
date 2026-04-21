"""Web-chat platform adapter.

Real-time browser chat over Server-Sent Events (SSE).  Designed for SaaS
products that embed a white-labelled chat widget — no Hermes / Paperclip
branding is surfaced to end users.

Endpoints (all under ``/v1/chat/``)::

    POST /v1/chat/turn              — submit a user turn (202 Accepted)
    GET  /v1/chat/stream            — SSE stream, honours Last-Event-ID
    POST /v1/chat/cancel            — AIAgent.interrupt() for a session
    GET  /v1/chat/history           — replay SessionDB history
    GET  /v1/chat/health            — liveness probe

SSE event types emitted on the stream::

    turn.start        {turn_id, ts}
    message.delta     {delta}                   — piped from stream_delta_callback
    reasoning.delta   {delta}                   — piped from reasoning_callback
    tool.start        {tool_id, name, args_preview}
    tool.progress     {tool_id, progress_text}  — only when core supports it
    tool.end          {tool_id, result_preview, duration_ms, ok}
    message.end       {turn_id, total_tokens, cost_cents}
    turn.end          {turn_id, total_tokens, cost_cents}
    cancelled         {turn_id}
    error             {code, message}

Every frame carries a monotonic integer ``id`` so EventSource clients can
resume dropped connections via ``Last-Event-ID``.

Authentication:  HS256 JWT.  POST endpoints read ``Authorization: Bearer
<jwt>``; the SSE endpoint reads ``?access_token=<jwt>`` because the
browser ``EventSource`` API cannot set headers.  JWT claims::

    {profile_id, org_id, user_id, session_id, exp}

``session_id`` from the JWT is the canonical routing key used for
``_active_sessions``, SSE subscribe, cancel, and conversation replay.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import signal
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Deque, Dict, List, Optional, Set

try:
    import jwt as _jwt  # PyJWT
    from jwt import ExpiredSignatureError, InvalidTokenError
    JWT_AVAILABLE = True
except ImportError:  # pragma: no cover — PyJWT is a hard dep but guard anyway
    JWT_AVAILABLE = False
    _jwt = None  # type: ignore[assignment]

    class ExpiredSignatureError(Exception):  # type: ignore[no-redef]
        pass

    class InvalidTokenError(Exception):  # type: ignore[no-redef]
        pass

try:
    from aiohttp import web
    AIOHTTP_AVAILABLE = True
except ImportError:  # pragma: no cover
    AIOHTTP_AVAILABLE = False
    web = None  # type: ignore[assignment]

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Defaults & tunables
# ---------------------------------------------------------------------------

DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 3200

# Each session has a bounded fan-out queue per subscriber.  A slow reader
# (e.g. a stalled browser tab) can't pin memory across the whole session.
_MAX_SUBSCRIBER_QUEUE = 512
# In-memory replay buffer — supports ``Last-Event-ID`` reconnection for clients
# that dropped the stream within a short reconnect window.  Durable replay
# after long outages is via /v1/chat/history.
_REPLAY_BUFFER_SIZE = 100
# SSE keep-alive — prevents proxies from closing idle streams.
_KEEPALIVE_SECONDS = 15.0
# Body-size ceiling for POST endpoints.
_MAX_BODY_BYTES = 1_048_576  # 1 MB
# JWT algorithm — HS256 only (task requirement).
_JWT_ALG = "HS256"


def check_web_requirements() -> bool:
    """Return True if the web adapter's runtime dependencies are present."""
    return AIOHTTP_AVAILABLE and JWT_AVAILABLE


# ---------------------------------------------------------------------------
# SSE event bookkeeping
# ---------------------------------------------------------------------------

@dataclass
class _SSEEvent:
    """A single SSE frame with a monotonic id used for Last-Event-ID replay."""
    id: int
    event: str
    data: Dict[str, Any]

    def encode(self) -> bytes:
        # Multi-line ``data:`` payloads are legal in SSE, but we JSON-encode
        # on one line to keep clients simple.  ``ensure_ascii=False`` lets
        # non-ASCII tokens flow through without \u escapes.
        body = json.dumps(self.data, ensure_ascii=False, default=str)
        return (
            f"id: {self.id}\n"
            f"event: {self.event}\n"
            f"data: {body}\n\n"
        ).encode("utf-8")


@dataclass
class _SessionState:
    """Per-session SSE fan-out state.

    A web session can have multiple subscribers (multiple browser tabs).
    Each subscriber gets its own asyncio.Queue.  Broadcast publishes to all
    queues; slow subscribers that overflow their queue get dropped so they
    can't stall the rest of the fan-out.
    """
    session_id: str
    # Monotonically increasing event id — used for Last-Event-ID resume.
    next_id: int = 1
    # Bounded replay buffer indexed by event id.
    replay: Deque[_SSEEvent] = field(default_factory=lambda: deque(maxlen=_REPLAY_BUFFER_SIZE))
    # Live subscriber queues.  Each one is bounded (_MAX_SUBSCRIBER_QUEUE).
    subscribers: Set[asyncio.Queue] = field(default_factory=set)
    # Active turn id so we know what to emit on cancel.
    current_turn_id: Optional[str] = None
    # Streaming aggregators — collect message.delta text so we can emit a
    # coherent ``turn.end`` with the final body even if the browser only
    # reconnected midway through.
    streamed_text: str = ""
    reasoning_text: str = ""
    total_tokens: int = 0
    # Set when /v1/chat/cancel is invoked — consumed by the message handler.
    cancelled_by_user: bool = False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _json_error(message: str, *, code: str = "bad_request", status: int = 400) -> "web.Response":
    """Uniform JSON error envelope (no product branding)."""
    return web.json_response(
        {"error": {"code": code, "message": message}}, status=status
    )


def _coerce_positive_int(value: Any, default: int) -> int:
    try:
        v = int(value)
        return v if v > 0 else default
    except (TypeError, ValueError):
        return default


def _safe_preview(value: Any, limit: int = 200) -> str:
    """Short-form preview of a value for tool.start / tool.end payloads."""
    try:
        if isinstance(value, (dict, list)):
            text = json.dumps(value, ensure_ascii=False, default=str)
        else:
            text = str(value)
    except Exception:
        text = "<unrenderable>"
    if len(text) > limit:
        text = text[: max(0, limit - 1)] + "…"
    return text


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------

class WebAdapter(BasePlatformAdapter):
    """aiohttp SSE web-chat adapter."""

    # HTTP-only adapter: the default base.py streaming path uses
    # edit_message() to progressively rewrite a prior message.  Over SSE we
    # emit deltas directly and never "edit", so keep the default False.
    REQUIRES_EDIT_FINALIZE: bool = False

    def __init__(self, config: PlatformConfig):
        super().__init__(config, Platform.WEB)
        self._host: str = config.extra.get("host", DEFAULT_HOST)
        self._port: int = _coerce_positive_int(
            config.extra.get("port") or os.environ.get("WEB_GATEWAY_PORT"),
            DEFAULT_PORT,
        )
        # HS256 secret is required at startup — JWTs without verification
        # would allow session hijack.
        self._jwt_secret: str = (
            config.extra.get("jwt_secret")
            or os.environ.get("WEB_GATEWAY_JWT_SECRET", "")
        ).strip()
        self._jwt_audience: Optional[str] = config.extra.get("jwt_audience")
        # CORS — default "*", but can be locked down per tenant.
        self._cors_allow_origin: str = config.extra.get("cors_allow_origin", "*")
        # Agent-completion signal storage.  The gateway's run.py builds a
        # stream_delta_callback that must route into the per-session SSE
        # fan-out, so we expose ``self._stream_sinks`` for the runner.
        self._sessions: Dict[str, _SessionState] = {}
        self._sessions_lock = asyncio.Lock()
        self._runner: Optional["web.AppRunner"] = None
        self._site: Optional["web.BaseSite"] = None
        # Captured at connect() — used by sync agent callbacks to schedule
        # SSE publishes from worker threads via call_soon_threadsafe.
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        # Registered with BasePlatformAdapter so cross-platform delivery
        # still works (not used in browser scenarios, but harmless).
        self.gateway_runner: Any = None
        # SIGTERM drain — set True to stop accepting new SSE subscribers.
        self._draining: bool = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> bool:
        if not AIOHTTP_AVAILABLE:
            logger.error("[web] aiohttp not installed")
            return False
        if not JWT_AVAILABLE:
            logger.error("[web] PyJWT not installed — run `pip install PyJWT`")
            return False
        if not self._jwt_secret:
            logger.error(
                "[web] WEB_GATEWAY_JWT_SECRET is not set.  The web chat "
                "adapter requires an HS256 signing key to authenticate "
                "incoming turns."
            )
            self._set_fatal_error(
                "jwt_secret_missing",
                "WEB_GATEWAY_JWT_SECRET not set",
                retryable=False,
            )
            return False

        # Bind a closure over ``self`` for the CORS middleware so it can
        # access adapter state (allowed origin).  aiohttp's @web.middleware
        # decorator requires a bare ``async def (request, handler)`` sig.
        @web.middleware
        async def cors_middleware(request, handler):
            return await self._apply_cors(request, handler)

        app = web.Application(middlewares=[cors_middleware])
        app["web_adapter"] = self

        app.router.add_route("OPTIONS", "/v1/chat/{tail:.*}", self._handle_options)
        app.router.add_post("/v1/chat/turn", self._handle_turn)
        app.router.add_get("/v1/chat/stream", self._handle_stream)
        app.router.add_post("/v1/chat/cancel", self._handle_cancel)
        app.router.add_get("/v1/chat/history", self._handle_history)
        app.router.add_get("/v1/chat/health", self._handle_health)

        self._loop = asyncio.get_running_loop()
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, self._host, self._port)

        try:
            await self._site.start()
        except OSError as e:
            logger.error(
                "[web] Failed to bind %s:%d — %s", self._host, self._port, e,
            )
            return False

        self._mark_connected()
        logger.info(
            "[web] Listening on http://%s:%d — endpoints: POST /v1/chat/turn, "
            "GET /v1/chat/stream, POST /v1/chat/cancel, GET /v1/chat/history, "
            "GET /v1/chat/health",
            self._host, self._port,
        )

        # Register graceful-shutdown hook so SIGTERM drains active SSE clients.
        try:
            loop = asyncio.get_running_loop()
            for _sig in (signal.SIGTERM, signal.SIGINT):
                try:
                    loop.add_signal_handler(
                        _sig,
                        lambda: asyncio.create_task(self._begin_drain()),
                    )
                except (NotImplementedError, RuntimeError):
                    # Windows / non-main thread — skip silently.
                    break
        except RuntimeError:
            pass

        return True

    async def disconnect(self) -> None:
        self._draining = True

        # Snapshot sessions so we can notify each subscriber to close.
        async with self._sessions_lock:
            sessions = list(self._sessions.values())

        for state in sessions:
            await self._publish(state, "error", {
                "code": "gateway_shutdown",
                "message": "Gateway draining — please reconnect.",
            })
            # Push sentinel into every subscriber queue so writers exit.
            for q in list(state.subscribers):
                try:
                    q.put_nowait(None)
                except asyncio.QueueFull:
                    # Best-effort: the writer loop has its own keep-alive
                    # timeout and will exit eventually.
                    pass

        if self._site is not None:
            try:
                await self._site.stop()
            except Exception:
                pass
            self._site = None

        if self._runner is not None:
            try:
                await self._runner.cleanup()
            except Exception:
                pass
            self._runner = None

        self._mark_disconnected()
        logger.info("[web] Disconnected")

    async def _begin_drain(self) -> None:
        """Mark the adapter as draining but keep live streams open to finish."""
        if self._draining:
            return
        self._draining = True
        logger.info("[web] Received shutdown signal — draining active sessions")

    # ------------------------------------------------------------------
    # BasePlatformAdapter required surface
    # ------------------------------------------------------------------

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Deliver a complete message over SSE.

        ``chat_id`` is the web session_id.  We emit a ``message.delta`` with
        the full content (so non-streaming messages still surface in the UI)
        followed by a ``message.end`` / ``turn.end`` pair.
        """
        state = await self._get_session_state(chat_id, create=True)
        turn_id = state.current_turn_id or metadata.get("turn_id") if metadata else state.current_turn_id

        if content:
            await self._publish(state, "message.delta", {"delta": content})
            state.streamed_text += content

        await self._publish(state, "message.end", {
            "turn_id": turn_id,
            "total_tokens": state.total_tokens,
            # Cost is not tracked by this adapter — upstream accounting owns it.
            "cost_cents": None,
        })
        await self._publish(state, "turn.end", {
            "turn_id": turn_id,
            "total_tokens": state.total_tokens,
            "cost_cents": None,
        })
        state.current_turn_id = None
        state.streamed_text = ""
        state.reasoning_text = ""
        state.total_tokens = 0
        return SendResult(success=True, message_id=turn_id)

    async def send_typing(self, chat_id: str, metadata=None) -> None:
        """No-op — browsers render their own typing state from stream activity."""
        return

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        return {"name": chat_id, "type": "web", "chat_id": chat_id}

    # ------------------------------------------------------------------
    # Runner-side callback hooks
    # ------------------------------------------------------------------
    # These helpers are called by gateway/run.py when it constructs an
    # AIAgent: gateway wires AIAgent.stream_delta_callback etc. to methods
    # that route back through here.  Each method is sync (callable from the
    # agent's worker thread) and schedules the publish on our event loop.

    def push_stream_delta(self, session_id: str, delta: Optional[str]) -> None:
        """Receive a token delta from AIAgent.stream_delta_callback."""
        if not delta:
            return
        state = self._sessions.get(session_id)
        if state is None:
            return
        state.streamed_text += delta
        self._schedule_publish(state, "message.delta", {"delta": delta})

    def push_reasoning_delta(self, session_id: str, delta: Optional[str]) -> None:
        """Receive a reasoning/thinking delta."""
        if not delta:
            return
        state = self._sessions.get(session_id)
        if state is None:
            return
        state.reasoning_text += delta
        self._schedule_publish(state, "reasoning.delta", {"delta": delta})

    def push_tool_start(self, session_id: str, tool_id: str, name: str, args: Any) -> None:
        state = self._sessions.get(session_id)
        if state is None:
            return
        self._schedule_publish(state, "tool.start", {
            "tool_id": tool_id,
            "name": name,
            "args_preview": _safe_preview(args),
        })

    def push_tool_progress(self, session_id: str, tool_id: str, progress: str) -> None:
        """Mid-tool progress update — requires the Hermes core patch."""
        state = self._sessions.get(session_id)
        if state is None:
            return
        self._schedule_publish(state, "tool.progress", {
            "tool_id": tool_id,
            "progress_text": _safe_preview(progress, limit=500),
        })

    def push_tool_end(
        self,
        session_id: str,
        tool_id: str,
        name: str,
        args: Any,
        result: Any,
        *,
        duration_ms: Optional[int] = None,
        ok: bool = True,
    ) -> None:
        state = self._sessions.get(session_id)
        if state is None:
            return
        self._schedule_publish(state, "tool.end", {
            "tool_id": tool_id,
            "name": name,
            "result_preview": _safe_preview(result, limit=400),
            "duration_ms": duration_ms,
            "ok": ok,
        })

    # ------------------------------------------------------------------
    # Middleware & CORS
    # ------------------------------------------------------------------

    async def _apply_cors(self, request: "web.Request", handler):
        """Add CORS headers and short-circuit OPTIONS preflight."""
        origin = request.headers.get("Origin", "")
        allow = self._cors_allow_origin
        cors = {
            "Access-Control-Allow-Origin": allow if allow == "*" else origin,
            "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
            "Access-Control-Allow-Headers": "Authorization, Content-Type, Last-Event-ID",
            "Access-Control-Expose-Headers": "Content-Type",
            "Access-Control-Max-Age": "600",
        }
        if allow != "*" and origin and origin != allow:
            return web.Response(status=403)

        if request.method == "OPTIONS":
            return web.Response(status=204, headers=cors)

        try:
            response = await handler(request)
        except web.HTTPException as http_exc:
            for k, v in cors.items():
                http_exc.headers[k] = v
            raise
        except Exception:
            # Unhandled — log trace but don't leak details.
            logger.exception("[web] Unhandled error in request %s %s", request.method, request.path)
            resp = web.json_response(
                {"error": {"code": "internal_error", "message": "Internal error."}},
                status=500,
            )
            for k, v in cors.items():
                resp.headers[k] = v
            return resp

        for k, v in cors.items():
            response.headers.setdefault(k, v)
        return response

    async def _handle_options(self, request: "web.Request") -> "web.Response":
        # CORS middleware already handled OPTIONS — this is just a fallback.
        return web.Response(status=204)

    # ------------------------------------------------------------------
    # JWT validation
    # ------------------------------------------------------------------

    def _validate_jwt(self, token: str) -> Dict[str, Any]:
        """Decode & verify an HS256 JWT.  Raises ``InvalidTokenError`` on failure."""
        if not token:
            raise InvalidTokenError("Missing token")
        options = {"require": ["exp", "session_id"]}
        decode_kwargs: Dict[str, Any] = {
            "algorithms": [_JWT_ALG],
            "options": options,
        }
        if self._jwt_audience:
            decode_kwargs["audience"] = self._jwt_audience
        # PyJWT raises ExpiredSignatureError / InvalidTokenError on failure.
        claims = _jwt.decode(token, self._jwt_secret, **decode_kwargs)
        if not isinstance(claims, dict):
            raise InvalidTokenError("Malformed claims")
        if not claims.get("session_id"):
            raise InvalidTokenError("Missing session_id")
        return claims

    @staticmethod
    def _extract_bearer(request: "web.Request") -> Optional[str]:
        auth = request.headers.get("Authorization", "")
        if not auth.lower().startswith("bearer "):
            return None
        return auth[7:].strip() or None

    def _authenticate(self, request: "web.Request", *, allow_query: bool = False) -> Dict[str, Any]:
        """Return validated JWT claims.  Raises ``web.HTTPUnauthorized`` on failure."""
        token = self._extract_bearer(request)
        if token is None and allow_query:
            token = request.query.get("access_token")
        if not token:
            raise web.HTTPUnauthorized(
                reason="Missing token",
                text=json.dumps({"error": {"code": "missing_token", "message": "Authentication required."}}),
                content_type="application/json",
            )
        try:
            return self._validate_jwt(token)
        except ExpiredSignatureError:
            raise web.HTTPUnauthorized(
                reason="Expired token",
                text=json.dumps({"error": {"code": "token_expired", "message": "Token expired."}}),
                content_type="application/json",
            )
        except InvalidTokenError as e:
            logger.info("[web] Invalid JWT: %s", e)
            raise web.HTTPUnauthorized(
                reason="Invalid token",
                text=json.dumps({"error": {"code": "invalid_token", "message": "Invalid token."}}),
                content_type="application/json",
            )

    # ------------------------------------------------------------------
    # Board Operator company-id sync
    # ------------------------------------------------------------------

    _COMPANY_ID_RE = re.compile(
        r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
        re.IGNORECASE,
    )

    def _sync_board_ops_company(self, company_id: Any) -> None:
        """Mirror the JWT's company_id into the board_ops state file.

        The board-ops plugin resolves the active company per tool call via a
        fallback chain (explicit arg → PAPERCLIP_COMPANY_ID env → state file).
        Writing the state file here keeps the agent's active company in lock
        step with whichever company the UI currently shows, so two browser
        tabs on two different companies under the same tenant don't collide.

        No-op if the claim is absent or malformed (legacy tokens still work
        via the plugin's existing fallbacks).
        """
        if not isinstance(company_id, str):
            return
        cid = company_id.strip()
        if not cid or not self._COMPANY_ID_RE.match(cid):
            return
        try:
            home = os.environ.get("HERMES_HOME") or str(Path.home() / ".hermes")
            state_path = Path(home) / "board_ops_state.json"
            current: Dict[str, Any] = {}
            if state_path.exists():
                try:
                    current = json.loads(state_path.read_text() or "{}")
                    if not isinstance(current, dict):
                        current = {}
                except Exception:
                    current = {}
            if current.get("company_id") == cid:
                return
            current["company_id"] = cid
            state_path.parent.mkdir(parents=True, exist_ok=True)
            state_path.write_text(json.dumps(current, indent=2))
        except Exception as e:
            logger.warning("[web] board_ops company sync failed: %s", e)

    # ------------------------------------------------------------------
    # Handlers
    # ------------------------------------------------------------------

    async def _handle_health(self, request: "web.Request") -> "web.Response":
        """GET /v1/chat/health — liveness probe (unauthenticated)."""
        return web.json_response({
            "status": "ok",
            "platform": "web",
            "draining": self._draining,
            "active_sessions": len(self._sessions),
        })

    async def _handle_turn(self, request: "web.Request") -> "web.Response":
        """POST /v1/chat/turn — submit a new user turn.

        The response is 202 Accepted with ``{turn_id, session_id}``; the
        actual reply streams over SSE.
        """
        if self._draining:
            return _json_error("Gateway draining", code="draining", status=503)

        claims = self._authenticate(request, allow_query=False)
        session_id = str(claims["session_id"])

        # Sync the UI-selected company into the board_ops state file so any
        # ops_* tool call made during this turn resolves to the same company
        # the owner is looking at. The JWT is the source of truth — Lynk mints
        # it with company_id in the claims on every company switch.
        self._sync_board_ops_company(claims.get("company_id"))

        content_length = request.content_length or 0
        if content_length > _MAX_BODY_BYTES:
            return _json_error("Request body too large", code="body_too_large", status=413)

        try:
            raw = await request.read()
            payload = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            return _json_error("Invalid JSON body", code="invalid_json")
        except Exception:
            return _json_error("Could not read body", code="bad_request")

        if not isinstance(payload, dict):
            return _json_error("Body must be a JSON object", code="invalid_body")

        text = payload.get("text", "")
        if not isinstance(text, str) or not text.strip():
            return _json_error("Field 'text' is required", code="missing_text")

        turn_id = str(payload.get("turn_id") or uuid.uuid4())
        state = await self._get_session_state(session_id, create=True)
        state.current_turn_id = turn_id
        state.cancelled_by_user = False
        state.streamed_text = ""
        state.reasoning_text = ""

        await self._publish(state, "turn.start", {
            "turn_id": turn_id,
            "ts": int(time.time() * 1000),
        })

        # Build the event for the base handler.  The canonical session key
        # is the JWT's session_id — BasePlatformAdapter derives session_key
        # from the SessionSource, and with chat_type="dm" it reduces to
        # chat_id, which is exactly what we want.
        source = self.build_source(
            chat_id=session_id,
            chat_name=payload.get("session_name") or session_id,
            chat_type="dm",
            user_id=str(claims.get("user_id") or claims.get("profile_id") or session_id),
            user_name=payload.get("user_name") or str(claims.get("profile_id") or "web-user"),
        )
        event = MessageEvent(
            text=text.strip(),
            message_type=MessageType.TEXT,
            source=source,
            raw_message={
                "payload": payload,
                "jwt_claims": {
                    "profile_id": claims.get("profile_id"),
                    "org_id": claims.get("org_id"),
                    "user_id": claims.get("user_id"),
                    "session_id": session_id,
                },
            },
            message_id=turn_id,
        )

        # Dispatch to the gateway in a background task so we can return 202
        # immediately.  BasePlatformAdapter.handle_message() already manages
        # the _active_sessions / pending-message lifecycle.
        task = asyncio.create_task(self.handle_message(event))
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

        return web.json_response(
            {"turn_id": turn_id, "session_id": session_id},
            status=202,
        )

    async def _handle_stream(self, request: "web.Request") -> "web.StreamResponse":
        """GET /v1/chat/stream?session_id=X  — SSE subscribe.

        EventSource can't set ``Authorization`` so we accept the JWT via
        ``access_token`` query param.  Supports ``Last-Event-ID`` replay.
        """
        claims = self._authenticate(request, allow_query=True)
        token_session = str(claims["session_id"])
        requested_session = request.query.get("session_id") or token_session

        if requested_session != token_session:
            return _json_error(
                "session_id does not match token claims",
                code="session_mismatch",
                status=403,
            )

        if self._draining:
            return _json_error("Gateway draining", code="draining", status=503)

        state = await self._get_session_state(token_session, create=True)

        # Parse Last-Event-ID — clients send it on auto-reconnect.
        last_event_id = request.headers.get("Last-Event-ID")
        if last_event_id is None:
            last_event_id = request.query.get("last_event_id")
        try:
            last_id = int(last_event_id) if last_event_id else 0
        except (TypeError, ValueError):
            last_id = 0

        response = web.StreamResponse(
            status=200,
            reason="OK",
            headers={
                "Content-Type": "text/event-stream; charset=utf-8",
                "Cache-Control": "no-cache, no-transform",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )
        await response.prepare(request)

        # Replay missed events.  ``Last-Event-ID`` is SSE's built-in resume
        # marker — the browser auto-sets it on reconnect.  We treat any
        # non-negative value as a lower bound (``> last_id``).  Clients that
        # want a full replay pass ``Last-Event-ID: 0``.
        for ev in list(state.replay):
            if ev.id > last_id:
                try:
                    await response.write(ev.encode())
                except ConnectionResetError:
                    return response

        # Subscribe.
        queue: asyncio.Queue = asyncio.Queue(maxsize=_MAX_SUBSCRIBER_QUEUE)
        state.subscribers.add(queue)
        # Preamble comment + retry hint so EventSource knows our reconnect delay.
        try:
            await response.write(b": connected\nretry: 2000\n\n")
        except ConnectionResetError:
            state.subscribers.discard(queue)
            return response

        logger.debug("[web] session=%s subscriber joined (total=%d)", token_session, len(state.subscribers))

        try:
            while True:
                try:
                    ev = await asyncio.wait_for(queue.get(), timeout=_KEEPALIVE_SECONDS)
                except asyncio.TimeoutError:
                    # Keep-alive comment.  Prevents proxies from closing us.
                    try:
                        await response.write(b": keepalive\n\n")
                    except (ConnectionResetError, asyncio.CancelledError):
                        break
                    continue
                if ev is None:
                    # Sentinel from disconnect().
                    break
                try:
                    await response.write(ev.encode())
                except (ConnectionResetError, asyncio.CancelledError):
                    break
        finally:
            state.subscribers.discard(queue)
            logger.debug("[web] session=%s subscriber left (total=%d)", token_session, len(state.subscribers))

        return response

    async def _handle_cancel(self, request: "web.Request") -> "web.Response":
        """POST /v1/chat/cancel — AIAgent.interrupt() for the session."""
        claims = self._authenticate(request, allow_query=False)
        session_id = str(claims["session_id"])
        state = await self._get_session_state(session_id, create=False)
        turn_id = state.current_turn_id if state else None

        # Mark the session cancelled and signal the interrupt event.
        if state is not None:
            state.cancelled_by_user = True

        interrupt_event = self._active_sessions.get(session_id)
        if interrupt_event is not None:
            interrupt_event.set()

        # If gateway runner exposes the live AIAgent, call interrupt() so
        # long-running tools break early.  We don't hard-depend on this —
        # the _active_sessions signal alone is enough for the main loop.
        runner = getattr(self, "gateway_runner", None)
        if runner is not None:
            try:
                agent_cache = getattr(runner, "_agent_cache", None) or {}
                entry = agent_cache.get(session_id)
                if entry and hasattr(entry[0], "interrupt"):
                    entry[0].interrupt(message=None)
            except Exception as e:
                logger.debug("[web] cancel interrupt failed: %s", e)

        if state is not None:
            await self._publish(state, "cancelled", {"turn_id": turn_id})

        return web.json_response(
            {"session_id": session_id, "turn_id": turn_id, "cancelled": True},
            status=200,
        )

    async def _handle_history(self, request: "web.Request") -> "web.Response":
        """GET /v1/chat/history?session_id=X&before=&limit=50 — replay."""
        claims = self._authenticate(request, allow_query=True)
        token_session = str(claims["session_id"])
        requested_session = request.query.get("session_id") or token_session

        if requested_session != token_session:
            return _json_error(
                "session_id does not match token claims",
                code="session_mismatch",
                status=403,
            )

        limit = _coerce_positive_int(request.query.get("limit"), 50)
        limit = min(limit, 500)
        before_raw = request.query.get("before")

        messages = self._load_session_history(token_session)
        if before_raw:
            # ``before`` is a JSON-serialisable index (0-based).  Callers
            # paginate backwards by passing the earliest index seen so far.
            try:
                before = int(before_raw)
            except (TypeError, ValueError):
                before = len(messages)
            messages = messages[: max(before, 0)]

        tail = messages[-limit:]
        return web.json_response({
            "session_id": token_session,
            "messages": tail,
            "count": len(tail),
            "total": len(messages),
        })

    # ------------------------------------------------------------------
    # Session state helpers
    # ------------------------------------------------------------------

    async def _get_session_state(
        self, session_id: str, *, create: bool,
    ) -> Optional[_SessionState]:
        async with self._sessions_lock:
            state = self._sessions.get(session_id)
            if state is None and create:
                state = _SessionState(session_id=session_id)
                self._sessions[session_id] = state
            return state

    async def _publish(
        self, state: _SessionState, event: str, data: Dict[str, Any],
    ) -> None:
        """Publish an SSE frame to every subscriber of ``state``."""
        frame = _SSEEvent(id=state.next_id, event=event, data=data)
        state.next_id += 1
        state.replay.append(frame)
        dead: List[asyncio.Queue] = []
        for q in list(state.subscribers):
            try:
                q.put_nowait(frame)
            except asyncio.QueueFull:
                logger.warning(
                    "[web] session=%s subscriber queue full — dropping",
                    state.session_id,
                )
                dead.append(q)
        for q in dead:
            state.subscribers.discard(q)
            # Force the stream handler to exit on its next tick.
            try:
                q.put_nowait(None)
            except asyncio.QueueFull:
                pass

    def _schedule_publish(
        self, state: _SessionState, event: str, data: Dict[str, Any],
    ) -> None:
        """Thread-safe publish — usable from sync agent callbacks.

        AIAgent callbacks fire on the agent's worker thread, not on our
        event loop.  We use the loop captured at connect() via
        ``run_coroutine_threadsafe`` so publish runs on the correct loop.
        """
        loop = self._loop
        if loop is None or loop.is_closed():
            return

        async def _do() -> None:
            await self._publish(state, event, data)

        try:
            asyncio.run_coroutine_threadsafe(_do(), loop)
        except RuntimeError:
            # Loop not running — drop silently.
            pass

    def _load_session_history(self, session_id: str) -> List[Dict[str, Any]]:
        """Read conversation history from Hermes' session store.

        Returns a list of ``{role, content, ...}`` dicts suitable for the UI.
        Falls back to an empty list if the session store is unavailable.
        """
        try:
            from hermes_state import SessionDB  # local import — heavy module
        except Exception as e:
            logger.debug("[web] SessionDB unavailable: %s", e)
            return []

        try:
            db = SessionDB()
        except Exception as e:
            logger.warning("[web] Failed to open SessionDB: %s", e)
            return []

        try:
            return db.get_messages_as_conversation(session_id) or []
        except Exception as e:
            logger.warning("[web] get_messages_as_conversation failed: %s", e)
            return []


# ---------------------------------------------------------------------------
# Self-check — `python -m gateway.platforms.web`
# ---------------------------------------------------------------------------

def _sanity_check() -> int:
    """Print registered routes, validate env, and return an exit status."""
    print("Hermes web-chat SSE gateway — sanity check\n")

    print("Environment:")
    port = os.environ.get("WEB_GATEWAY_PORT") or str(DEFAULT_PORT)
    secret = os.environ.get("WEB_GATEWAY_JWT_SECRET", "")
    print(f"  WEB_GATEWAY_PORT       = {port}")
    print(f"  WEB_GATEWAY_JWT_SECRET = {'<set>' if secret else '<missing>'}")

    print("\nDependencies:")
    print(f"  aiohttp available      = {AIOHTTP_AVAILABLE}")
    print(f"  PyJWT available        = {JWT_AVAILABLE}")

    print("\nRegistered routes:")
    routes = [
        ("POST",    "/v1/chat/turn",    "submit a user turn"),
        ("GET",     "/v1/chat/stream",  "SSE stream — uses access_token query param"),
        ("POST",    "/v1/chat/cancel",  "interrupt active agent"),
        ("GET",     "/v1/chat/history", "replay SessionDB conversation"),
        ("GET",     "/v1/chat/health",  "liveness probe"),
        ("OPTIONS", "/v1/chat/*",       "CORS preflight"),
    ]
    for method, path, desc in routes:
        print(f"  {method:<8}{path:<24} {desc}")

    problems: List[str] = []
    if not AIOHTTP_AVAILABLE:
        problems.append("aiohttp is not installed — `pip install aiohttp`")
    if not JWT_AVAILABLE:
        problems.append("PyJWT is not installed — `pip install PyJWT`")
    if not secret:
        problems.append(
            "WEB_GATEWAY_JWT_SECRET is not set — the adapter will refuse to start"
        )

    if problems:
        print("\nProblems:")
        for p in problems:
            print(f"  - {p}")
        return 1

    print("\nAll checks passed.")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(_sanity_check())
