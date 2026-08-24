"""Behavior tests for the BeckerEntity cover implementation."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, Mock

import pytest
from homeassistant.components.cover import CoverDeviceClass, CoverEntityFeature
from homeassistant.exceptions import HomeAssistantError

from custom_components.becker.const import (
    COVER_TYPE_BLIND,
    COVER_TYPE_SHUTTER,
    DOMAIN,
    OPEN_POSITION,
)
from custom_components.becker.cover import BeckerEntity
from custom_components.becker.pybecker.becker_helper import (
    MESSAGE,
    finalize_code,
    generate_code,
)


def test_config_entry_cover_has_device_info_and_combined_position_tracking() -> None:
    """UI covers become devices and templates can correct travel-time positions."""
    entity = BeckerEntity(
        object(),
        "Wohnzimmer rechts",
        "2:4",
        COVER_TYPE_BLIND,
        object(),
        None,
        27,
        31.5,
        25,
        75,
        True,
        True,
        False,
        0.3,
        create_devices=True,
    )

    assert entity.device_info["identifiers"] == {(DOMAIN, "cover-2:4")}
    assert "via_device" not in entity.device_info
    assert entity.device_class is CoverDeviceClass.BLIND
    assert entity.supported_features & CoverEntityFeature.OPEN_TILT
    assert entity.supported_features & CoverEntityFeature.SET_POSITION


def test_multiple_remotes_groups_and_central_commands_are_tracked() -> None:
    """Every configured remote channel also accepts that remote's central channel."""
    entity = BeckerEntity(
        object(),
        "Wohnzimmer links",
        "2:3",
        COVER_TYPE_BLIND,
        None,
        "6ED33:3, 6ED33:5, 09FC3:3, 09FC3:5",
        60,
        63.5,
        25,
        75,
        True,
        False,
        False,
        0.3,
        create_devices=True,
    )

    assert entity._remode_ids == {
        b"6ED333",
        b"6ED335",
        b"6ED33F",
        b"09FC33",
        b"09FC35",
        b"09FC3F",
    }


def test_swc545_remote_codes_do_not_turn_short_tilt_into_full_travel() -> None:
    """SHIFT tilt, hold and double-tap packets drive distinct state changes."""
    entity = BeckerEntity(
        object(),
        "Büro Fenster",
        "1:2",
        COVER_TYPE_BLIND,
        None,
        "ABCDE:1",
        25.5,
        25.5,
        25,
        0,
        True,
        False,
        True,
        0.3,
    )
    entity._travel_stop = Mock()
    entity._travel_to_position = Mock()
    entity._update_scheduled_stop_travel_callback = Mock()
    entity._update_scheduled_remote_hold_callback = Mock()

    async def receive(command_code: int) -> None:
        packet = finalize_code(generate_code(1, ["ABCDE", 1, 1], command_code))
        match = MESSAGE.search(packet)
        assert match is not None
        await entity._async_message_received(match)

    asyncio.run(receive(0x28))
    entity._travel_stop.assert_called_once_with()
    entity._travel_to_position.assert_not_called()
    entity._update_scheduled_remote_hold_callback.assert_called_once_with("up")

    entity._travel_stop.reset_mock()
    asyncio.run(receive(0x29))
    entity._travel_stop.assert_not_called()
    entity._travel_to_position.assert_called_once_with(OPEN_POSITION)

    entity._travel_to_position.reset_mock()
    entity._travel_stop.reset_mock()
    asyncio.run(receive(0x2C))
    entity._travel_stop.assert_called_once_with()
    entity._travel_to_position.assert_not_called()

    entity._travel_stop.reset_mock()
    asyncio.run(receive(0x4C))
    entity._travel_to_position.assert_called_once_with(0)

    entity._travel_to_position.reset_mock()
    asyncio.run(receive(0x24))
    entity._travel_to_position.assert_called_once_with(25)


def test_legacy_blind_double_tap_keeps_vertical_position() -> None:
    """A blind without configured tilt tracking must not jump vertically."""
    entity = BeckerEntity(
        object(),
        "Wohnzimmer links",
        "2:3",
        COVER_TYPE_BLIND,
        None,
        "ABCDE:1",
        60,
        63.5,
        25,
        75,
        True,
        False,
        False,
        0.3,
    )
    entity._travel_stop = Mock()
    entity._travel_to_position = Mock()
    entity._update_scheduled_stop_travel_callback = Mock()
    entity._update_scheduled_remote_hold_callback = Mock()

    packet = finalize_code(generate_code(1, ["ABCDE", 1, 1], 0x24))
    match = MESSAGE.search(packet)
    assert match is not None
    asyncio.run(entity._async_message_received(match))

    entity._travel_stop.assert_called_once_with()
    entity._travel_to_position.assert_not_called()


