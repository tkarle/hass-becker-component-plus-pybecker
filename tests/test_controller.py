"""Concurrency and failure-safety tests for the controller worker."""

from __future__ import annotations

import asyncio

import pytest
from pybecker.becker import Becker, BeckerCommandError
from pybecker.becker_helper import BeckerConnectionError
from pybecker.database import Database


def _unit_increment(filename, unit=2) -> int:
    with Database(str(filename)) as database:
        return database.get_unit(unit).increment


def test_train_is_one_frame_and_concurrent_commands_are_serialized(tmp_path) -> None:
    filename = tmp_path / "centronic-stick.db"

    async def exercise() -> None:
        controller = Becker(device_name="loop://", db_filename=str(filename))
        try:
            await controller.pair("2:3")
            await asyncio.gather(*(controller.move_up("2:3") for _ in range(12)))
        finally:
            controller.close()

    asyncio.run(exercise())
    # One TRAIN counter and twelve unique UP counters were reserved.
    assert _unit_increment(filename) == 13


def test_unknown_commands_and_channels_are_rejected(tmp_path) -> None:
    filename = tmp_path / "centronic-stick.db"

    async def exercise() -> None:
        controller = Becker(device_name="loop://", db_filename=str(filename))
        try:
            with pytest.raises(BeckerCommandError, match="Unsupported"):
                await controller.send("1:1", "RESET")
            with pytest.raises(BeckerCommandError, match="Channel"):
                await controller.send("2:8", "UP")
        finally:
            controller.close()

    asyncio.run(exercise())
    assert _unit_increment(filename, unit=1) == 0


def test_ambiguous_write_failure_consumes_counter(tmp_path) -> None:
    filename = tmp_path / "centronic-stick.db"

    async def exercise() -> None:
        controller = Becker(device_name="loop://", db_filename=str(filename))
        try:
            await controller.pair("2:3")

            def fail_write(_packet: bytes) -> None:
                raise BeckerConnectionError("simulated ambiguous write")

            controller._worker._connection.write = fail_write  # type: ignore[method-assign]
            with pytest.raises(BeckerConnectionError, match="ambiguous"):
                await controller.move_up("2:3")
        finally:
            controller.close()

    asyncio.run(exercise())
    # TRAIN used counter 0. The failed UP reserved counter 1 and must not reuse it.
    assert _unit_increment(filename) == 2
