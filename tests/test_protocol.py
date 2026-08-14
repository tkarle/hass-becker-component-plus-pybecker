"""Protocol regression tests based on frames captured from the working installation."""

from __future__ import annotations

import pytest
from pybecker.becker_helper import checksum, generate_code


def test_generate_known_up_frame() -> None:
    """Generate the exact 2:3 UP frame observed on the Raspberry Pi."""
    assert generate_code(3, ["1737c", 0x31A9, 1], 0x20) == (
        "0000000002010B31A90000001737C02101030020C8"
    )


def test_generate_known_down_frame() -> None:
    """Generate the exact 1:1 DOWN frame observed on the Raspberry Pi."""
    assert generate_code(1, ["1737b", 0x0E92, 1], 0x40) == (
        "0000000002010B0E920000001737B02101010040F4"
    )


def test_checksum_rejects_invalid_payload() -> None:
    with pytest.raises(ValueError, match="exactly 40"):
        checksum("00")
    with pytest.raises(ValueError, match="non-hexadecimal"):
        checksum("Z" * 40)
