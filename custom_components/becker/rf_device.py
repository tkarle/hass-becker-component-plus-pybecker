"""Handle the Becker USB device."""

import logging
import os

import voluptuous as vol
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import callback as ha_callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.dispatcher import async_dispatcher_send

from .const import (
    COMMANDS,
    CONF_CHANNEL,
    CONF_UNIT,
    DOMAIN,
    RECEIVE_MESSAGE,
    REMOTE_PACKET_EVENT,
)
from .pybecker.becker import Becker
from .pybecker.becker_helper import BeckerConnectionError
from .pybecker.database import FILE_PATH, SQL_DB_FILE

_LOGGER = logging.getLogger(__name__)

PAIR_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_CHANNEL): vol.All(int, vol.Range(min=1, max=7)),
        vol.Optional(CONF_UNIT): vol.All(int, vol.Range(min=1, max=5)),
    }
)


def remote_packet_event_data(packet):
    """Build backward-compatible event data for a received remote packet."""
    unit = packet.group("unit_id").decode("ascii").upper()
    channel = packet.group("channel").decode("ascii").upper()
    command_nibble = packet.group("command").upper()
    argument = packet.group("argument").decode("ascii").upper()
    command_code = (command_nibble + packet.group("argument")).decode("ascii").upper()

    data = {
        "unit": unit,
        "channel": channel,
        "argument": argument,
        "command_code": command_code,
    }

    # Keep the historic broad command (for example "up" for both 20 and 24)
    # so existing event automations continue to work.
    broad_code = command_nibble.lower() + b"0"
    broad_name = next((name for name, code in COMMANDS.items() if code == broad_code), None)
    if broad_name is not None:
        data["command"] = broad_name

    # The action exposes the complete command byte and therefore distinguishes
    # normal travel commands from intermediate/double-tap variants.
    exact_code = command_code.lower().encode("ascii")
    action_name = next((name for name, code in COMMANDS.items() if code == exact_code), None)
    if action_name is not None:
        data["action"] = action_name

    return data


