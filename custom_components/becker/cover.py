"""Support for Becker RF covers."""

import logging
import time
from dataclasses import dataclass

import homeassistant.helpers.config_validation as cv
import voluptuous as vol
from homeassistant.components.cover import (
    ATTR_CURRENT_POSITION,
    ATTR_POSITION,
    PLATFORM_SCHEMA,
    CoverDeviceClass,
    CoverEntity,
    CoverEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    CONF_COVERS,
    CONF_DEVICE,
    CONF_FILENAME,
    CONF_FRIENDLY_NAME,
    CONF_VALUE_TEMPLATE,
)
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError, TemplateError
from homeassistant.helpers import entity_platform
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.event import (
    TrackTemplate,
    async_call_later,
    async_track_template_result,
)
from homeassistant.helpers.restore_state import RestoreEntity

from .const import (
    CLOSED_POSITION,
    CONF_CHANNEL,
    CONF_COVER_TYPE,
    CONF_INTERMEDIATE_DISABLE,
    CONF_INTERMEDIATE_POSITION,
    CONF_INTERMEDIATE_POSITION_DOWN,
    CONF_INTERMEDIATE_POSITION_UP,
    CONF_REMOTE_ID,
    CONF_SUN_PROTECTION_POSITION,
    CONF_TILT_BLIND,
    CONF_TILT_INTERMEDIATE,
    CONF_TILT_TIME_BLIND,
    CONF_TRAVELLING_TIME_DOWN,
    CONF_TRAVELLING_TIME_UP,
    COVER_TYPE_BLIND,
    COVER_TYPE_SHUTTER,
    DATA_YAML_CONFIG,
    DOMAIN,
    INTERMEDIATE_POSITION,
    MANUFACTURER,
    OPEN_POSITION,
    RECEIVE_MESSAGE,
    REMOTE_HOLD_TIMEOUT,
    REMOTE_ID,
    TEMPLATE_UNKNOWN_STATES,
    TEMPLATE_VALID_CLOSE,
    TEMPLATE_VALID_OPEN,
    TILT_FUNCTIONALITY,
    TILT_RECEIVE_TIMEOUT,
    TILT_TIME,
    VENTILATION_POSITION,
)
from .rf_device import PyBecker, decode_remote_action
from .travelcalculator import TravelCalculator

_LOGGER = logging.getLogger(__name__)

COVER_FEATURES = CoverEntityFeature.OPEN | CoverEntityFeature.CLOSE | CoverEntityFeature.STOP

COVER_SCHEMA = vol.Schema(
    {
        vol.Optional(CONF_FRIENDLY_NAME): cv.string,
        vol.Required(CONF_CHANNEL): cv.string,
        vol.Optional(CONF_COVER_TYPE): vol.In(
            [COVER_TYPE_SHUTTER, COVER_TYPE_BLIND]
        ),
        vol.Optional(CONF_VALUE_TEMPLATE): cv.template,
        vol.Optional(CONF_REMOTE_ID): cv.string,
        vol.Optional(CONF_TRAVELLING_TIME_DOWN): cv.positive_float,
        vol.Optional(CONF_TRAVELLING_TIME_UP): cv.positive_float,
        vol.Optional(CONF_INTERMEDIATE_POSITION_UP, default=VENTILATION_POSITION): cv.positive_int,
        vol.Optional(
            CONF_INTERMEDIATE_POSITION_DOWN, default=INTERMEDIATE_POSITION
        ): cv.positive_int,
        vol.Optional(CONF_INTERMEDIATE_DISABLE): cv.boolean,
        vol.Optional(CONF_INTERMEDIATE_POSITION, default=True): cv.boolean,
        vol.Optional(CONF_TILT_INTERMEDIATE): cv.boolean,
        vol.Optional(CONF_TILT_BLIND, default=False): cv.boolean,
        vol.Optional(CONF_TILT_TIME_BLIND, default=TILT_TIME): cv.positive_float,
        vol.Optional(CONF_SUN_PROTECTION_POSITION): vol.All(
            vol.Coerce(int), vol.Range(min=0, max=100)
        ),
    }
)

PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend(
    {
        vol.Required(CONF_COVERS): cv.schema_with_slug_keys(COVER_SCHEMA),
        vol.Optional(CONF_DEVICE): cv.string,
        vol.Optional(CONF_FILENAME): cv.string,
    }
)


async def async_setup_platform(hass, config, async_add_entities, discovery_info=None):
    """Set up the legacy YAML platform."""
    _async_register_entity_services(hass)
    hass.data.setdefault(DOMAIN, {})[DATA_YAML_CONFIG] = serialize_platform_config(config)
    device = config.get(CONF_DEVICE)
    filename = config.get(CONF_FILENAME)
    _LOGGER.debug("%s: %s; %s: %s", CONF_DEVICE, device, CONF_FILENAME, filename)
    await PyBecker.async_setup(hass, device=device, filename=filename)
    await _async_setup_covers(config, async_add_entities)


async def async_setup_entry(hass, entry: ConfigEntry, async_add_entities):
    """Set up covers belonging to a UI-configured Becker hub."""
    _async_register_entity_services(hass)
    config = {**entry.data, **entry.options}
    await _async_setup_covers(config, async_add_entities, create_devices=True)


def _async_register_entity_services(hass):
    """Register cover entity services once for YAML and config-entry setups."""
    platform = entity_platform.async_get_current_platform()
    if not hass.services.has_service(DOMAIN, "set_known_position"):
        platform.async_register_entity_service(
            "set_known_position",
            {
                vol.Required(ATTR_POSITION): vol.All(
                    vol.Coerce(int), vol.Range(min=0, max=100)
                )
            },
            "async_set_known_position",
        )
    if not hass.services.has_service(DOMAIN, "move_down_intermediate"):
        platform.async_register_entity_service(
            "move_down_intermediate",
            {},
            "async_move_down_intermediate",
        )
    if not hass.services.has_service(DOMAIN, "move_to_sun_protection"):
        platform.async_register_entity_service(
            "move_to_sun_protection",
            {},
            "async_move_to_sun_protection",
        )


def serialize_platform_config(config) -> dict:
    """Convert validated YAML data into config-entry-safe primitive values."""
    result = {
        CONF_COVERS: {},
    }
    for key in (CONF_DEVICE, CONF_FILENAME):
        if (value := config.get(key)) is not None:
            result[key] = str(value)

    for object_id, device_config in config[CONF_COVERS].items():
        plain_config = {}
        for key, value in device_config.items():
            if key == CONF_VALUE_TEMPLATE and hasattr(value, "template"):
                value = value.template
            if value is not None:
                plain_config[str(key)] = value
        result[CONF_COVERS][str(object_id)] = plain_config
    return result


@dataclass
class TiltConfig:
    """Resolved intermediate-position and tilt settings for a cover."""

    cover_type: str
    intermediate_position: bool
    intermediate_pos_up: int | None
    intermediate_pos_down: int | None
    tilt_intermediate: bool
    tilt_blind: bool
    tilt_time_blind: float | None


