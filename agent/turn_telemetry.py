"""Defensive per-turn performance telemetry.

This module is intentionally small and side-effect free.  The conversation
loop can call into it at existing measurement points without letting telemetry
failures change runtime behaviour.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)


def _safe_int(value: Any) -> Optional[int]:
    try:
        if value is None:
            return None
        return int(value)
    except Exception:
        return None


def _safe_float_ms(seconds: Any) -> Optional[float]:
    try:
        if seconds is None:
            return None
        return round(float(seconds) * 1000.0, 3)
    except Exception:
        return None


def _json_safe(value: Any) -> Any:
    """Return a JSON-serializable value without raising."""
    try:
        json.dumps(value)
        return value
    except Exception:
        if isinstance(value, dict):
            return {str(k): _json_safe(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [_json_safe(v) for v in value]
        return str(value)


class TurnTelemetry:
    """Per-turn timing and count collector that never raises to callers."""

    def __init__(
        self,
        *,
        enabled: bool = True,
        log_turn_summary: bool = True,
        clock: Callable[[], float] = time.perf_counter,
    ) -> None:
        self.enabled = bool(enabled)
        self.log_turn_summary = bool(log_turn_summary)
        self._clock = clock
        self._started_at = self._now()
        self._api_started_at: Optional[float] = None
        self._first_delta_recorded = False
        self._summary_logged = False
        self._summary: Dict[str, Any] = {
            "enabled": self.enabled,
            "turn_total_ms": None,
            "preflight_compression_ms": None,
            "api_duration_ms": None,
            "time_to_first_delta_ms": None,
            "api_call_count": 0,
            "request": {
                "message_count": None,
                "rough_token_estimate": None,
                "char_count": None,
                "tool_count": None,
            },
            "cache": {
                "read_tokens": 0,
                "write_tokens": 0,
            },
            "responses_state": {
                "enabled": False,
                "used": False,
                "previous_response_id_used": False,
                "fallback_reason": None,
                "reset_reason": None,
                "full_input_items": None,
                "delta_input_items": None,
                "full_input_chars": None,
                "delta_input_chars": None,
                "stateless_retry_count": 0,
            },
        }

    def _now(self) -> float:
        try:
            return float(self._clock())
        except Exception:
            logger.debug("turn telemetry clock failed", exc_info=True)
            return time.perf_counter()

    def _guard(self, label: str, fn: Callable[[], None]) -> None:
        if not self.enabled:
            return
        try:
            fn()
        except Exception:
            logger.debug("turn telemetry %s failed", label, exc_info=True)

    def record_preflight_compression(self, duration_seconds: Any) -> None:
        self._guard(
            "record_preflight_compression",
            lambda: self._summary.__setitem__(
                "preflight_compression_ms", _safe_float_ms(duration_seconds)
            ),
        )

    def record_request_size(
        self,
        *,
        message_count: Any,
        rough_token_estimate: Any,
        char_count: Any,
        tool_count: Any,
    ) -> None:
        def _record() -> None:
            self._summary["request"] = {
                "message_count": _safe_int(message_count),
                "rough_token_estimate": _safe_int(rough_token_estimate),
                "char_count": _safe_int(char_count),
                "tool_count": _safe_int(tool_count),
            }

        self._guard("record_request_size", _record)

    def start_api_call(self, api_call_count: Any = None) -> None:
        def _record() -> None:
            self._api_started_at = self._now()
            if api_call_count is not None:
                value = _safe_int(api_call_count)
                if value is not None:
                    self._summary["api_call_count"] = value

        self._guard("start_api_call", _record)

    def record_first_delta(self) -> None:
        def _record() -> None:
            if self._first_delta_recorded or self._api_started_at is None:
                return
            self._first_delta_recorded = True
            self._summary["time_to_first_delta_ms"] = _safe_float_ms(
                self._now() - self._api_started_at
            )

        self._guard("record_first_delta", _record)

    def record_api_duration(self, duration_seconds: Any, api_call_count: Any = None) -> None:
        def _record() -> None:
            self._summary["api_duration_ms"] = _safe_float_ms(duration_seconds)
            if api_call_count is not None:
                value = _safe_int(api_call_count)
                if value is not None:
                    self._summary["api_call_count"] = value

        self._guard("record_api_duration", _record)

    def record_usage(self, usage: Any) -> None:
        def _record() -> None:
            read_tokens = _safe_int(getattr(usage, "cache_read_tokens", 0)) or 0
            write_tokens = _safe_int(getattr(usage, "cache_write_tokens", 0)) or 0
            cache = self._summary.setdefault("cache", {})
            cache["read_tokens"] = (_safe_int(cache.get("read_tokens")) or 0) + read_tokens
            cache["write_tokens"] = (_safe_int(cache.get("write_tokens")) or 0) + write_tokens

        self._guard("record_usage", _record)

    def record_responses_state(self, **values: Any) -> None:
        def _record() -> None:
            state = self._summary.setdefault("responses_state", {})
            for key, value in values.items():
                if key in {
                    "full_input_items",
                    "delta_input_items",
                    "full_input_chars",
                    "delta_input_chars",
                    "stateless_retry_count",
                }:
                    state[key] = _safe_int(value)
                elif key in {"enabled", "used", "previous_response_id_used"}:
                    state[key] = bool(value)
                elif key in {"fallback_reason", "reset_reason"}:
                    state[key] = str(value) if value is not None else None
                else:
                    state[key] = _json_safe(value)

        self._guard("record_responses_state", _record)

    def summary(self) -> Dict[str, Any]:
        try:
            data = dict(self._summary)
            data["request"] = dict(self._summary.get("request") or {})
            data["cache"] = dict(self._summary.get("cache") or {})
            data["responses_state"] = dict(self._summary.get("responses_state") or {})
            data["turn_total_ms"] = _safe_float_ms(self._now() - self._started_at)
            return _json_safe(data)
        except Exception:
            logger.debug("turn telemetry summary failed", exc_info=True)
            return {"enabled": self.enabled, "error": "telemetry_summary_failed"}

    def log_summary(
        self,
        *,
        session_id: str = "",
        model: str = "",
        provider: str = "",
        log: Optional[logging.Logger] = None,
    ) -> Dict[str, Any]:
        summary = self.summary()
        if not self.enabled or not self.log_turn_summary or self._summary_logged:
            return summary
        self._summary_logged = True
        try:
            payload = {
                "session_id": session_id or "",
                "model": model or "",
                "provider": provider or "",
                "performance": summary,
            }
            (log or logger).info(
                "turn_performance %s",
                json.dumps(_json_safe(payload), sort_keys=True, separators=(",", ":")),
            )
        except Exception:
            logger.debug("turn telemetry log_summary failed", exc_info=True)
        return summary
