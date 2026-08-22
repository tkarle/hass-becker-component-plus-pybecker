"""Safety tests for the Becker UI configuration flow."""

from __future__ import annotations

import asyncio
import hashlib
import sqlite3
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest
from homeassistant.components.cover import CoverDeviceClass, CoverEntityFeature
from homeassistant.config_entries import ConfigEntries
from homeassistant.const import (
    CONF_COVERS,
    CONF_DEVICE,
    CONF_FILENAME,
    CONF_FRIENDLY_NAME,
)
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.becker.config_flow import (
    BeckerConfigFlow,
    DatabaseMissingError,
    DatabaseOutsideConfigError,
    InvalidDatabaseError,
    prepare_covers,
    update_cover_options,
    validate_database,
)
from custom_components.becker.const import (
    CONF_CHANNEL,
    CONF_COVER_TYPE,
    CONF_INTERMEDIATE_POSITION,
    CONF_INTERMEDIATE_POSITION_DOWN,
    CONF_INTERMEDIATE_POSITION_UP,
    CONF_MIGRATION_PENDING,
    CONF_REMOTE_ID,
    CONF_TILT_BLIND,
    CONF_TILT_INTERMEDIATE,
    CONF_TILT_MODE,
    CONF_TILT_TIME_BLIND,
    CONF_TRAVELLING_TIME_DOWN,
    CONF_TRAVELLING_TIME_UP,
    COVER_TYPE_BLIND,
    COVER_TYPE_SHUTTER,
    DATA_YAML_CONFIG,
    DOMAIN,
    OPEN_POSITION,
    TILT_MODE_BLIND,
    TILT_MODE_INTERMEDIATE,
)
from custom_components.becker.cover import BeckerEntity
from custom_components.becker.pybecker.becker_helper import (
    MESSAGE,
    finalize_code,
    generate_code,
)
from custom_components.becker.pybecker.database import Database


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _configured_database(path: Path) -> None:
    with Database(str(path)) as database:
        database.reserve(1, 1, configure=True)
        database.reserve(2, 1, configure=True)


def test_validate_existing_database_is_read_only(tmp_path: Path) -> None:
    """Validation accepts paired units without changing any counter bytes."""
    filename = tmp_path / "centronic-stick.db"
    _configured_database(filename)
    digest_before = _sha256(filename)

    info = validate_database(str(tmp_path), str(filename))

    assert info.filename == str(filename)
    assert info.configured_units == frozenset({1, 2})
    assert _sha256(filename) == digest_before


def test_validate_database_never_creates_a_missing_file(tmp_path: Path) -> None:
    """The UI must fail closed instead of silently starting fresh counters."""
    filename = tmp_path / "missing.db"

    with pytest.raises(DatabaseMissingError):
        validate_database(str(tmp_path), str(filename))

    assert not filename.exists()


def test_validate_database_rejects_outside_and_invalid_files(tmp_path: Path) -> None:
    """Only the HA config tree and the exact Becker schema are accepted."""
    with pytest.raises(DatabaseOutsideConfigError):
        validate_database(str(tmp_path), "/etc/passwd")

    filename = tmp_path / "not-becker.db"
    with sqlite3.connect(filename) as connection:
        connection.execute("CREATE TABLE unrelated (value INTEGER)")

    with pytest.raises(InvalidDatabaseError):
        validate_database(str(tmp_path), str(filename))


def test_prepare_covers_preserves_channels_and_intermediate_mode() -> None:
    """Imported YAML values remain primitive and config-entry serializable."""
    covers = prepare_covers(
        {
            "wohnzimmer_rechts": {
                CONF_CHANNEL: "2:4",
                CONF_INTERMEDIATE_POSITION: True,
            },
            "kueche": {
                CONF_CHANNEL: "2:1",
                CONF_INTERMEDIATE_POSITION: False,
            },
        },
        frozenset({1, 2}),
    )

    assert covers["wohnzimmer_rechts"][CONF_CHANNEL] == "2:4"
    assert covers["wohnzimmer_rechts"][CONF_INTERMEDIATE_POSITION] is True
    assert covers["kueche"][CONF_INTERMEDIATE_POSITION] is False


def test_prepare_covers_rejects_unpaired_or_duplicate_channels() -> None:
    """UI entries cannot address an unpaired sender or one channel twice."""
    with pytest.raises(InvalidDatabaseError, match="not configured"):
        prepare_covers({"bad": {CONF_CHANNEL: "3:1"}}, frozenset({1, 2}))

    with pytest.raises(ValueError, match="Duplicate"):
        prepare_covers(
            {
                "first": {CONF_CHANNEL: "2:3"},
                "second": {CONF_CHANNEL: "2:3"},
            },
            frozenset({1, 2}),
        )