def _resolve_tilt_config(device_config: dict, friendly_name: str) -> TiltConfig:
    """Validate and resolve the intermediate-position/tilt settings for a cover.

    Handles the deprecated intermediate_disable option, the mutual exclusivity
    of tilt_intermediate/tilt_blind, and the legacy cover_type auto-detection.
    """
    cover_type = device_config.get(CONF_COVER_TYPE)

    intermediate_disable = device_config.get(CONF_INTERMEDIATE_DISABLE)
    if intermediate_disable is not None:
        _LOGGER.error(
            "%s is no longer supported for cover %s. Remove it from "
            "configuration.yaml and replace it with %s: %s",
            CONF_INTERMEDIATE_DISABLE,
            friendly_name,
            CONF_TILT_INTERMEDIATE,
            not intermediate_disable,
        )
    else:
        intermediate_disable = False
    intermediate_position = (
        device_config.get(CONF_INTERMEDIATE_POSITION) and not intermediate_disable
    )
    intermediate_pos_up = device_config.get(CONF_INTERMEDIATE_POSITION_UP)
    intermediate_pos_down = device_config.get(CONF_INTERMEDIATE_POSITION_DOWN)

    tilt_intermediate = device_config.get(CONF_TILT_INTERMEDIATE)
    tilt_blind = device_config.get(CONF_TILT_BLIND)
    if tilt_intermediate is None:
        tilt_intermediate = intermediate_position and not tilt_blind
    if tilt_intermediate and not intermediate_position:
        _LOGGER.error(
            "%s is enabled for cover %s, but %s is deactivated. Will deactivate %s.",
            CONF_TILT_INTERMEDIATE,
            friendly_name,
            CONF_INTERMEDIATE_POSITION,
            CONF_TILT_INTERMEDIATE,
        )
        tilt_intermediate = False
    if tilt_intermediate and tilt_blind:
        _LOGGER.error(
            "Both, %s and %s are enabled for cover %s. Will use %s and deactivate %s.",
            CONF_TILT_INTERMEDIATE,
            CONF_TILT_BLIND,
            friendly_name,
            CONF_TILT_BLIND,
            CONF_TILT_INTERMEDIATE,
        )
        tilt_intermediate = False
    tilt_time_blind = device_config.get(CONF_TILT_TIME_BLIND)

    if cover_type is None:
        # Preserve legacy behavior until a type is explicitly selected.
        cover_type = (
            COVER_TYPE_BLIND if tilt_intermediate or tilt_blind else COVER_TYPE_SHUTTER
        )
    elif cover_type == COVER_TYPE_SHUTTER:
        tilt_intermediate = False
        tilt_blind = False

    return TiltConfig(
        cover_type=cover_type,
        intermediate_position=intermediate_position,
        intermediate_pos_up=intermediate_pos_up,
        intermediate_pos_down=intermediate_pos_down,
        tilt_intermediate=tilt_intermediate,
        tilt_blind=tilt_blind,
        tilt_time_blind=tilt_time_blind,
    )


async def _async_setup_covers(config, async_add_entities, *, create_devices=False):
    """Create cover entities from either YAML or config-entry data."""
    covers = []

    for device, device_config in config[CONF_COVERS].items():
        device_config = COVER_SCHEMA(device_config)
        friendly_name = device_config.get(CONF_FRIENDLY_NAME, device)
        channel = device_config.get(CONF_CHANNEL)
        state_template = device_config.get(CONF_VALUE_TEMPLATE)
        remote_id = device_config.get(CONF_REMOTE_ID)
        travel_time_down = device_config.get(CONF_TRAVELLING_TIME_DOWN)
        travel_time_up = device_config.get(CONF_TRAVELLING_TIME_UP)
        # Warning if both template and travelling time are set
        if (travel_time_down or travel_time_up) is not None and state_template is not None:
            _LOGGER.warning(
                'Both "%s" and "%s" are configured for cover %s. "%s" might influence with "%s"!',
                CONF_VALUE_TEMPLATE,
                CONF_TRAVELLING_TIME_UP.rpartition("_")[0],
                friendly_name,
                CONF_VALUE_TEMPLATE,
                CONF_TRAVELLING_TIME_UP.rpartition("_")[0],
            )
        tilt_config = _resolve_tilt_config(device_config, friendly_name)
        sun_protection_position = device_config.get(CONF_SUN_PROTECTION_POSITION)

        if channel is None:
            _LOGGER.error("Must specify %s", CONF_CHANNEL)
            continue
        # Initialize all missing units in the db file and send stop command for sync
        await PyBecker.becker.init_unconfigured_unit(channel, name=friendly_name)

        covers.append(
            BeckerEntity(
                PyBecker.becker,
                friendly_name,
                channel,
                tilt_config.cover_type,
                state_template,
                remote_id,
                travel_time_down,
                travel_time_up,
                tilt_config.intermediate_pos_up,
                tilt_config.intermediate_pos_down,
                tilt_config.intermediate_position,
                tilt_config.tilt_intermediate,
                tilt_config.tilt_blind,
                tilt_config.tilt_time_blind,
                sun_protection_position=sun_protection_position,
                create_devices=create_devices,
            )
        )

    async_add_entities(covers)