class PyBecker:
    """Manages a (single, global) pybecker Becker instance."""

    becker = None
    _shutdown_registered = False
    _shutdown_unsub = None
    _hass = None
    _device = None
    _filename = None

    @classmethod
    async def async_setup(
        cls,
        hass,
        device=None,
        filename=None,
        *,
        require_existing_database=False,
    ):
        """Initiate becker instance."""
        filename = await hass.async_add_executor_job(
            cls._resolve_filename_sync,
            hass,
            filename,
            require_existing_database,
        )

        if cls.becker is not None:
            if cls._device == device and cls._filename == filename:
                return
            raise BeckerConnectionError(
                "A different Becker hub already owns the serial port and database"
            )

        cls.becker = await hass.async_add_executor_job(cls._setup_sync, hass, device, filename)
        cls._hass = hass
        cls._device = device
        cls._filename = filename
        if not cls._shutdown_registered:
            cls._shutdown_unsub = hass.bus.async_listen_once(
                EVENT_HOMEASSISTANT_STOP, cls._async_shutdown
            )
            cls._shutdown_registered = True

    @classmethod
    def _resolve_filename_sync(cls, hass, filename=None, require_existing_database=False):
        """Resolve the database path without opening or modifying the database."""
        if filename is None:
            filename = SQL_DB_FILE
        if not os.path.isfile(filename):
            file = os.path.basename(filename)
            path = os.path.dirname(filename)
            if path == "":
                # file in HA config folder
                if os.path.isfile(os.path.join(hass.config.config_dir, file)):
                    filename = os.path.join(hass.config.config_dir, file)
                # file in pybecker folder
                elif os.path.isfile(os.path.join(FILE_PATH, file)):
                    # move file to config folder once
                    filename = os.path.join(hass.config.config_dir, file)
                    _LOGGER.debug("Move file to %s", filename)
                    os.rename(os.path.join(FILE_PATH, file), filename)
                else:
                    filename = os.path.join(hass.config.config_dir, file)
                    if require_existing_database:
                        raise ValueError(f"Becker sender database {filename} does not exist")
                    _LOGGER.warning("Filename %s does not exist. Create a new file.", file)
            else:
                if not os.path.exists(path):
                    raise ValueError(f"Path of filename {filename} invalid or does not exist")
                if require_existing_database:
                    raise ValueError(f"Becker sender database {filename} does not exist")
                _LOGGER.warning("Filename %s does not exist. Create a new file.", filename)
        _LOGGER.debug("Use filename: %s", filename)
        return os.path.abspath(filename)

    @classmethod
    def _setup_sync(cls, hass, device=None, filename=None):
        """Start the worker outside the event loop."""

        def receive_callback(packet):
            cls.worker_callback(hass, packet)

        # Opening SQLite and starting the worker can perform disk I/O. Keep it
        # outside Home Assistant's event loop.
        return Becker(device, False, filename, receive_callback)

    @classmethod
    async def _async_shutdown(cls, _event):
        """Close the worker cleanly when Home Assistant stops."""
        cls._shutdown_unsub = None
        await cls._async_close()

    @classmethod
    async def async_shutdown(cls):
        """Close the worker when a config entry is unloaded."""
        if cls._shutdown_unsub is not None:
            cls._shutdown_unsub()
            cls._shutdown_unsub = None
        await cls._async_close()

    @classmethod
    async def _async_close(cls):
        """Close the active worker and reset singleton ownership."""
        if cls.becker is not None:
            becker = cls.becker
            cls.becker = None
            await cls._hass.async_add_executor_job(becker.close)
        cls._shutdown_registered = False
        cls._hass = None
        cls._device = None
        cls._filename = None

    @classmethod
    async def async_register_services(cls, hass):
        """Register component services."""
        if not hass.services.has_service(DOMAIN, "pair"):
            hass.services.async_register(DOMAIN, "pair", cls.handle_pair, PAIR_SCHEMA)
        if not hass.services.has_service(DOMAIN, "log_units"):
            hass.services.async_register(DOMAIN, "log_units", cls.handle_log_units)

    @classmethod
    async def handle_pair(cls, call):
        """Service to pair with a cover receiver."""
        if cls.becker is None:
            raise ServiceValidationError("Becker integration is not initialized")
        channel = call.data.get(CONF_CHANNEL)
        unit = call.data.get(CONF_UNIT, 1)
        await cls.becker.pair(f"{unit}:{channel}")

    @classmethod
    async def handle_log_units(cls, call):
        """Service that logs all paired units."""
        if cls.becker is None:
            raise ServiceValidationError("Becker integration is not initialized")
        units = await cls.becker.list_units()

        # Apparently the SQLite results are implicitly returned in unit id
        # order. This seems pretty dirty to rely on.
        unit_id = 1
        _LOGGER.info("Configured Becker centronic units:")
        for row in units:
            unit_code, increment = row[0:2]
            _LOGGER.info("Unit id %d, unit code %s, increment %d", unit_id, unit_code, increment)
            unit_id += 1

    @classmethod
    def worker_callback(cls, hass, packet):
        """Move a worker-thread callback safely onto Home Assistant's loop."""
        hass.loop.call_soon_threadsafe(cls._async_callback, hass, packet)

    @staticmethod
    @ha_callback
    def _async_callback(hass, packet):
        """Handle a received packet on Home Assistant's event loop."""
        _LOGGER.debug("Received packet for dispatcher")
        async_dispatcher_send(hass, f"{DOMAIN}.{RECEIVE_MESSAGE}", packet)

        # Also fire an explicit event that external applications can listen to
        # if that is of use to them.
        data = remote_packet_event_data(packet)
        hass.bus.async_fire(f"{DOMAIN}_{REMOTE_PACKET_EVENT}", data)
