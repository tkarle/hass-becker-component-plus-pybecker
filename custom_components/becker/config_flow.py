"""UI configuration flow for the Becker Centronic USB hub."""

from __future__ import annotations

import os
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, override

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import (
    CONF_COVERS,
    CONF_DEVICE,
    CONF_FILENAME,
    CONF_FRIENDLY_NAME,
    CONF_VALUE_TEMPLATE,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.selector import (
    BooleanSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    SerialPortSelector,
    TextSelector,
    TextSelectorConfig,
)
from homeassistant.util import slugify

from .const import (
    CONF_ADD_ANOTHER,
    CONF_CHANNEL,
    CONF_COVER_TYPE,
    CONF_INTERMEDIATE_POSITION,
    CONF_INTERMEDIATE_POSITION_DOWN,
    CONF_INTERMEDIATE_POSITION_UP,
    CONF_MIGRATION_PENDING,
    CONF_REMOTE_ID,
    CONF_SELECTED_COVER,
    CONF_SUN_PROTECTION_POSITION,
    CONF_TILT_BLIND,
    CONF_TILT_INTERMEDIATE,
    CONF_TILT_MODE,
    CONF_TILT_TIME_BLIND,
    CONF_TRAVELLING_TIME_DOWN,
    CONF_TRAVELLING_TIME_UP,
    COVER_TYPE_BLIND,
    COVER_TYPE_SHUTTER,
    DATA_YAML_CONFIG,
    DEFAULT_DATABASE_PATH,
    DEFAULT_HUB_TITLE,
    DOMAIN,
    HUB_UNIQUE_ID,
    INTERMEDIATE_POSITION,
    TILT_MODE_BLIND,
    TILT_MODE_INTERMEDIATE,
    TILT_MODE_NONE,
    TILT_TIME,
    VENTILATION_POSITION,
)
from .cover import COVER_SCHEMA, serialize_platform_config
from .pybecker.becker import CHANNEL_PATTERN

EXPECTED_UNIT_CODES = [f"1737{suffix}" for suffix in "bcdef"]


class DatabaseMissingError(ValueError):
    """Raised when the selected rolling-counter database does not exist."""


class DatabaseOutsideConfigError(ValueError):
    """Raised when a database resolves outside Home Assistant's config folder."""


class InvalidDatabaseError(ValueError):
    """Raised when a file is not a valid Becker rolling-counter database."""


@dataclass(frozen=True, slots=True)
class DatabaseInfo:
    """Validated, read-only information about a Becker database."""

    filename: str
    configured_units: frozenset[int]


def validate_database(config_dir: str, filename: str) -> DatabaseInfo:
    """Validate an existing Becker database without creating or changing it."""
    root = Path(config_dir).resolve()
    candidate = Path(filename)
    if not candidate.is_absolute():
        candidate = root / candidate
    candidate = candidate.resolve()

    if not candidate.is_relative_to(root):
        raise DatabaseOutsideConfigError(filename)
    if not candidate.is_file():
        raise DatabaseMissingError(filename)

    try:
        with sqlite3.connect(f"{candidate.as_uri()}?mode=ro", uri=True) as connection:
            if connection.execute("PRAGMA integrity_check").fetchone() != ("ok",):
                raise InvalidDatabaseError(filename)

            columns = {row[1] for row in connection.execute("PRAGMA table_info(unit)")}
            if not {"code", "increment", "configured", "executed"}.issubset(columns):
                raise InvalidDatabaseError(filename)

            rows = list(
                connection.execute("SELECT rowid, code, configured FROM unit ORDER BY rowid")
            )
    except sqlite3.Error as err:
        raise InvalidDatabaseError(filename) from err

    if [str(row[1]).lower() for row in rows] != EXPECTED_UNIT_CODES:
        raise InvalidDatabaseError(filename)

    return DatabaseInfo(
        filename=str(candidate),
        configured_units=frozenset(int(row[0]) for row in rows if bool(row[2])),
    )


def _unit_from_channel(channel: str) -> int:
    match = CHANNEL_PATTERN.fullmatch(channel)
    if match is None:
        raise ValueError(channel)
    return int(match.group("unit") or 1)


def prepare_covers(covers: dict[str, Any], configured_units: frozenset[int]) -> dict:
    """Validate and serialize cover definitions for config-entry storage."""
    prepared: dict[str, dict[str, Any]] = {}
    used_channels: set[str] = set()

    for object_id, cover_config in covers.items():
        normalized = COVER_SCHEMA(cover_config)
        channel = normalized[CONF_CHANNEL]
        unit = _unit_from_channel(channel)
        if unit not in configured_units:
            raise InvalidDatabaseError(
                f"Sender unit {unit} used by channel {channel} is not configured"
            )
        if channel in used_channels:
            raise ValueError(f"Duplicate Becker channel {channel}")
        used_channels.add(channel)

        serialized = serialize_platform_config({CONF_COVERS: {slugify(object_id): normalized}})[
            CONF_COVERS
        ]
        prepared.update(serialized)

    if not prepared:
        raise ValueError("At least one Becker cover is required")
    return prepared


def _current_tilt_mode(cover: dict[str, Any]) -> str:
    """Return the single UI tilt mode represented by legacy boolean options."""
    if cover.get(CONF_TILT_BLIND, False):
        return TILT_MODE_BLIND
    if cover.get(
        CONF_TILT_INTERMEDIATE,
        cover.get(CONF_INTERMEDIATE_POSITION, True),
    ):
        return TILT_MODE_INTERMEDIATE
    return TILT_MODE_NONE


def _current_cover_type(cover: dict[str, Any]) -> str:
    """Return an explicit type or preserve the legacy tilt-based behavior."""
    if CONF_COVER_TYPE in cover:
        return str(cover[CONF_COVER_TYPE])
    if _current_tilt_mode(cover) != TILT_MODE_NONE:
        return COVER_TYPE_BLIND
    return COVER_TYPE_SHUTTER


def _normalize_remote_ids(value: str) -> str:
    """Validate and normalize a comma/space separated list of Becker remote IDs."""
    identifiers = [item.upper() for item in re.split(r"[\s,;]+", value.strip()) if item]
    if any(re.fullmatch(r"[0-9A-F]{5}:[0-9A-F]", item) is None for item in identifiers):
        raise ValueError("Invalid Becker remote ID")
    return ", ".join(identifiers)


def update_cover_options(cover: dict[str, Any], user_input: dict[str, Any]) -> dict[str, Any]:
    """Return a validated, serializable cover after applying UI options."""
    updated = dict(cover)
    name = str(user_input[CONF_FRIENDLY_NAME]).strip()
    if not name:
        raise ValueError("Friendly name must not be empty")
    updated[CONF_FRIENDLY_NAME] = name
    cover_type = str(user_input[CONF_COVER_TYPE])
    if cover_type not in (COVER_TYPE_SHUTTER, COVER_TYPE_BLIND):
        raise ValueError("Invalid cover type")
    updated[CONF_COVER_TYPE] = cover_type
    # Only venetian blinds expose these in the UI (see async_step_blind_options);
    # roller shutters keep their existing/default value since tilt is force-disabled
    # for them regardless.
    for key, default in (
        (CONF_INTERMEDIATE_POSITION, True),
        (CONF_INTERMEDIATE_POSITION_UP, VENTILATION_POSITION),
        (CONF_INTERMEDIATE_POSITION_DOWN, INTERMEDIATE_POSITION),
    ):
        updated[key] = user_input.get(key, cover.get(key, default))
    updated[CONF_TILT_TIME_BLIND] = user_input.get(
        CONF_TILT_TIME_BLIND,
        cover.get(CONF_TILT_TIME_BLIND, TILT_TIME),
    )

    for key in (
        CONF_TRAVELLING_TIME_UP,
        CONF_TRAVELLING_TIME_DOWN,
    ):
        value = user_input.get(key)
        if value is None:
            updated.pop(key, None)
        else:
            updated[key] = value

    sun_protection_position = user_input.get(CONF_SUN_PROTECTION_POSITION)
    if sun_protection_position is None:
        updated.pop(CONF_SUN_PROTECTION_POSITION, None)
    else:
        updated[CONF_SUN_PROTECTION_POSITION] = sun_protection_position

    template = str(user_input.get(CONF_VALUE_TEMPLATE, "")).strip()
    if template:
        updated[CONF_VALUE_TEMPLATE] = template
    else:
        updated.pop(CONF_VALUE_TEMPLATE, None)

    remote_ids = _normalize_remote_ids(str(user_input.get(CONF_REMOTE_ID, "")))
    if remote_ids:
        updated[CONF_REMOTE_ID] = remote_ids
    else:
        updated.pop(CONF_REMOTE_ID, None)

    tilt_mode = user_input.get(CONF_TILT_MODE, TILT_MODE_NONE)
    if cover_type == COVER_TYPE_SHUTTER:
        tilt_mode = TILT_MODE_NONE
    if tilt_mode == TILT_MODE_INTERMEDIATE and not updated[CONF_INTERMEDIATE_POSITION]:
        raise ValueError("Tilt intermediate requires an intermediate position")
    updated[CONF_TILT_INTERMEDIATE] = tilt_mode == TILT_MODE_INTERMEDIATE
    updated[CONF_TILT_BLIND] = tilt_mode == TILT_MODE_BLIND

    normalized = COVER_SCHEMA(updated)
    return serialize_platform_config({CONF_COVERS: {"cover": normalized}})[CONF_COVERS][
        "cover"
    ]


async def _async_database_info(hass: HomeAssistant, filename: str) -> DatabaseInfo:
    return await hass.async_add_executor_job(validate_database, hass.config.config_dir, filename)


class BeckerConfigFlow(ConfigFlow, domain=DOMAIN):
    """Configure one safe Becker USB hub."""

    VERSION = 1
    MINOR_VERSION = 1

    def __init__(self) -> None:
        self._hub_data: dict[str, Any] = {}
        self._covers: dict[str, dict[str, Any]] = {}
        self._configured_units: frozenset[int] = frozenset()

    @staticmethod
    @callback
    @override
    def async_get_options_flow(config_entry: ConfigEntry) -> BeckerOptionsFlow:
        """Return the options flow for migration activation and hub settings."""
        return BeckerOptionsFlow()

    @override
    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Import active YAML automatically or configure a new hub."""
        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")

        yaml_config = self.hass.data.get(DOMAIN, {}).get(DATA_YAML_CONFIG)
        if yaml_config is not None and user_input is None:
            return await self.async_step_import_yaml()

        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                database = await _async_database_info(self.hass, user_input[CONF_FILENAME])
                if not await self.hass.async_add_executor_job(
                    os.path.exists, user_input[CONF_DEVICE]
                ):
                    errors[CONF_DEVICE] = "device_not_found"
                else:
                    self._hub_data = {
                        CONF_DEVICE: user_input[CONF_DEVICE],
                        CONF_FILENAME: database.filename,
                    }
                    self._configured_units = database.configured_units
                    return await self.async_step_cover()
            except DatabaseMissingError:
                errors[CONF_FILENAME] = "database_missing"
            except DatabaseOutsideConfigError:
                errors[CONF_FILENAME] = "database_outside_config"
            except InvalidDatabaseError:
                errors[CONF_FILENAME] = "invalid_database"

        schema = vol.Schema(
            {
                vol.Required(CONF_DEVICE): SerialPortSelector(),
                vol.Required(CONF_FILENAME, default=DEFAULT_DATABASE_PATH): TextSelector(),
            }
        )
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(schema, user_input or {}),
            errors=errors,
        )

    async def async_step_import_yaml(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm lossless import of the currently active YAML platform."""
        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")

        yaml_config = self.hass.data.get(DOMAIN, {}).get(DATA_YAML_CONFIG)
        if yaml_config is None:
            return self.async_abort(reason="yaml_not_found")

        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                database = await _async_database_info(self.hass, yaml_config[CONF_FILENAME])
                covers = prepare_covers(yaml_config[CONF_COVERS], database.configured_units)
            except DatabaseMissingError:
                errors["base"] = "database_missing"
            except DatabaseOutsideConfigError:
                errors["base"] = "database_outside_config"
            except InvalidDatabaseError:
                errors["base"] = "invalid_database"
            except KeyError, ValueError, vol.Invalid:
                errors["base"] = "invalid_cover_config"
            else:
                await self.async_set_unique_id(HUB_UNIQUE_ID)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=DEFAULT_HUB_TITLE,
                    data={
                        CONF_DEVICE: yaml_config[CONF_DEVICE],
                        CONF_FILENAME: database.filename,
                        CONF_COVERS: covers,
                        CONF_MIGRATION_PENDING: True,
                    },
                )

        return self.async_show_form(
            step_id="import_yaml",
            data_schema=vol.Schema({}),
            errors=errors,
            description_placeholders={
                "device": str(yaml_config.get(CONF_DEVICE, "")),
                "database": str(yaml_config.get(CONF_FILENAME, "")),
                "cover_count": str(len(yaml_config.get(CONF_COVERS, {}))),
            },
        )

    async def async_step_cover(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Add one or more covers to a newly configured hub."""
        errors: dict[str, str] = {}
        if user_input is not None:
            channel = user_input[CONF_CHANNEL]
            if channel in {cover[CONF_CHANNEL] for cover in self._covers.values()}:
                errors[CONF_CHANNEL] = "duplicate_channel"
            elif _unit_from_channel(channel) not in self._configured_units:
                errors[CONF_CHANNEL] = "sender_not_configured"
            else:
                name = user_input[CONF_FRIENDLY_NAME].strip()
                object_id = slugify(name)
                if object_id in self._covers:
                    object_id = f"{object_id}_{channel.replace(':', '_')}"
                self._covers[object_id] = {
                    CONF_FRIENDLY_NAME: name,
                    CONF_CHANNEL: channel,
                    CONF_COVER_TYPE: user_input[CONF_COVER_TYPE],
                    CONF_INTERMEDIATE_POSITION: user_input[CONF_INTERMEDIATE_POSITION],
                }
                if user_input[CONF_ADD_ANOTHER]:
                    user_input = None
                else:
                    covers = prepare_covers(self._covers, self._configured_units)
                    await self.async_set_unique_id(HUB_UNIQUE_ID)
                    self._abort_if_unique_id_configured()
                    return self.async_create_entry(
                        title=DEFAULT_HUB_TITLE,
                        data={
                            **self._hub_data,
                            CONF_COVERS: covers,
                            CONF_MIGRATION_PENDING: False,
                        },
                    )

        channels = [
            f"{unit}:{channel}"
            for unit in sorted(self._configured_units)
            for channel in range(1, 8)
        ]
        schema = vol.Schema(
            {
                vol.Required(CONF_FRIENDLY_NAME): TextSelector(),
                vol.Required(CONF_CHANNEL): SelectSelector(
                    SelectSelectorConfig(
                        options=channels,
                        mode=SelectSelectorMode.DROPDOWN,
                    )
                ),
                vol.Required(
                    CONF_COVER_TYPE, default=COVER_TYPE_SHUTTER
                ): SelectSelector(
                    SelectSelectorConfig(
                        options=[COVER_TYPE_SHUTTER, COVER_TYPE_BLIND],
                        translation_key="cover_type",
                        mode=SelectSelectorMode.DROPDOWN,
                    )
                ),
                vol.Required(CONF_INTERMEDIATE_POSITION, default=True): BooleanSelector(),
                vol.Required(CONF_ADD_ANOTHER, default=False): BooleanSelector(),
            }
        )
        return self.async_show_form(
            step_id="cover",
            data_schema=self.add_suggested_values_to_schema(schema, user_input or {}),
            errors=errors,
            description_placeholders={"cover_count": str(len(self._covers))},
        )


class BeckerOptionsFlow(OptionsFlow):
    """Activate a YAML import or edit safe hub paths."""

    def __init__(self) -> None:
        self._selected_cover: str | None = None
        self._pending_cover_input: dict[str, Any] | None = None

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Choose the correct options form for the entry state."""
        if self.config_entry.data.get(CONF_MIGRATION_PENDING):
            return await self.async_step_activate(user_input)
        return self.async_show_menu(
            step_id="init",
            menu_options=["hub", "select_cover"],
        )

    async def async_step_activate(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Activate an imported hub only after legacy YAML is no longer loaded."""
        errors: dict[str, str] = {}
        if user_input is not None:
            if DATA_YAML_CONFIG in self.hass.data.get(DOMAIN, {}):
                errors["base"] = "yaml_still_active"
            else:
                try:
                    await _async_database_info(self.hass, self.config_entry.data[CONF_FILENAME])
                except DatabaseMissingError, DatabaseOutsideConfigError:
                    errors["base"] = "database_missing"
                except InvalidDatabaseError:
                    errors["base"] = "invalid_database"
                else:
                    data = dict(self.config_entry.data)
                    data[CONF_MIGRATION_PENDING] = False
                    self.hass.config_entries.async_update_entry(self.config_entry, data=data)
                    return self.async_create_entry(data=dict(self.config_entry.options))

        return self.async_show_form(
            step_id="activate",
            data_schema=vol.Schema({}),
            errors=errors,
        )

    async def async_step_hub(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Edit the serial device and existing database path."""
        current = {**self.config_entry.data, **self.config_entry.options}
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                database = await _async_database_info(self.hass, user_input[CONF_FILENAME])
                if not await self.hass.async_add_executor_job(
                    os.path.exists, user_input[CONF_DEVICE]
                ):
                    errors[CONF_DEVICE] = "device_not_found"
                else:
                    options = dict(self.config_entry.options)
                    options.update(
                        {
                            CONF_DEVICE: user_input[CONF_DEVICE],
                            CONF_FILENAME: database.filename,
                        }
                    )
                    return self.async_create_entry(data=options)
            except DatabaseMissingError:
                errors[CONF_FILENAME] = "database_missing"
            except DatabaseOutsideConfigError:
                errors[CONF_FILENAME] = "database_outside_config"
            except InvalidDatabaseError:
                errors[CONF_FILENAME] = "invalid_database"

        schema = vol.Schema(
            {
                vol.Required(CONF_DEVICE): SerialPortSelector(),
                vol.Required(CONF_FILENAME): TextSelector(),
            }
        )
        return self.async_show_form(
            step_id="hub",
            data_schema=self.add_suggested_values_to_schema(
                schema,
                user_input
                or {
                    CONF_DEVICE: current[CONF_DEVICE],
                    CONF_FILENAME: current[CONF_FILENAME],
                },
            ),
            errors=errors,
        )

    async def async_step_select_cover(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Select one cover whose options should be edited."""
        current = {**self.config_entry.data, **self.config_entry.options}
        covers = current[CONF_COVERS]
        if user_input is not None:
            self._selected_cover = user_input[CONF_SELECTED_COVER]
            return await self.async_step_cover_options()

        options = [
            {
                "value": object_id,
                "label": f"{cover.get(CONF_FRIENDLY_NAME, object_id)} ({cover[CONF_CHANNEL]})",
            }
            for object_id, cover in covers.items()
        ]
        return self.async_show_form(
            step_id="select_cover",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_SELECTED_COVER): SelectSelector(
                        SelectSelectorConfig(
                            options=options,
                            mode=SelectSelectorMode.DROPDOWN,
                        )
                    )
                }
            ),
        )

    async def async_step_cover_options(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Edit behavior and position tracking for one cover."""
        if self._selected_cover is None:
            return await self.async_step_select_cover()

        current = {**self.config_entry.data, **self.config_entry.options}
        covers = {key: dict(value) for key, value in current[CONF_COVERS].items()}
        cover = covers[self._selected_cover]
        errors: dict[str, str] = {}

        if user_input is not None:
            if user_input[CONF_COVER_TYPE] == COVER_TYPE_BLIND:
                preliminary = {
                    **user_input,
                    CONF_TILT_MODE: TILT_MODE_NONE,
                    CONF_TILT_TIME_BLIND: cover.get(CONF_TILT_TIME_BLIND, TILT_TIME),
                }
                try:
                    update_cover_options(cover, preliminary)
                except (ValueError, vol.Invalid) as err:
                    if "remote ID" in str(err):
                        errors[CONF_REMOTE_ID] = "invalid_remote_id"
                    else:
                        errors["base"] = "invalid_cover_config"
                else:
                    self._pending_cover_input = dict(user_input)
                    return await self.async_step_blind_options()
            else:
                user_input = {
                    **user_input,
                    CONF_TILT_MODE: TILT_MODE_NONE,
                    CONF_TILT_TIME_BLIND: cover.get(CONF_TILT_TIME_BLIND, TILT_TIME),
                }
                try:
                    covers[self._selected_cover] = update_cover_options(cover, user_input)
                except (ValueError, vol.Invalid) as err:
                    if "remote ID" in str(err):
                        errors[CONF_REMOTE_ID] = "invalid_remote_id"
                    else:
                        errors["base"] = "invalid_cover_config"
                else:
                    options = dict(self.config_entry.options)
                    options[CONF_COVERS] = covers
                    return self.async_create_entry(data=options)

        schema = vol.Schema(
            {
                vol.Required(CONF_FRIENDLY_NAME): TextSelector(),
                vol.Required(CONF_COVER_TYPE): SelectSelector(
                    SelectSelectorConfig(
                        options=[COVER_TYPE_SHUTTER, COVER_TYPE_BLIND],
                        translation_key="cover_type",
                        mode=SelectSelectorMode.DROPDOWN,
                    )
                ),
                vol.Optional(CONF_TRAVELLING_TIME_UP): NumberSelector(
                    NumberSelectorConfig(
                        min=0.1,
                        max=600,
                        step=0.1,
                        unit_of_measurement="s",
                        mode=NumberSelectorMode.BOX,
                    )
                ),
                vol.Optional(CONF_TRAVELLING_TIME_DOWN): NumberSelector(
                    NumberSelectorConfig(
                        min=0.1,
                        max=600,
                        step=0.1,
                        unit_of_measurement="s",
                        mode=NumberSelectorMode.BOX,
                    )
                ),
                vol.Optional(CONF_VALUE_TEMPLATE): TextSelector(
                    TextSelectorConfig(multiline=True)
                ),
                vol.Optional(CONF_REMOTE_ID): TextSelector(),
                vol.Optional(CONF_SUN_PROTECTION_POSITION): NumberSelector(
                    NumberSelectorConfig(
                        min=0,
                        max=100,
                        step=1,
                        unit_of_measurement="%",
                        mode=NumberSelectorMode.BOX,
                    )
                ),
            }
        )
        suggested = {
            CONF_FRIENDLY_NAME: cover.get(CONF_FRIENDLY_NAME, self._selected_cover),
            CONF_COVER_TYPE: _current_cover_type(cover),
        }
        for key in (
            CONF_TRAVELLING_TIME_UP,
            CONF_TRAVELLING_TIME_DOWN,
            CONF_VALUE_TEMPLATE,
            CONF_REMOTE_ID,
            CONF_SUN_PROTECTION_POSITION,
        ):
            if key in cover:
                suggested[key] = cover[key]

        return self.async_show_form(
            step_id="cover_options",
            data_schema=self.add_suggested_values_to_schema(
                schema,
                user_input or suggested,
            ),
            errors=errors,
            description_placeholders={
                "name": str(cover.get(CONF_FRIENDLY_NAME, self._selected_cover)),
                "channel": str(cover[CONF_CHANNEL]),
            },
        )

    async def async_step_blind_options(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Edit options that only apply to a venetian blind."""
        if self._selected_cover is None or self._pending_cover_input is None:
            return await self.async_step_select_cover()

        current = {**self.config_entry.data, **self.config_entry.options}
        covers = {key: dict(value) for key, value in current[CONF_COVERS].items()}
        cover = covers[self._selected_cover]
        errors: dict[str, str] = {}

        if user_input is not None:
            combined = {**self._pending_cover_input, **user_input}
            try:
                covers[self._selected_cover] = update_cover_options(cover, combined)
            except (ValueError, vol.Invalid) as err:
                if "requires an intermediate" in str(err):
                    errors[CONF_TILT_MODE] = "tilt_requires_intermediate"
                else:
                    errors["base"] = "invalid_cover_config"
            else:
                self._pending_cover_input = None
                options = dict(self.config_entry.options)
                options[CONF_COVERS] = covers
                return self.async_create_entry(data=options)

        schema = vol.Schema(
            {
                vol.Required(CONF_INTERMEDIATE_POSITION): BooleanSelector(),
                vol.Required(CONF_INTERMEDIATE_POSITION_UP): NumberSelector(
                    NumberSelectorConfig(
                        min=0,
                        max=100,
                        step=1,
                        unit_of_measurement="%",
                        mode=NumberSelectorMode.BOX,
                    )
                ),
                vol.Required(CONF_INTERMEDIATE_POSITION_DOWN): NumberSelector(
                    NumberSelectorConfig(
                        min=0,
                        max=100,
                        step=1,
                        unit_of_measurement="%",
                        mode=NumberSelectorMode.BOX,
                    )
                ),
                vol.Required(CONF_TILT_MODE): SelectSelector(
                    SelectSelectorConfig(
                        options=[
                            TILT_MODE_NONE,
                            TILT_MODE_INTERMEDIATE,
                            TILT_MODE_BLIND,
                        ],
                        translation_key="tilt_mode",
                        mode=SelectSelectorMode.DROPDOWN,
                    )
                ),
                vol.Required(CONF_TILT_TIME_BLIND): NumberSelector(
                    NumberSelectorConfig(
                        min=0.05,
                        max=5,
                        step=0.05,
                        unit_of_measurement="s",
                        mode=NumberSelectorMode.BOX,
                    )
                ),
            }
        )
        suggested = {
            CONF_INTERMEDIATE_POSITION: cover.get(CONF_INTERMEDIATE_POSITION, True),
            CONF_INTERMEDIATE_POSITION_UP: cover.get(
                CONF_INTERMEDIATE_POSITION_UP, VENTILATION_POSITION
            ),
            CONF_INTERMEDIATE_POSITION_DOWN: cover.get(
                CONF_INTERMEDIATE_POSITION_DOWN, INTERMEDIATE_POSITION
            ),
            CONF_TILT_MODE: _current_tilt_mode(cover),
            CONF_TILT_TIME_BLIND: cover.get(CONF_TILT_TIME_BLIND, TILT_TIME),
        }
        return self.async_show_form(
            step_id="blind_options",
            data_schema=self.add_suggested_values_to_schema(
                schema,
                user_input or suggested,
            ),
            errors=errors,
            description_placeholders={
                "name": str(cover.get(CONF_FRIENDLY_NAME, self._selected_cover)),
                "channel": str(cover[CONF_CHANNEL]),
            },
        )