class BeckerEntity(CoverEntity, RestoreEntity):
    """Representation of a Becker cover entity."""

    def __init__(
        self,
        becker,
        name,
        channel,
        cover_type,
        state_template,
        remote_id,
        travel_time_down,
        travel_time_up,
        intermediate_pos_up,
        intermediate_pos_down,
        intermediate_position,
        tilt_intermediate,
        tilt_blind,
        tilt_time_blind,
        *,
        sun_protection_position=None,
        create_devices=False,
    ):
        """Init the Becker entity."""
        self._becker = becker
        self._name = name
        self._attr = dict()
        self._channel = channel
        self._cover_type = cover_type
        if cover_type == COVER_TYPE_SHUTTER:
            tilt_intermediate = False
            tilt_blind = False
        if create_devices:
            self._attr_device_info = DeviceInfo(
                identifiers={(DOMAIN, f"cover-{channel}")},
                manufacturer=MANUFACTURER,
                model=(
                    "Centronic venetian blind"
                    if cover_type == COVER_TYPE_BLIND
                    else "Centronic roller shutter"
                ),
                name=name,
            )
        self._attr[CONF_CHANNEL] = str(channel)
        self._cover_features = COVER_FEATURES
        # Template
        self._template = state_template
        # Intermediate position settings
        self._intermediate_position = intermediate_position
        self._intermediate_pos_up = intermediate_pos_up
        self._intermediate_pos_down = intermediate_pos_down
        if intermediate_position:
            self._attr[CONF_INTERMEDIATE_POSITION] = str(intermediate_position)
            self._attr[CONF_INTERMEDIATE_POSITION_UP] = str(intermediate_pos_up)
            self._attr[CONF_INTERMEDIATE_POSITION_DOWN] = str(intermediate_pos_down)
        # tilt settings
        self._tilt_intermediate = tilt_intermediate
        self._tilt_blind = tilt_blind
        self._tilt_time_blind = tilt_time_blind
        self._sun_protection_position = sun_protection_position
        if sun_protection_position is not None:
            self._attr[CONF_SUN_PROTECTION_POSITION] = str(sun_protection_position)
        self._tilt_timeout = time.time()
        self._pending_remote_direction = None
        if tilt_intermediate or tilt_blind:
            self._cover_features |= CoverEntityFeature.OPEN_TILT | CoverEntityFeature.CLOSE_TILT
        if tilt_blind:
            self._attr[TILT_FUNCTIONALITY] = str(CONF_TILT_BLIND)
            self._attr[CONF_TILT_TIME_BLIND] = str(tilt_time_blind)
        if tilt_intermediate:
            self._attr[TILT_FUNCTIONALITY] = str(CONF_TILT_INTERMEDIATE)
        # Callbacks
        self._callbacks = dict()
        # Setup TravelCalculator
        if travel_time_down is not None or travel_time_up is not None:
            self._cover_features |= CoverEntityFeature.SET_POSITION

        travel_time_down = travel_time_down or travel_time_up or 0
        travel_time_up = travel_time_up or travel_time_down or 0
        if self._cover_features & CoverEntityFeature.SET_POSITION:
            self._attr[CONF_TRAVELLING_TIME_DOWN] = str(travel_time_down)
            self._attr[CONF_TRAVELLING_TIME_UP] = str(travel_time_up)
        self._tc = TravelCalculator(travel_time_down, travel_time_up)
        # Setup Remote IDs
        if remote_id is None:
            remote_id = ""
        self._remode_ids = set()
        for i in REMOTE_ID.finditer(remote_id):
            id1 = i["id"].upper() + i["ch"].upper()  # Configured channel
            id2 = i["id"].upper() + "F"  # ALL channels of Multi-Channel-Remote
            self._remode_ids.update([id1.encode(), id2.encode()])
        if len(self._remode_ids) > 0:
            self._attr[CONF_REMOTE_ID] = b", ".join(self._remode_ids).decode()

    async def async_added_to_hass(self):
        """Register callbacks."""
        # restore position
        old_state = await self.async_get_last_state()
        if old_state is not None:
            if ATTR_CURRENT_POSITION in old_state.attributes:
                pos = old_state.attributes[ATTR_CURRENT_POSITION]
                if pos is not None:
                    # In TravelCalculator 0 is open, 100 is closed.
                    self._tc.set_position(100 - pos)
        # set closed position as default if still unknown
        if self._tc.current_position() is None:
            self._tc.set_position(100 - CLOSED_POSITION)
        # Setup callback on received packets
        receive = async_dispatcher_connect(
            self.hass, f"{DOMAIN}.{RECEIVE_MESSAGE}", self._async_message_received
        )
        self.async_on_remove(receive)
        # Setup callback on template changes
        if self._template is not None:
            info = async_track_template_result(
                self.hass,
                [TrackTemplate(self._template, None)],
                self._async_on_template_update,
            )
            self.async_on_remove(info.async_remove)
            info.async_refresh()

    async def async_will_remove_from_hass(self):
        """Unsubscribe temporary callbacks."""
        for callback_name in self._callbacks:
            self._callbacks[callback_name]()

    @property
    def name(self):
        """Return the name of the device as reported by tellcore."""
        return self._name

    @property
    def unique_id(self):
        """Return the unique id of the device - the channel."""
        return self._channel

    @property
    def current_cover_position(self):
        """Return current position of cover. None is unknown, 0 is closed, 100 is fully open."""
        # In TravelCalculator 0 is open, 100 is closed.
        return 100 - self._tc.current_position()

    @property
    def device_class(self):
        """Return the native Home Assistant class for this cover type."""
        if self._cover_type == COVER_TYPE_BLIND:
            return CoverDeviceClass.BLIND
        return CoverDeviceClass.SHUTTER

    @property
    def supported_features(self):
        """Flag supported features."""
        return self._cover_features

    @property
    def is_closed(self):
        """Return true if cover is closed, else False."""
        return self._tc.is_closed()

    @property
    def is_opening(self):
        """Return if the cover is opening or not."""
        action = False
        if self._cover_features & CoverEntityFeature.SET_POSITION:
            action = self._tc.is_opening()
        return action

    @property
    def is_closing(self):
        """Return if the cover is closing or not."""
        action = False
        if self._cover_features & CoverEntityFeature.SET_POSITION:
            action = self._tc.is_closing()
        return action

    @property
    def extra_state_attributes(self):
        """Return the device state attributes."""
        self._attr[ATTR_POSITION] = self.current_cover_position
        return self._attr

    @property
    def should_poll(self):
        """Return if the cover should poll"""
        # by default this is set to True, therefore all cover entities
        # would be updated regulary. This disables the automatic update, but we
        # have to notify hass whenever something changes.
        return False

    @property
    def available(self):
        """Return whether the Becker USB stick is currently connected."""
        return self._becker.connected

    async def async_open_cover(self, **kwargs):
        """Set the cover to the open position."""
        self._travel_to_position(OPEN_POSITION)
        await self._becker.move_up(self._channel)

    async def async_open_cover_tilt(self, **kwargs):
        """Open the cover tilt."""
        # Feature only available if SUPPORT_OPEN_TILT is set
        if self._tilt_blind:
            await self.async_open_cover()
            self._update_scheduled_stop_travel_callback(self._tilt_time_blind)
        if self._tilt_intermediate:
            self._travel_to_position(self._intermediate_pos_up)
            await self._becker.move_up_intermediate(self._channel)

    async def async_close_cover(self, **kwargs):
        """Set the cover to the closed position."""
        self._travel_to_position(CLOSED_POSITION)
        await self._becker.move_down(self._channel)

    async def async_close_cover_tilt(self, **kwargs):
        """Close the cover tilt."""
        # Feature only available if SUPPORT_CLOSE_TILT is set
        if self._tilt_blind:
            await self.async_close_cover()
            self._update_scheduled_stop_travel_callback(self._tilt_time_blind)
        if self._tilt_intermediate:
            self._travel_to_position(self._intermediate_pos_down)
            await self._becker.move_down_intermediate(self._channel)

    async def async_stop_cover(self, **kwargs):
        """Set the cover to the stopped position."""
        self._travel_stop()
        await self._becker.stop(self._channel)

    async def async_set_cover_position(self, **kwargs):
        """Move the cover to a specific position."""
        # Feature only available if SUPPORT_SET_POSITION is set
        if ATTR_POSITION in kwargs:
            pos = kwargs[ATTR_POSITION]
            travel_time = self._travel_to_position(pos)
            if self._tc.is_closing():
                await self._becker.move_down(self._channel)
            elif self._tc.is_opening():
                await self._becker.move_up(self._channel)
            if 0 < pos < 100:
                self._update_scheduled_stop_travel_callback(travel_time)

    async def async_set_known_position(self, **kwargs):
        """Correct the estimated position without transmitting a command."""
        position = kwargs[ATTR_POSITION]
        self._update_scheduled_stop_travel_callback()
        self._update_scheduled_remote_hold_callback()
        self._callbacks.pop("update_ha", lambda: None)()
        self._tc.set_position(100 - position)
        self._tilt_timeout = time.time()
        _LOGGER.info("%s known position set to %s without radio command", self.name, position)
        self._update_scheduled_ha_state_callback(0)

    async def async_move_down_intermediate(self, **kwargs):
        """Move to the receiver's programmed DOWN intermediate position."""
        if self._intermediate_position:
            self._travel_to_position(self._intermediate_pos_down)
        await self._becker.move_down_intermediate(self._channel)

    async def async_move_to_sun_protection(self, **kwargs):
        """Move to this cover's configured sun-protection position."""
        if self._sun_protection_position is not None:
            await self.async_set_cover_position(position=self._sun_protection_position)
            return
        if self._cover_type == COVER_TYPE_BLIND:
            await self.async_move_down_intermediate()
            return
        raise HomeAssistantError(
            f"No sun-protection position configured for {self.name}"
        )

    def _travel_to_position(self, position):
        """Start TravelCalculator and update ha-state."""
        # In TravelCalculator 0 is open, 100 is closed. Swap from_ and to_ position
        travel_time = self._tc.calculate_travel_time(position, self.current_cover_position)
        if self._template is None:
            _LOGGER.debug(
                "%s is travelling from position %s to %s in %s seconds",
                self.name,
                self.current_cover_position,
                position,
                travel_time,
            )
            self._tc.start_travel(100 - position)
            self._update_scheduled_ha_state_callback(travel_time)
        return travel_time

    def _travel_stop(self):
        """Stop TravelCalculator and update ha-state."""
        self._tc.stop()
        if not (self._cover_features & CoverEntityFeature.SET_POSITION) and self._template is None:
            self._tc.set_position(50)
        _LOGGER.debug("%s stopped at position %s", self.name, self.current_cover_position)
        self._update_scheduled_ha_state_callback(0)

    def _update_scheduled_ha_state_callback(self, delay=None):
        """
        Update ha-state callback
        None: unsubscribe pending callback
        0:    update now
        > 0:  update now and setup callback after delay later.
        """  # noqa: D205, D212
        # unsubscribe outdated pending callbacks
        self._callbacks.pop("update_ha", lambda: None)()
        # Schedule callback to update ha-state at end of travel
        if delay is not None:
            # Update ha-state immediately
            _LOGGER.debug("%s update ha-state now", self._name)
            self.async_schedule_update_ha_state()
            # Schedule update ha-state later
            if delay > 0:
                _LOGGER.debug(
                    "%s setup update ha-state callback in %s seconds",
                    self.name,
                    delay,
                )
                self._callbacks["update_ha"] = async_call_later(
                    self.hass, delay, self._async_update_ha_state
                )

    def _update_scheduled_stop_travel_callback(self, delay=None):
        """
        Update ha-state callback
        None: unsubscibe pending callback
        >= 0: setup callback stop after delay later
        """
        # unsubscribe outdated pending callbacks
        self._callbacks.pop("travel_stop", lambda: None)()
        # schedule callback to stop travelling at end of travel
        if delay is not None:
            # Stop now or later
            if delay >= 0:
                _LOGGER.debug(
                    "%s setup stop travel callback in %s seconds",
                    self.name,
                    delay,
                )
                self._callbacks["travel_stop"] = async_call_later(
                    self.hass, delay, self._async_stop_travel
                )

    def _update_scheduled_remote_hold_callback(self, direction=None):
        """Replace or cancel delayed promotion of a remote slat press."""
        self._callbacks.pop("remote_hold", lambda: None)()
        self._pending_remote_direction = direction
        if direction is not None:
            _LOGGER.debug(
                "%s waits %s seconds for remote %s release",
                self.name,
                REMOTE_HOLD_TIMEOUT,
                direction,
            )
            self._callbacks["remote_hold"] = async_call_later(
                self.hass,
                REMOTE_HOLD_TIMEOUT,
                self._async_remote_hold_expired,
            )

    @callback
    async def _async_message_received(self, packet):
        """Handle received packets."""
        ids = packet.group("unit_id") + packet.group("channel")
        if ids in self._remode_ids:
            _LOGGER.debug("%s received a packet from dispatcher", self._name)
            command_code = (
                packet.group("command") + packet.group("argument")
            ).decode("ascii")
            action = decode_remote_action(command_code)
            if action is None:
                _LOGGER.debug(
                    "%s ignores unsupported remote command %s",
                    self._name,
                    command_code,
                )
                return

            # An external command supersedes a pending local timed stop. This
            # prevents an earlier position or tilt callback from stopping a
            # later remote movement.
            if action != "release":
                self._update_scheduled_stop_travel_callback()

            if action not in ("release", "up_tilt", "down_tilt"):
                self._update_scheduled_remote_hold_callback()

            if action == "halt":
                self._travel_stop()
            elif action == "release":
                self._update_scheduled_remote_hold_callback()
                if self._tilt_timeout > time.time():
                    if self._tilt_blind and (self.is_opening or self.is_closing):
                        self._travel_stop()
            elif action == "up_double_tap":
                # On an SWC545 blind remote 2C does not start vertical travel.
                # Keep the last vertical position and discard any stale travel
                # estimate started by an earlier packet.
                self._travel_stop()
                self._tilt_timeout = time.time()
            elif action == "up_intermediate" and self._intermediate_position:
                if (
                    self._cover_type == COVER_TYPE_BLIND
                    and not self._tilt_intermediate
                    and not self._tilt_blind
                ):
                    # Some Becker blind remotes (notably SWC445) emit the
                    # legacy 24 packet for a slat-only double-UP.  The blind
                    # stays at its vertical position; do not project the
                    # configured intermediate percentage into HA.
                    self._travel_stop()
                    self._tilt_timeout = time.time()
                else:
                    self._travel_to_position(self._intermediate_pos_up)
                    self._tilt_timeout = time.time()  # reset timeout
            elif action == "up_tilt":
                # A short SWC545 press only turns the slats. It must cancel a
                # stale vertical estimate but must not start travel to 100%.
                self._travel_stop()
                self._tilt_timeout = time.time() + TILT_RECEIVE_TIMEOUT
                self._update_scheduled_remote_hold_callback("up")
            elif action in ("up", "up_hold"):
                self._travel_to_position(OPEN_POSITION)
                self._tilt_timeout = (
                    time.time()
                    if action == "up_hold"
                    else time.time() + TILT_RECEIVE_TIMEOUT
                )
            elif action == "down_double_tap":
                # SWC545 4C closes the blind completely and then turns the
                # slats horizontally; it is not a configurable 75% target.
                self._travel_to_position(CLOSED_POSITION)
                self._tilt_timeout = time.time()
            elif action == "down_intermediate" and self._intermediate_position:
                if (
                    self._cover_type == COVER_TYPE_BLIND
                    and not self._tilt_intermediate
                    and not self._tilt_blind
                ):
                    # Mirror the SWC445 UP handling for the corresponding
                    # slat-only double-DOWN packet.
                    self._travel_stop()
                    self._tilt_timeout = time.time()
                else:
                    self._travel_to_position(self._intermediate_pos_down)
                    self._tilt_timeout = time.time()  # reset timeout
            elif action == "down_tilt":
                self._travel_stop()
                self._tilt_timeout = time.time() + TILT_RECEIVE_TIMEOUT
                self._update_scheduled_remote_hold_callback("down")
            elif action in ("down", "down_hold"):
                self._travel_to_position(CLOSED_POSITION)
                self._tilt_timeout = (
                    time.time()
                    if action == "down_hold"
                    else time.time() + TILT_RECEIVE_TIMEOUT
                )

    @callback
    async def _async_remote_hold_expired(self, _):
        """Promote an unreleased SWC545 slat press to vertical travel."""
        self._callbacks.pop("remote_hold", None)
        direction = self._pending_remote_direction
        self._pending_remote_direction = None
        self._tilt_timeout = time.time()
        if direction == "up":
            _LOGGER.debug("%s promotes remote UP press to vertical travel", self.name)
            self._travel_to_position(OPEN_POSITION)
        elif direction == "down":
            _LOGGER.debug("%s promotes remote DOWN press to vertical travel", self.name)
            self._travel_to_position(CLOSED_POSITION)

    @callback
    async def _async_stop_travel(self, _):
        """Stop the cover callack."""
        self._travel_stop()
        await self._becker.stop(self._channel)

    @callback
    async def _async_update_ha_state(self, _):
        """Update HA-State while travelling."""
        self._update_scheduled_ha_state_callback(0)

    @callback
    async def _async_on_template_update(self, _, updates):
        """Update position on template update"""
        result = updates.pop().result
        self._attr[CONF_VALUE_TEMPLATE] = result
        if isinstance(result, TemplateError):
            _LOGGER.error("%s: Update template with error", self._name)
        else:
            _LOGGER.debug(
                "%s: Update template with result: %s with type %s", self._name, result, type(result)
            )
            if isinstance(result, str):
                result = result.lower()
            if result in TEMPLATE_VALID_OPEN:
                pos = OPEN_POSITION
            elif result in TEMPLATE_VALID_CLOSE:
                pos = CLOSED_POSITION
            elif isinstance(result, int) or isinstance(result, float):
                # Clip position to a range of 0 - 100
                pos = round(max(min(result, OPEN_POSITION), CLOSED_POSITION))
            elif result in TEMPLATE_UNKNOWN_STATES:
                pos = self.current_cover_position
            else:
                _LOGGER.error("%s: invalid template result: %s", self._name, result)
                pos = self.current_cover_position
            # In TravelCalculator 0 is open, 100 is closed.
            self._tc.set_position(100 - pos)
            self._attr[CONF_VALUE_TEMPLATE] = result
            self.async_schedule_update_ha_state()
