import logging

from agent.turn_telemetry import TurnTelemetry


def test_default_config_enables_low_noise_turn_telemetry():
    from hermes_cli.config import DEFAULT_CONFIG

    telemetry = DEFAULT_CONFIG["performance"]["telemetry"]
    responses_state = DEFAULT_CONFIG["performance"]["responses_state"]

    assert telemetry["enabled"] is True
    assert telemetry["log_turn_summary"] is True
    assert responses_state["enabled"] is False
    assert responses_state["fallback_to_stateless"] is True


def test_turn_telemetry_summary_shape_is_json_safe():
    values = iter([10.0, 10.125, 10.5, 11.0])
    telemetry = TurnTelemetry(clock=lambda: next(values))

    telemetry.record_preflight_compression(0.025)
    telemetry.record_request_size(
        message_count=3,
        rough_token_estimate=1200,
        char_count=4800,
        tool_count=7,
    )
    telemetry.start_api_call(1)
    telemetry.record_first_delta()
    telemetry.record_api_duration(0.75, 1)

    summary = telemetry.summary()

    assert summary["enabled"] is True
    assert summary["preflight_compression_ms"] == 25.0
    assert summary["api_duration_ms"] == 750.0
    assert summary["time_to_first_delta_ms"] == 375.0
    assert summary["api_call_count"] == 1
    assert summary["request"] == {
        "message_count": 3,
        "rough_token_estimate": 1200,
        "char_count": 4800,
        "tool_count": 7,
    }
    assert summary["cache"] == {"read_tokens": 0, "write_tokens": 0}
    assert summary["responses_state"]["enabled"] is False
    assert summary["responses_state"]["used"] is False
    assert summary["turn_total_ms"] == 1000.0


def test_turn_telemetry_records_responses_state_defensively():
    telemetry = TurnTelemetry()

    telemetry.record_responses_state(
        enabled=True,
        used=True,
        previous_response_id_used=True,
        fallback_reason="provider_rejected_stateful",
        reset_reason="compatibility_changed",
        full_input_items="12",
        delta_input_items="2",
        full_input_chars="500",
        delta_input_chars="80",
        stateless_retry_count="1",
    )

    state = telemetry.summary()["responses_state"]
    assert state["enabled"] is True
    assert state["used"] is True
    assert state["previous_response_id_used"] is True
    assert state["fallback_reason"] == "provider_rejected_stateful"
    assert state["reset_reason"] == "compatibility_changed"
    assert state["full_input_items"] == 12
    assert state["delta_input_items"] == 2
    assert state["full_input_chars"] == 500
    assert state["delta_input_chars"] == 80
    assert state["stateless_retry_count"] == 1


def test_turn_telemetry_swallows_recording_failures(caplog):
    telemetry = TurnTelemetry()
    telemetry._summary = None

    with caplog.at_level(logging.DEBUG, logger="agent.turn_telemetry"):
        telemetry.record_request_size(
            message_count=object(),
            rough_token_estimate=object(),
            char_count=object(),
            tool_count=object(),
        )

    assert "turn telemetry record_request_size failed" in caplog.text
    assert telemetry.summary()["error"] == "telemetry_summary_failed"