def test_unreleased_swc545_tilt_press_is_promoted_to_vertical_travel() -> None:
    """The receiver may enter maintained travel before emitting 29 or 49."""
    entity = BeckerEntity(
        object(),
        "Büro Fenster",
        "1:2",
        COVER_TYPE_BLIND,
        None,
        None,
        25.5,
        25.5,
        25,
        75,
        True,
        False,
        True,
        0.3,
    )
    entity._travel_to_position = Mock()
    entity._callbacks["remote_hold"] = Mock()

    entity._pending_remote_direction = "up"
    asyncio.run(entity._async_remote_hold_expired(None))
    entity._travel_to_position.assert_called_once_with(OPEN_POSITION)
    assert entity._pending_remote_direction is None
    assert "remote_hold" not in entity._callbacks

    entity._travel_to_position.reset_mock()
    entity._pending_remote_direction = "down"
    asyncio.run(entity._async_remote_hold_expired(None))
    entity._travel_to_position.assert_called_once_with(0)


def test_remote_hold_promotion_can_be_cancelled_on_release() -> None:
    """A short slat press must not become vertical travel after release."""
    entity = BeckerEntity(
        object(),
        "Büro Fenster",
        "1:2",
        COVER_TYPE_BLIND,
        None,
        None,
        25.5,
        25.5,
        25,
        75,
        True,
        False,
        True,
        0.3,
    )
    cancel = Mock()
    entity._callbacks["remote_hold"] = cancel
    entity._pending_remote_direction = "up"

    entity._update_scheduled_remote_hold_callback()

    cancel.assert_called_once_with()
    assert entity._pending_remote_direction is None
    assert "remote_hold" not in entity._callbacks


def test_set_known_position_updates_estimate_without_radio_command() -> None:
    """Manual resynchronization changes only the estimated position."""
    becker = Mock()
    entity = BeckerEntity(
        becker,
        "Büro Fenster",
        "1:2",
        COVER_TYPE_BLIND,
        None,
        None,
        25.5,
        25.5,
        25,
        75,
        True,
        False,
        True,
        0.3,
    )
    entity._update_scheduled_ha_state_callback = Mock()

    asyncio.run(entity.async_set_known_position(position=100))

    assert entity.current_cover_position == 100
    entity._update_scheduled_ha_state_callback.assert_called_once_with(0)
    assert not becker.method_calls


def test_move_down_intermediate_sends_down2_and_tracks_configured_position() -> None:
    """The entity service sends DOWN2 and uses the configured HA estimate."""
    becker = Mock()
    becker.move_down_intermediate = AsyncMock()
    entity = BeckerEntity(
        becker,
        "Wohnzimmer Links",
        "2:3",
        COVER_TYPE_BLIND,
        None,
        None,
        25.5,
        25.5,
        25,
        75,
        True,
        False,
        False,
        0.3,
    )
    entity._tc.set_position(0)
    entity._update_scheduled_ha_state_callback = Mock()

    asyncio.run(entity.async_move_down_intermediate())

    becker.move_down_intermediate.assert_awaited_once_with("2:3")
    assert entity._tc._travel_to_position == 25
    entity._update_scheduled_ha_state_callback.assert_called_once()


def test_move_to_sun_protection_uses_configured_shutter_position() -> None:
    """Roller shutters use their absolute per-entity sun-protection percentage."""
    becker = Mock()
    becker.move_down = AsyncMock()
    entity = BeckerEntity(
        becker,
        "Küche",
        "2:1",
        COVER_TYPE_SHUTTER,
        None,
        None,
        12,
        13,
        25,
        75,
        False,
        False,
        False,
        0.3,
        sun_protection_position=42,
    )
    entity._tc.set_position(0)
    entity._update_scheduled_ha_state_callback = Mock()
    entity._update_scheduled_stop_travel_callback = Mock()

    asyncio.run(entity.async_move_to_sun_protection())

    becker.move_down.assert_awaited_once_with("2:1")
    assert entity._tc._travel_to_position == 58
    entity._update_scheduled_stop_travel_callback.assert_called_once_with(
        pytest.approx(6.96)
    )


def test_move_to_sun_protection_uses_down2_for_blind_without_percentage() -> None:
    """Venetian blinds fall back to their receiver-programmed DOWN2 target."""
    becker = Mock()
    becker.move_down_intermediate = AsyncMock()
    entity = BeckerEntity(
        becker,
        "Wohnzimmer Links",
        "2:3",
        COVER_TYPE_BLIND,
        None,
        None,
        60,
        63.5,
        25,
        75,
        True,
        False,
        False,
        0.3,
    )
    entity._tc.set_position(0)
    entity._update_scheduled_ha_state_callback = Mock()

    asyncio.run(entity.async_move_to_sun_protection())

    becker.move_down_intermediate.assert_awaited_once_with("2:3")


def test_move_to_sun_protection_rejects_unconfigured_shutter() -> None:
    """An unconfigured shutter fails closed instead of sending DOWN2."""
    entity = BeckerEntity(
        Mock(),
        "Küche",
        "2:1",
        COVER_TYPE_SHUTTER,
        None,
        None,
        12,
        13,
        25,
        75,
        False,
        False,
        False,
        0.3,
    )

    with pytest.raises(HomeAssistantError, match="No sun-protection position"):
        asyncio.run(entity.async_move_to_sun_protection())
