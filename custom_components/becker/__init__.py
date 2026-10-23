"""The Becker Centronic integration."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from homeassistant.components import persistent_notification
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_DEVICE, CONF_FILENAME, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.storage import Store
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
BOOT_ID_PATH = Path("/proc/sys/kernel/random/boot_id")
BOOT_STORE_KEY = f"{DOMAIN}.host_boot"
BOOT_NOTIFICATION_ID = f"{DOMAIN}_host_reboot"


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Initialize shared integration data and services."""
    hass.data.setdefault(DOMAIN, {})
    await _async_warn_after_host_reboot(hass)
    await PyBecker.async_register_services(hass)
    return True


async def _async_read_host_boot_id(hass: HomeAssistant) -> str | None:
    """Read the Linux host boot ID without blocking Home Assistant's event loop."""
    try:
        value = await hass.async_add_executor_job(BOOT_ID_PATH.read_text, "utf-8")
    except OSError:
        _LOGGER.warning("Could not read host boot ID; Becker reboot warning is unavailable")
        return None
    value = value.strip()
    return value or None


async def _async_warn_after_host_reboot(hass: HomeAssistant) -> None:
    """Warn once when the host, rather than only Home Assistant Core, rebooted."""
    boot_id = await _async_read_host_boot_id(hass)
    if boot_id is None:
        return

    store: Store[dict[str, str]] = Store(hass, 1, BOOT_STORE_KEY)
    previous = await store.async_load()
    previous_boot_id = previous.get("boot_id") if previous else None
    await store.async_save({"boot_id": boot_id})

    if previous_boot_id is None or previous_boot_id == boot_id:
        return

    persistent_notification.async_create(
        hass,
        (
            "The Home Assistant host has restarted. The Becker Centronic USB stick may "
            "be visible but unable to transmit radio commands after a cold boot. Unplug "
            "and reconnect the Becker USB stick before operating covers, then verify one "
            "cover physically. Home Assistant cannot confirm that radio commands were "
            "received."
        ),
        title="Becker USB stick: check required after host reboot",
        notification_id=BOOT_NOTIFICATION_ID,
    )


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

    try:
        _async_remove_unused_legacy_hub(hass, entry)
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except Exception:
        await PyBecker.async_shutdown()
        raise
    domain_data[DATA_CONFIG_ENTRY_ACTIVE] = entry.entry_id

    return True


def _async_remove_unused_legacy_hub(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Remove only an unused extra USB device registered by beta 3."""
    device_registry = dr.async_get(hass)
    duplicate_hub = device_registry.async_get_device_by_identifier(
        (DOMAIN, HUB_UNIQUE_ID), entry.entry_id
    )
    if duplicate_hub is None:
        return

    # The entry represents the hub already; never create a replacement device.
    # Preserve ambiguous records and user assignments, even without entities.
    if (
        duplicate_hub.config_entry_id != entry.entry_id
        or duplicate_hub.identifiers != {(DOMAIN, HUB_UNIQUE_ID)}
        or duplicate_hub.connections
        or duplicate_hub.manufacturer != MANUFACTURER
        or duplicate_hub.model != "Centronic USB Stick"
        or duplicate_hub.name != DEFAULT_HUB_TITLE
        or duplicate_hub.config_subentry_id is not None
        or duplicate_hub.composite_device_id is not None
        or duplicate_hub.area_id is not None
        or duplicate_hub.name_by_user is not None
        or duplicate_hub.labels
        or duplicate_hub.via_device_id is not None
    ):
        reason = "legacy identity is ambiguous or device has user assignments"
    elif er.async_entries_for_device(
        er.async_get(hass), duplicate_hub.id, include_disabled_entities=True
    ):
        reason = "device has registered entities (including disabled entities)"
    elif dr.async_entries_for_parent_device(device_registry, duplicate_hub.id):
        reason = "device has child devices"
    elif any(
        device.via_device_id == duplicate_hub.id
        for config_entry in hass.config_entries.async_entries()
        for device in dr.async_entries_for_config_entry(device_registry, config_entry.entry_id)
    ):
        # A via-device link can originate in another integration's entry.
        reason = "device has dependent devices linked by via_device_id"
    else:
        device_registry.async_remove_device(duplicate_hub.id)
        _LOGGER.debug("Removed unused legacy Becker hub device %s", duplicate_hub.id)
        return

    _LOGGER.warning("Keeping legacy Becker hub device %s: %s", duplicate_hub.id, reason)


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