def test_update_cover_options_preserves_channel_and_normalizes_modes() -> None:
    """Per-cover UI edits remain primitive and cannot silently change the RF channel."""
    result = update_cover_options(
        {
            CONF_CHANNEL: "2:4",
            CONF_FRIENDLY_NAME: "Wohnzimmer rechts",
        },
        {
            CONF_FRIENDLY_NAME: "Wohnzimmer rechts",
            CONF_COVER_TYPE: COVER_TYPE_BLIND,
            CONF_TRAVELLING_TIME_UP: 31.5,
            CONF_TRAVELLING_TIME_DOWN: 27,
            CONF_REMOTE_ID: "abcde:2, 12345:f",
            CONF_INTERMEDIATE_POSITION: True,
            CONF_INTERMEDIATE_POSITION_UP: 30,
            CONF_INTERMEDIATE_POSITION_DOWN: 70,
            CONF_TILT_MODE: TILT_MODE_BLIND,
            CONF_TILT_TIME_BLIND: 0.4,
        },
    )

    assert result[CONF_CHANNEL] == "2:4"
    assert result[CONF_TRAVELLING_TIME_UP] == 31.5
    assert result[CONF_REMOTE_ID] == "ABCDE:2, 12345:F"
    assert result[CONF_TILT_BLIND] is True
    assert result[CONF_TILT_INTERMEDIATE] is False


def test_update_cover_options_rejects_unsafe_combinations() -> None:
    """The UI rejects malformed remotes and impossible intermediate tilt settings."""
    base = {
        CONF_FRIENDLY_NAME: "Test",
        CONF_COVER_TYPE: COVER_TYPE_BLIND,
        CONF_INTERMEDIATE_POSITION: False,
        CONF_INTERMEDIATE_POSITION_UP: 25,
        CONF_INTERMEDIATE_POSITION_DOWN: 75,
        CONF_TILT_MODE: TILT_MODE_INTERMEDIATE,
        CONF_TILT_TIME_BLIND: 0.3,
    }
    with pytest.raises(ValueError, match="requires an intermediate"):
        update_cover_options({CONF_CHANNEL: "1:1"}, base)

    with pytest.raises(ValueError, match="remote ID"):
        update_cover_options(
            {CONF_CHANNEL: "1:1"},
            {**base, CONF_INTERMEDIATE_POSITION: True, CONF_REMOTE_ID: "wrong"},
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


def test_shutter_type_never_exposes_tilt_controls() -> None:
    """Roller shutters hide all slat controls even with stale legacy flags."""
    stored = update_cover_options(
        {
            CONF_CHANNEL: "2:1",
            CONF_TILT_INTERMEDIATE: True,
            CONF_TILT_BLIND: True,
        },
        {
            CONF_FRIENDLY_NAME: "Küche",
            CONF_COVER_TYPE: COVER_TYPE_SHUTTER,
            CONF_INTERMEDIATE_POSITION: True,
            CONF_INTERMEDIATE_POSITION_UP: 25,
            CONF_INTERMEDIATE_POSITION_DOWN: 75,
        },
    )
    assert stored[CONF_COVER_TYPE] == COVER_TYPE_SHUTTER
    assert stored[CONF_TILT_INTERMEDIATE] is False
    assert stored[CONF_TILT_BLIND] is False

    entity = BeckerEntity(
        object(),
        "Küche",
        "2:1",
        COVER_TYPE_SHUTTER,
        None,
        None,
        20,
        20,
        25,
        75,
        True,
        True,
        True,
        0.3,
        create_devices=True,
    )

    assert entity.device_class is CoverDeviceClass.SHUTTER
    assert not entity.supported_features & CoverEntityFeature.OPEN_TILT
    assert not entity.supported_features & CoverEntityFeature.CLOSE_TILT


def test_yaml_import_creates_a_passive_lossless_entry(tmp_path: Path) -> None:
    """The UI import copies active YAML but cannot take ownership immediately."""
    filename = tmp_path / "centronic-stick.db"
    _configured_database(filename)
    digest_before = _sha256(filename)

    async def exercise() -> None:
        hass = HomeAssistant(str(tmp_path))
        hass.config_entries = ConfigEntries(hass, {})
        hass.data[DOMAIN] = {
            DATA_YAML_CONFIG: {
                CONF_DEVICE: "/dev/null",
                CONF_FILENAME: str(filename),
                CONF_COVERS: {
                    "wohnzimmer_rechts": {
                        CONF_FRIENDLY_NAME: "Wohnzimmer rechts",
                        CONF_CHANNEL: "2:4",
                        CONF_INTERMEDIATE_POSITION: True,
                    }
                },
            }
        }

        flow = BeckerConfigFlow()
        flow.hass = hass
        flow.context = {"source": "user"}

        form = await flow.async_step_user()
        assert form["type"] is FlowResultType.FORM
        assert form["step_id"] == "import_yaml"
        assert form["description_placeholders"]["cover_count"] == "1"

        result = await flow.async_step_import_yaml({})
        assert result["type"] is FlowResultType.CREATE_ENTRY
        assert result["data"][CONF_MIGRATION_PENDING] is True
        assert result["data"][CONF_FILENAME] == str(filename.resolve())
        assert result["data"][CONF_COVERS]["wohnzimmer_rechts"][CONF_CHANNEL] == "2:4"
        await hass.async_stop()

    asyncio.run(exercise())
    assert _sha256(filename) == digest_before
