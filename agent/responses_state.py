"""Opt-in provider-side state replay for Codex Responses requests.

Hermes' local transcript remains canonical.  This module only rewrites the
wire payload for a single Codex Responses state chain when the caller has
explicitly enabled provider-side stored responses.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def _stable_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
            default=str,
        )
    except Exception:
        return repr(value)


def _rough_chars(value: Any) -> int:
    try:
        return len(_stable_json(value))
    except Exception:
        return 0


def _items_equal_prefix(left: List[Dict[str, Any]], right: List[Dict[str, Any]]) -> bool:
    if len(left) < len(right):
        return False
    for idx, expected in enumerate(right):
        if _stable_json(left[idx]) != _stable_json(expected):
            return False
    return True


def _is_provider_output_item(item: Dict[str, Any]) -> bool:
    item_type = item.get("type")
    role = item.get("role")
    return item_type in {"function_call", "reasoning", "message"} or role == "assistant"


def _is_safe_delta_item(item: Dict[str, Any]) -> bool:
    if item.get("type") == "function_call_output":
        return True
    if item.get("role") == "user":
        return True
    return False


def _extract_safe_delta_items(
    full_input: List[Dict[str, Any]],
    previous_full_input: List[Dict[str, Any]],
) -> tuple[Optional[List[Dict[str, Any]]], str]:
    if not _items_equal_prefix(full_input, previous_full_input):
        return None, "input_prefix_changed"

    remainder = full_input[len(previous_full_input):]
    if not remainder:
        return None, "empty_delta"

    idx = 0
    while idx < len(remainder) and _is_provider_output_item(remainder[idx]):
        idx += 1

    delta = remainder[idx:]
    if not delta:
        return None, "empty_delta"
    if not all(isinstance(item, dict) and _is_safe_delta_item(item) for item in delta):
        return None, "unsafe_delta_items"
    return list(delta), ""


def compatibility_fingerprint(
    api_kwargs: Dict[str, Any],
    *,
    provider: str = "",
    base_url: str = "",
    session_id: str = "",
    branch_id: str = "",
) -> str:
    """Return a deterministic compatibility key for a Responses state chain."""

    identity = {
        "model": api_kwargs.get("model"),
        "provider": provider or "",
        "base_url": base_url or "",
        "reasoning": api_kwargs.get("reasoning"),
        "include": api_kwargs.get("include"),
        "instructions": api_kwargs.get("instructions") or "",
        "tools": api_kwargs.get("tools") or [],
        "session_id": session_id or "",
        "branch_id": branch_id or "",
    }
    return hashlib.sha256(_stable_json(identity).encode("utf-8", errors="replace")).hexdigest()


def _response_id(response: Any) -> Optional[str]:
    if response is None:
        return None
    if isinstance(response, dict):
        value = response.get("id")
    else:
        value = getattr(response, "id", None)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _looks_like_stateful_rejection(error: BaseException, *, used_previous_response_id: bool) -> bool:
    status = getattr(error, "status_code", None)
    try:
        status_int = int(status) if status is not None else None
    except Exception:
        status_int = None

    body = ""
    for attr in ("body", "message", "response"):
        try:
            value = getattr(error, attr, None)
        except Exception:
            value = None
        if value:
            body += f" {value}"
    try:
        body += f" {error}"
    except Exception:
        pass
    text = body.lower()

    state_markers = (
        "previous_response_id",
        "previous response",
        "stored response",
        "response state",
        "store",
        "not supported",
        "unsupported",
        "expired",
        "not found",
        "missing",
        "unknown parameter",
    )
    if any(marker in text for marker in state_markers):
        return status_int is None or 400 <= status_int < 500
    if used_previous_response_id and status_int in {404, 409, 410, 422}:
        return True
    return False


@dataclass
class _PendingRequest:
    full_input: List[Dict[str, Any]]
    used_stateful: bool
    used_previous_response_id: bool


class ResponsesStateReplay:
    """Manage one opt-in Codex Responses state chain."""

    def __init__(
        self,
        *,
        enabled: bool,
        fallback_to_stateless: bool = True,
        unavailable_reason: Optional[str] = None,
    ) -> None:
        self.enabled = bool(enabled)
        self.fallback_to_stateless = bool(fallback_to_stateless)
        self.unavailable_reason = unavailable_reason
        self.previous_response_id: Optional[str] = None
        self.fingerprint: Optional[str] = None
        self.last_full_input: Optional[List[Dict[str, Any]]] = None
        self.reset_reason: Optional[str] = None
        self.fallback_reason: Optional[str] = unavailable_reason
        self.stateless_retry_count = 0
        self._pending: Optional[_PendingRequest] = None

    @property
    def unavailable(self) -> bool:
        return bool(self.unavailable_reason)

    def _record(
        self,
        telemetry: Any,
        *,
        used: bool,
        previous_response_id_used: bool,
        full_input: Optional[List[Dict[str, Any]]] = None,
        delta_input: Optional[List[Dict[str, Any]]] = None,
        reset_reason: Optional[str] = None,
        fallback_reason: Optional[str] = None,
    ) -> None:
        recorder = getattr(telemetry, "record_responses_state", None)
        if not callable(recorder):
            return
        try:
            recorder(
                enabled=self.enabled,
                used=used,
                previous_response_id_used=previous_response_id_used,
                fallback_reason=fallback_reason if fallback_reason is not None else self.fallback_reason,
                reset_reason=reset_reason if reset_reason is not None else self.reset_reason,
                full_input_items=len(full_input) if full_input is not None else None,
                delta_input_items=len(delta_input) if delta_input is not None else None,
                full_input_chars=_rough_chars(full_input) if full_input is not None else None,
                delta_input_chars=_rough_chars(delta_input) if delta_input is not None else None,
                stateless_retry_count=self.stateless_retry_count,
            )
        except Exception:
            logger.debug("responses state telemetry recording failed", exc_info=True)

    def _stateless(self, api_kwargs: Dict[str, Any], telemetry: Any = None) -> Dict[str, Any]:
        self._pending = None
        api_kwargs["store"] = False
        api_kwargs.pop("previous_response_id", None)
        full_input = api_kwargs.get("input") if isinstance(api_kwargs.get("input"), list) else None
        self._record(
            telemetry,
            used=False,
            previous_response_id_used=False,
            full_input=full_input,
            fallback_reason=self.fallback_reason,
        )
        return api_kwargs

    def prepare(
        self,
        api_kwargs: Dict[str, Any],
        *,
        fingerprint: str,
        telemetry: Any = None,
    ) -> Dict[str, Any]:
        """Return request kwargs for this call, using delta replay when safe."""

        if not self.enabled or self.unavailable:
            return self._stateless(api_kwargs, telemetry)

        full_input = api_kwargs.get("input")
        if not isinstance(full_input, list) or not all(isinstance(item, dict) for item in full_input):
            self.reset_reason = "unsafe_full_input"
            self.previous_response_id = None
            self.fingerprint = fingerprint
            self.last_full_input = None
            api_kwargs["store"] = True
            api_kwargs.pop("previous_response_id", None)
            self._pending = _PendingRequest([], True, False)
            self._record(
                telemetry,
                used=True,
                previous_response_id_used=False,
                full_input=None,
                reset_reason=self.reset_reason,
            )
            return api_kwargs

        reset_reason = None
        if self.fingerprint and self.fingerprint != fingerprint:
            reset_reason = "compatibility_changed"
            self.previous_response_id = None
            self.last_full_input = None

        self.fingerprint = fingerprint

        if self.previous_response_id and self.last_full_input is not None:
            delta, unsafe_reason = _extract_safe_delta_items(full_input, self.last_full_input)
            if delta:
                api_kwargs["input"] = delta
                api_kwargs["store"] = True
                api_kwargs["previous_response_id"] = self.previous_response_id
                self.reset_reason = reset_reason
                self._pending = _PendingRequest(list(full_input), True, True)
                self._record(
                    telemetry,
                    used=True,
                    previous_response_id_used=True,
                    full_input=full_input,
                    delta_input=delta,
                    reset_reason=reset_reason,
                )
                return api_kwargs

            reset_reason = unsafe_reason or "unsafe_delta"
            self.previous_response_id = None
            self.last_full_input = None

        self.reset_reason = reset_reason
        api_kwargs["store"] = True
        api_kwargs.pop("previous_response_id", None)
        self._pending = _PendingRequest(list(full_input), True, False)
        self._record(
            telemetry,
            used=True,
            previous_response_id_used=False,
            full_input=full_input,
            reset_reason=reset_reason,
        )
        return api_kwargs

    def record_response(self, response: Any, telemetry: Any = None) -> None:
        pending = self._pending
        self._pending = None
        if not pending or not pending.used_stateful:
            return

        response_id = _response_id(response)
        if not response_id:
            self.fallback_reason = "missing_response_id"
            self.unavailable_reason = "missing_response_id"
            self.previous_response_id = None
            self.last_full_input = None
            self._record(
                telemetry,
                used=False,
                previous_response_id_used=False,
                full_input=pending.full_input,
                fallback_reason=self.fallback_reason,
            )
            return

        self.previous_response_id = response_id
        self.last_full_input = list(pending.full_input)

    def handle_error(self, error: BaseException, telemetry: Any = None) -> bool:
        pending = self._pending
        self._pending = None
        if (
            not self.enabled
            or not self.fallback_to_stateless
            or not pending
            or not pending.used_stateful
        ):
            return False
        if not _looks_like_stateful_rejection(
            error,
            used_previous_response_id=pending.used_previous_response_id,
        ):
            return False

        self.stateless_retry_count += 1
        self.fallback_reason = "provider_rejected_stateful"
        self.unavailable_reason = self.fallback_reason
        self.previous_response_id = None
        self.last_full_input = None
        self._record(
            telemetry,
            used=False,
            previous_response_id_used=False,
            full_input=pending.full_input,
            fallback_reason=self.fallback_reason,
        )
        return True
