"""Setup safety and legacy hub cleanup against the real HA registries."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import MappingProxyType, SimpleNamespace
from unittest.mock import AsyncMock, PropertyMock, patch

import pytest
from homeassistant.config_entries import ConfigEntries, ConfigEntry
from homeassistant.const import CONF_DEVICE, CONF_FILENAME
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from custom_components import becker
from custom_components.becker.const import (
    CONF_MIGRATION_PENDING,
    DATA_CONFIG_ENTRY_ACTIVE,
    DEFAULT_HUB_TITLE,
    DOMAIN,
    HUB_UNIQUE_ID,
    MANUFACTURER,
)


@pytest.fixture
def registry_env(tmp_path):
    """Use real entries and registries; mock only platform and hardware setup."""
    @asynccontextmanager
    async def create():
        hass = HomeAssistant(str(tmp_path))
        hass.config_entries = ConfigEntries(hass, {})
        entries = [
            ConfigEntry(
                domain=DOMAIN,
                title=f"Becker {index}",
                entry_id=f"becker-{index}",
                data={CONF_DEVICE: "/dev/mock-becker", CONF_FILENAME: "/mock/existing.db"},
                options={},
                version=1,
                minor_version=1,
                source="user",
                unique_id=f"hub-{index}",
                discovery_keys=MappingProxyType({}),
                subentries_data=[],
            )
            for index in range(3)
        ]
        with patch.object(hass.config_entries, "async_setup", AsyncMock(return_value=True)):
            for entry in entries:
                await hass.config_entries.async_add(entry)
        dr.async_setup(hass)
        devices = dr.async_get(hass)
        entities = er.async_get(hass)
        await devices.async_load(load_empty=True)
        await entities.async_load(load_empty=True)
        try:
            with (
                patch.object(becker.PyBecker, "async_setup", AsyncMock()) as setup,
                patch.object(becker.PyBecker, "async_shutdown", AsyncMock()) as shutdown,
                patch.object(
                    hass.config_entries, "async_forward_entry_setups", AsyncMock()
                ) as forward,
                patch.object(
                    hass.config_entries, "async_unload_platforms", AsyncMock(return_value=True)
                ) as unload,
                patch.object(
                    dr.DeviceRegistry, "async_get_device", side_effect=AssertionError("old lookup")
                ),
                patch.object(
                    dr.DeviceEntry, "config_entries", new_callable=PropertyMock,
                    side_effect=AssertionError("old ownership"),
                ),
                patch.object(dr, "report_usage", side_effect=AssertionError("deprecated registry")),
            ):
                yield SimpleNamespace(
                    hass=hass, entry=entries[0], others=entries[1:], devices=devices,
                    entities=entities, setup=setup, shutdown=shutdown,
                    forward=forward, unload=unload,
                )
        finally:
            await hass.async_block_till_done()
            await hass.async_stop()
    return create


def legacy_hub(env, entry=None, **overrides):
    """Reproduce exactly the device registered in beta 3."""
    return env.devices.async_get_or_create(**{
        "config_entry_id": (entry or env.entry).entry_id,
        "identifiers": {(DOMAIN, HUB_UNIQUE_ID)},
        "manufacturer": MANUFACTURER,
        "model": "Centronic USB Stick",
        "name": DEFAULT_HUB_TITLE,
        **overrides,
    })


def test_setup_without_legacy_hub(registry_env):
    """Normal setup never registers an extra hub and requires the existing DB."""
    async def exercise():
        async with registry_env() as env:
            with patch.object(env.devices, "async_remove_device") as remove:
                assert await becker.async_setup_entry(env.hass, env.entry)
            remove.assert_not_called()
            assert dr.async_entries_for_config_entry(env.devices, env.entry.entry_id) == []
            env.setup.assert_awaited_once_with(
                env.hass, device="/dev/mock-becker", filename="/mock/existing.db",
                require_existing_database=True,
            )
            assert env.hass.data[DOMAIN][DATA_CONFIG_ENTRY_ACTIVE] == env.entry.entry_id
    asyncio.run(exercise())


@pytest.mark.parametrize("own_hub", [False, True])
def test_scoped_cleanup_and_reload(registry_env, own_hub):
    """Foreign duplicate identifiers survive, even when registered first."""
    async def exercise():
        async with registry_env() as env:
            foreign = [legacy_hub(env, entry) for entry in env.others]
            own = legacy_hub(env) if own_hub else None
            with patch.object(
                env.devices, "async_remove_device", wraps=env.devices.async_remove_device
            ) as remove:
                for _ in range(2):
                    assert await becker.async_setup_entry(env.hass, env.entry)
                    assert await becker.async_unload_entry(env.hass, env.entry)
                assert remove.call_count == int(own_hub)
                if own:
                    remove.assert_called_once_with(own.id)
                    assert env.devices.async_get(own.id) is None
            for device in foreign:
                assert env.devices.async_get(device.id) == device
            assert dr.async_entries_for_config_entry(env.devices, env.entry.entry_id) == []
            assert env.setup.await_count == env.shutdown.await_count == 2
    asyncio.run(exercise())


@pytest.mark.parametrize("disabled_by", [None, er.RegistryEntryDisabler.USER])
def test_preserve_registered_entities(registry_env, disabled_by, caplog):
    """Both active and disabled entity assignments block removal."""
    async def exercise():
        async with registry_env() as env:
            hub = legacy_hub(env)
            entity = env.entities.async_get_or_create(
                "sensor", DOMAIN, "legacy-entity", config_entry=env.entry,
                device_id=hub.id, disabled_by=disabled_by,
            )
            assert await becker.async_setup_entry(env.hass, env.entry)
            assert env.devices.async_get(hub.id) == hub
            assert env.entities.async_get(entity.entity_id) == entity
            assert "registered entities" in caplog.text
    asyncio.run(exercise())


@pytest.mark.parametrize("dependency", ["child", "via-own", "via-foreign"])
def test_preserve_dependent_devices(registry_env, dependency, caplog):
    """Do not cascade-delete children or clear via links, including foreign ones."""
    async def exercise():
        async with registry_env() as env:
            hub = legacy_hub(env)
            if dependency == "child":
                dependent = env.devices.async_get_or_create_child(
                    config_entry_id=env.entry.entry_id, identifiers={(DOMAIN, "child")},
                    parent_device_id=hub.id, name="Child",
                )
            else:
                entry = env.others[0] if dependency == "via-foreign" else env.entry
                dependent = env.devices.async_get_or_create(
                    config_entry_id=entry.entry_id, identifiers={(DOMAIN, "dependent")},
                    via_device_id=hub.id,
                )
            assert await becker.async_setup_entry(env.hass, env.entry)
            assert env.devices.async_get(hub.id) == hub
            assert env.devices.async_get(dependent.id) == dependent
            assert "Keeping legacy Becker hub" in caplog.text
    asyncio.run(exercise())


@pytest.mark.parametrize("change", [
    {"model": "Another device"},
    {"identifiers": {(DOMAIN, HUB_UNIQUE_ID), (DOMAIN, "cover_1_1")}},
    {"connections": {("mac", "aa:bb:cc:dd:ee:ff")}},
    {"name_by_user": "My hub"},
    {"area_id": "living-room"},
    {"labels": {"keep"}},
])
def test_preserve_ambiguous_or_customized_hub(registry_env, change, caplog):
    """A matching identifier alone does not authorize losing user data."""
    async def exercise():
        async with registry_env() as env:
            hub = legacy_hub(env)
            if "identifiers" in change:
                update = {"new_identifiers": change["identifiers"]}
            elif "connections" in change:
                update = {"new_connections": change["connections"]}
            else:
                update = change
            hub = env.devices.async_update_device(hub.id, **update)
            assert await becker.async_setup_entry(env.hass, env.entry)
            assert env.devices.async_get(hub.id) == hub
            assert "ambiguous or device has user assignments" in caplog.text
    asyncio.run(exercise())


def test_cover_device_and_assignments_unchanged(registry_env):
    """Removing an unrelated old hub preserves cover/entity identities and settings."""
    async def exercise():
        async with registry_env() as env:
            hub = legacy_hub(env)
            cover = env.devices.async_get_or_create(
                config_entry_id=env.entry.entry_id, identifiers={(DOMAIN, "2:4")},
                manufacturer=MANUFACTURER, name="Wohnzimmer rechts",
            )
            cover = env.devices.async_update_device(
                cover.id, area_id="living-room", name_by_user="My blind", labels={"sun"}
            )
            entity = env.entities.async_get_or_create(
                "cover", DOMAIN, "becker_2_4", config_entry=env.entry,
                device_id=cover.id, suggested_object_id="wohnzimmer_rechts",
            )
            assert await becker.async_setup_entry(env.hass, env.entry)
            await env.hass.async_block_till_done()
            assert env.devices.async_get(hub.id) is None
            assert env.devices.async_get(cover.id) == cover
            assert env.entities.async_get(entity.entity_id) == entity
    asyncio.run(exercise())


def test_reject_wrong_owner_even_if_lookup_returns_it(registry_env, caplog):
    """Keep the explicit ownership defense as well as the scoped query."""
    async def exercise():
        async with registry_env() as env:
            foreign = legacy_hub(env, env.others[0])
            with patch.object(
                env.devices, "async_get_device_by_identifier", return_value=foreign
            ) as lookup:
                assert await becker.async_setup_entry(env.hass, env.entry)
            lookup.assert_called_once_with((DOMAIN, HUB_UNIQUE_ID), env.entry.entry_id)
            assert env.devices.async_get(foreign.id) == foreign
            assert "Keeping legacy Becker hub" in caplog.text
    asyncio.run(exercise())


def test_pending_yaml_import_is_passive(registry_env):
    """Do not open hardware or mutate registries before YAML migration completes."""
    async def exercise():
        async with registry_env() as env:
            env.hass.config_entries.async_update_entry(
                env.entry, data={**env.entry.data, CONF_MIGRATION_PENDING: True}
            )
            hub = legacy_hub(env)
            assert await becker.async_setup_entry(env.hass, env.entry)
            env.setup.assert_not_awaited()
            env.forward.assert_not_awaited()
            assert env.devices.async_get(hub.id) == hub
    asyncio.run(exercise())


def test_single_active_hub(registry_env):
    """A second hub cannot open the stick or trigger cleanup."""
    async def exercise():
        async with registry_env() as env:
            assert await becker.async_setup_entry(env.hass, env.entry)
            with pytest.raises(ConfigEntryError, match="Only one"):
                await becker.async_setup_entry(env.hass, env.others[0])
            assert env.setup.await_count == 1
    asyncio.run(exercise())


@pytest.mark.parametrize("failure", ["registry", "platform"])
def test_setup_failure_closes_serial_resources(registry_env, failure):
    """Registry or platform errors after opening the stick always shut it down."""
    async def exercise():
        async with registry_env() as env:
            if failure == "platform":
                env.forward.side_effect = RuntimeError("platform failed")
            with patch.object(
                env.devices, "async_get_device_by_identifier",
                side_effect=RuntimeError("registry failed") if failure == "registry" else None,
                return_value=None,
            ), pytest.raises(RuntimeError, match="failed"):
                await becker.async_setup_entry(env.hass, env.entry)
            env.shutdown.assert_awaited_once()
            assert DATA_CONFIG_ENTRY_ACTIVE not in env.hass.data[DOMAIN]
    asyncio.run(exercise())


def test_failed_unload_keeps_hardware(registry_env):
    """Do not shut down hardware while platform unloading failed."""
    async def exercise():
        async with registry_env() as env:
            assert await becker.async_setup_entry(env.hass, env.entry)
            env.unload.return_value = False
            assert not await becker.async_unload_entry(env.hass, env.entry)
            env.shutdown.assert_not_awaited()
            assert env.hass.data[DOMAIN][DATA_CONFIG_ENTRY_ACTIVE] == env.entry.entry_id
    asyncio.run(exercise())
