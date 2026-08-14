"""UI configuration flow for the Becker Centronic USB hub."""

from __future__ import annotations

import os
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
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.selector import (
    BooleanSelector,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    SerialPortSelector,
    TextSelector,
)
from homeassistant.util import slugify

from .const import (
    CONF_ADD_ANOTHER,
    CONF_CHANNEL,
    CONF_INTERMEDIATE_POSITION,
    CONF_MIGRATION_PENDING,
    DATA_YAML_CONFIG,
    DEFAULT_DATABASE_PATH,
    DEFAULT_HUB_TITLE,
    DOMAIN,
)
from .cover import COVER_SCHEMA, serialize_platform_config
from .pybecker.becker import CHANNEL_PATTERN

HUB_UNIQUE_ID = "becker-centronic-usb"
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

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Choose the correct options form for the entry state."""
        if self.config_entry.data.get(CONF_MIGRATION_PENDING):
            return await self.async_step_activate(user_input)
        return await self.async_step_hub(user_input)

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
                    return self.async_create_entry(
                        data={
                            CONF_DEVICE: user_input[CONF_DEVICE],
                            CONF_FILENAME: database.filename,
                        }
                    )
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
