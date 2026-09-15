"""Tests for the cold-host-reboot warning."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, Mock, patch

from custom_components.becker import (
    BOOT_NOTIFICATION_ID,
    _async_warn_after_host_reboot,
)


def test_first_boot_id_is_stored_without_warning() -> None:
    """Installing the feature initializes state without creating a false alarm."""
    hass = Mock()
    store = Mock()
    store.async_load = AsyncMock(return_value=None)
    store.async_save = AsyncMock()

    async def exercise() -> None:
        with (
            patch(
                "custom_components.becker._async_read_host_boot_id",
                AsyncMock(return_value="boot-a"),
            ),
            patch("custom_components.becker.Store", return_value=store),
            patch("custom_components.becker.persistent_notification.async_create") as create,
        ):
            await _async_warn_after_host_reboot(hass)
            create.assert_not_called()

    asyncio.run(exercise())
    store.async_save.assert_awaited_once_with({"boot_id": "boot-a"})


def test_changed_boot_id_creates_persistent_warning() -> None:
    """A real host reboot creates one visible warning with a stable ID."""
    hass = Mock()
    store = Mock()
    store.async_load = AsyncMock(return_value={"boot_id": "boot-a"})
    store.async_save = AsyncMock()

    async def exercise() -> None:
        with (
            patch(
                "custom_components.becker._async_read_host_boot_id",
                AsyncMock(return_value="boot-b"),
            ),
            patch("custom_components.becker.Store", return_value=store),
            patch("custom_components.becker.persistent_notification.async_create") as create,
        ):
            await _async_warn_after_host_reboot(hass)
            create.assert_called_once()
            assert create.call_args.kwargs["notification_id"] == BOOT_NOTIFICATION_ID

    asyncio.run(exercise())
    store.async_save.assert_awaited_once_with({"boot_id": "boot-b"})


def test_same_boot_id_does_not_warn_on_core_restart() -> None:
    """Restarting only Home Assistant Core must not create a warning."""
    hass = Mock()
    store = Mock()
    store.async_load = AsyncMock(return_value={"boot_id": "boot-a"})
    store.async_save = AsyncMock()

    async def exercise() -> None:
        with (
            patch(
                "custom_components.becker._async_read_host_boot_id",
                AsyncMock(return_value="boot-a"),
            ),
            patch("custom_components.becker.Store", return_value=store),
            patch("custom_components.becker.persistent_notification.async_create") as create,
        ):
            await _async_warn_after_host_reboot(hass)
            create.assert_not_called()

    asyncio.run(exercise())

