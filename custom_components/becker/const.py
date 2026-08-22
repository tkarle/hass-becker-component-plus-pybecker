"""Becker cover constants."""

import re

from homeassistant.const import STATE_CLOSED, STATE_OPEN

from .pybecker.becker import (
    COMMAND_DOWN,
    COMMAND_DOWN5,
    COMMAND_HALT,
    COMMAND_RELEASE,
    COMMAND_UP,
    COMMAND_UP5,
)

DOMAIN = "becker"
MANUFACTURER = "Becker"

DEFAULT_DATABASE_PATH = "/config/centronic-stick.db"
DEFAULT_HUB_TITLE = "Becker Centronic USB"
HUB_UNIQUE_ID = "becker-centronic-usb"

CONF_ADD_ANOTHER = "add_another"
CONF_COVER_TYPE = "cover_type"
CONF_MIGRATION_PENDING = "migration_pending"
CONF_SELECTED_COVER = "selected_cover"
CONF_SUN_PROTECTION_POSITION = "sun_protection_position"
CONF_TILT_MODE = "tilt_mode"

TILT_MODE_NONE = "none"
TILT_MODE_INTERMEDIATE = "intermediate"
TILT_MODE_BLIND = "blind"

COVER_TYPE_SHUTTER = "shutter"
COVER_TYPE_BLIND = "blind"

DATA_CONFIG_ENTRY_ACTIVE = "config_entry_active"
DATA_YAML_CONFIG = "yaml_config"

DEVICE = "device"
RECEIVE_MESSAGE = "receive_message"
REMOTE_PACKET_EVENT = "remote_packet_received"

CONF_CHANNEL = "channel"
CONF_COVERS = "covers"
CONF_UNIT = "unit"
CONF_REMOTE_ID = "remote_id"
CONF_TRAVELLING_TIME_DOWN = "travelling_time_down"
CONF_TRAVELLING_TIME_UP = "travelling_time_up"
CONF_INTERMEDIATE_DISABLE = "intermediate_position_disable"  # deprecated
CONF_INTERMEDIATE_POSITION = "intermediate_position"
CONF_INTERMEDIATE_POSITION_UP = "intermediate_position_up"
CONF_INTERMEDIATE_POSITION_DOWN = "intermediate_position_down"
CONF_TILT_INTERMEDIATE = "tilt_intermediate"
CONF_TILT_BLIND = "tilt_blind"
CONF_TILT_TIME_BLIND = "tilt_time_blind"

TILT_FUNCTIONALITY = "tilt_functionality"

CLOSED_POSITION = 0
VENTILATION_POSITION = 25
INTERMEDIATE_POSITION = 75
OPEN_POSITION = 100
TILT_TIME = 0.3
TILT_RECEIVE_TIMEOUT = 1.0
REMOTE_HOLD_TIMEOUT = 1.0

COMMANDS = {
    "halt": f"{COMMAND_HALT:02x}".encode(),
    "up": f"{COMMAND_UP:02x}".encode(),
    "up_intermediate": f"{COMMAND_UP5:02x}".encode(),
    "down": f"{COMMAND_DOWN:02x}".encode(),
    "down_intermediate": f"{COMMAND_DOWN5:02x}".encode(),
    "release": f"{COMMAND_RELEASE:02x}".encode(),
    # SWC545 venetian-blind remotes combine the direction nibble with
    # protocol argument flags: 0x08 = SHIFT, 0x04 = double tap and
    # 0x01 = the first (three-second) hold stage.
    "up_tilt": b"28",
    "up_hold": b"29",
    "up_double_tap": b"2c",
    "down_tilt": b"48",
    "down_hold": b"49",
    "down_double_tap": b"4c",
}

REMOTE_ID = re.compile(r"(?P<id>[0-9A-F]{5,5}):(?P<ch>[0-9A-F]{1,1})")

TEMPLATE_VALID_OPEN = [STATE_OPEN, "true", True]
TEMPLATE_VALID_CLOSE = [STATE_CLOSED, "false", False]
TEMPLATE_UNKNOWN_STATES = ["unknown", "unavailable", "none", None]
