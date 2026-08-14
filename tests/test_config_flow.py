"""Safety tests for the Becker UI configuration flow."""

from __future__ import annotations

import asyncio
import hashlib
import sqlite3
from pathlib import Path

import pytest
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
    validate_database,
)
from custom_components.becker.const import (
    CONF_CHANNEL,
    CONF_INTERMEDIATE_POSITION,
    CONF_MIGRATION_PENDING,
    DATA_YAML_CONFIG,
    DOMAIN,
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
