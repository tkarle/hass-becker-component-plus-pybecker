"""Smoke tests against the pinned Home Assistant beta target."""

from __future__ import annotations

from importlib.metadata import version

from custom_components import becker
from custom_components.becker import cover, rf_device


def test_home_assistant_2026_8_imports() -> None:
    """Load every HA-facing integration module on the target Core version."""
    assert becker and cover and rf_device
    assert version("homeassistant") == "2026.8.1"
