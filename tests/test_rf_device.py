"""Tests for received Becker remote events."""

from custom_components.becker.pybecker.becker_helper import MESSAGE, finalize_code, generate_code
from custom_components.becker.rf_device import remote_packet_event_data


def _event_data(command_code: int, *, channel: int = 1):
    packet = finalize_code(generate_code(channel, ["ABCDE", 1, 1], command_code))
    match = MESSAGE.search(packet)
    assert match is not None
    return remote_packet_event_data(match)


def test_remote_event_preserves_legacy_command_and_exposes_exact_action() -> None:
    assert _event_data(0x24, channel=15) == {
        "unit": "ABCDE",
        "channel": "F",
        "argument": "4",
        "command_code": "24",
        "command": "up",
        "action": "up_intermediate",
    }


def test_remote_event_reports_normal_command() -> None:
    assert _event_data(0x40) == {
        "unit": "ABCDE",
        "channel": "1",
        "argument": "0",
        "command_code": "40",
        "command": "down",
        "action": "down",
    }


def test_remote_event_keeps_unknown_argument_diagnosable() -> None:
    assert _event_data(0x2A) == {
        "unit": "ABCDE",
        "channel": "1",
        "argument": "A",
        "command_code": "2A",
        "command": "up",
    }
