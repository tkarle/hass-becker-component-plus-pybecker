"""The Becker Centronic integration."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_DEVICE, CONF_FILENAME, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.typing import ConfigType

from .const import (
    CONF_MIGRATION_PENDING,
    DATA_CONFIG_ENTRY_ACTIVE,
    DEFAULT_HUB_TITLE,
    DOMAIN,
    HUB_UNIQUE_ID,
    MANUFACTURER,
)
from .pybecker.becker_helper import BeckerConnectionError
from .rf_device import PyBecker

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.COVER]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Initialize shared integration data and services."""
    hass.data.setdefault(DOMAIN, {})
    await PyBecker.async_register_services(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up a Becker hub from the UI."""
    domain_data: dict[str, Any] = hass.data.setdefault(DOMAIN, {})
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    # A freshly imported entry must remain passive until the legacy YAML
    # platform is removed. Starting both would open the serial port and rolling
    # counter database twice.
    if entry.data.get(CONF_MIGRATION_PENDING):
        _LOGGER.warning("Becker UI hub imported and waiting for legacy YAML to be removed")
        return True

    active_entry = domain_data.get(DATA_CONFIG_ENTRY_ACTIVE)
    if active_entry not in (None, entry.entry_id):
        raise ConfigEntryError("Only one Becker USB hub can be active")

    config = {**entry.data, **entry.options}
    try:
        await PyBecker.async_setup(
            hass,
            device=config[CONF_DEVICE],
            filename=config[CONF_FILENAME],
            require_existing_database=True,
        )
    except (BeckerConnectionError, OSError, ValueError) as err:
        raise ConfigEntryError(str(err)) from err

    device_registry = dr.async_get(hass)
    device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, HUB_UNIQUE_ID)},
        manufacturer=MANUFACTURER,
        model="Centronic USB Stick",
        name=DEFAULT_HUB_TITLE,
    )

    try:
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except Exception:
        await PyBecker.async_shutdown()
        raise
    domain_data[DATA_CONFIG_ENTRY_ACTIVE] = entry.entry_id

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a Becker UI hub without touching its database."""
    domain_data: dict[str, Any] = hass.data.setdefault(DOMAIN, {})
    if domain_data.get(DATA_CONFIG_ENTRY_ACTIVE) != entry.entry_id:
        return True

    if not await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        return False

    await PyBecker.async_shutdown()
    domain_data.pop(DATA_CONFIG_ENTRY_ACTIVE, None)
    return True


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the hub after its UI options change."""
    await hass.config_entries.async_reload(entry.entry_id)
