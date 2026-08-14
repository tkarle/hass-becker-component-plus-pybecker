"""Safe asynchronous controller for the Becker Centronic USB stick."""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import queue
import re
import threading
import time
from dataclasses import dataclass
from typing import Any

from .becker_helper import (
    BeckerConnection,
    BeckerConnectionError,
    PacketParser,
    finalize_code,
    generate_code,
)
from .database import Database, Unit

COMMAND_RELEASE = 0x00
COMMAND_UP = 0x20
COMMAND_UP5 = 0x24
COMMAND_DOWN = 0x40
COMMAND_DOWN5 = 0x44
COMMAND_HALT = 0x10
COMMAND_PAIR2 = 0x81  # single TRAIN telegram; never used for travel commands

COMMUNICATION_DELAY = 0.3
RECONNECT_DELAY = 2.0
QUEUE_SIZE = 100
CHANNEL_PATTERN = re.compile(r"(?:(?P<unit>[1-5]):)?(?P<channel>[1-7]|15)\Z")

_LOGGER = logging.getLogger(__name__)


class BeckerCommandError(RuntimeError):
    """Raised when a command is invalid or cannot be completed safely."""


@dataclass(slots=True)
class _Request:
    operation: str
    args: tuple[Any, ...]
    future: concurrent.futures.Future[Any]


class _BeckerWorker(threading.Thread):
    """Own the serial connection and SQLite connection in exactly one thread."""

    def __init__(self, device: str | None, db_filename: str | None, callback=None) -> None:
        super().__init__(name="becker-centronic", daemon=True)
        self._device = device
        self._db_filename = db_filename
        self._parser = PacketParser(callback)
        self._requests: queue.Queue[_Request] = queue.Queue(maxsize=QUEUE_SIZE)
        self._stop_event = threading.Event()
        self._ready = threading.Event()
        self._connected = threading.Event()
        self._startup_error: BaseException | None = None
        self._connection: BeckerConnection | None = None
        self._database: Database | None = None
        self._next_reconnect = 0.0

    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    def wait_ready(self, timeout: float = 10) -> None:
        """Wait until the database is ready, without requiring the USB device."""
        if not self._ready.wait(timeout):
            raise BeckerConnectionError("Becker worker did not start in time")
        if self._startup_error is not None:
            raise BeckerConnectionError("Becker worker failed to start") from self._startup_error

    def submit(self, operation: str, *args: Any) -> concurrent.futures.Future[Any]:
        """Queue work without ever blocking Home Assistant's event loop."""
        future: concurrent.futures.Future[Any] = concurrent.futures.Future()
        if not self.is_alive() or self._startup_error is not None:
            future.set_exception(BeckerConnectionError("Becker worker is not available"))
            return future
        try:
            self._requests.put_nowait(_Request(operation, args, future))
        except queue.Full:
            future.set_exception(BeckerConnectionError("Becker command queue is full"))
        return future

    def run(self) -> None:
        """Process every database and serial operation in FIFO order."""
        try:
            self._database = Database(self._db_filename)
            self._connection = BeckerConnection(self._device)
        except BaseException as err:  # thread boundary: retain the original cause
            self._startup_error = err
            self._ready.set()
            _LOGGER.exception("Becker worker startup failed")
            return

        self._ready.set()
        _LOGGER.debug("Becker worker started")
        try:
            while not self._stop_event.is_set():
                self._read_once()
                try:
                    request = self._requests.get(timeout=0.05)
                except queue.Empty:
                    continue
                self._handle(request)
        finally:
            self._connected.clear()
            if self._connection is not None:
                self._connection.close()
            if self._database is not None:
                self._database.close()
            self._reject_pending(BeckerConnectionError("Becker worker stopped"))
            _LOGGER.debug("Becker worker stopped")

    def stop_worker(self) -> None:
        self._stop_event.set()

    def _handle(self, request: _Request) -> None:
        if request.future.cancelled():
            return
        try:
            if request.operation == "send":
                result = self._send(*request.args)
            elif request.operation == "list_units":
                assert self._database is not None
                result = [unit.as_legacy_list() for unit in self._database.get_all_units()]
            elif request.operation == "unit_configured":
                assert self._database is not None
                result = self._database.get_unit(int(request.args[0])).configured
            else:
                raise BeckerCommandError(f"Unknown worker operation {request.operation!r}")
        except BaseException as err:  # propagate worker errors to the awaiting coroutine
            request.future.set_exception(err)
        else:
            request.future.set_result(result)

    def _send(self, unit_index: int, channel: int, command: str, test: bool) -> None:
        assert self._database is not None
        assert self._connection is not None

        command_codes = self._command_codes(command)
        current = self._database.get_unit(unit_index)
        if not current.configured and command != "TRAIN":
            raise BeckerCommandError(
                f"Sender unit {unit_index} ({current.code}) is not configured; "
                "copy a valid database or pair it first"
            )

        if not test:
            # Opening happens before reservation: a definitely absent USB device does
            # not burn a counter. Any failure after reservation is treated as ambiguous.
            self._connection.open()
            self._connected.set()

        reserved = self._database.reserve(
            unit_index,
            len(command_codes),
            configure=command == "TRAIN",
            test=test,
        )
        packets = self._packets(channel, reserved, command_codes)
        if test:
            return

        for index, packet in enumerate(packets):
            try:
                self._connection.write(packet)
            except BeckerConnectionError:
                self._connected.clear()
                self._next_reconnect = time.monotonic() + RECONNECT_DELAY
                raise
            PacketParser.log(packet, "Sent packet: ")
            if index + 1 < len(packets):
                time.sleep(COMMUNICATION_DELAY)

    @staticmethod
    def _command_codes(command: str) -> tuple[int, ...]:
        commands = {
            "UP": (COMMAND_UP,),
            "UP2": (COMMAND_UP5,),
            "HALT": (COMMAND_HALT,),
            "RELEASE": (COMMAND_RELEASE,),
            "DOWN": (COMMAND_DOWN,),
            "DOWN2": (COMMAND_DOWN5,),
            # The user's Becker receivers reliably learn from exactly one long
            # programming-button telegram. Multi-frame legacy TRAIN sequences can
            # advance the rolling counter beyond the receiver and are not used.
            "TRAIN": (COMMAND_PAIR2,),
        }
        try:
            return commands[command]
        except KeyError as err:
            raise BeckerCommandError(f"Unsupported Becker command {command!r}") from err

    @staticmethod
    def _packets(channel: int, unit: Unit, commands: tuple[int, ...]) -> list[bytes]:
        packets: list[bytes] = []
        legacy = unit.as_legacy_list()
        for offset, command in enumerate(commands):
            legacy[1] = unit.increment + offset
            packets.append(finalize_code(generate_code(channel, legacy, command)))
        return packets

    def _read_once(self) -> None:
        assert self._connection is not None
        now = time.monotonic()
        if not self._connection.is_open and now < self._next_reconnect:
            return
        try:
            data = self._connection.read()
        except BeckerConnectionError:
            if self._connected.is_set():
                _LOGGER.warning(
                    "Becker USB connection lost; reconnecting in %.1fs",
                    RECONNECT_DELAY,
                )
            self._connected.clear()
            self._next_reconnect = now + RECONNECT_DELAY
            return
        self._connected.set()
        if data:
            self._parser.feed(data)

    def _reject_pending(self, error: BaseException) -> None:
        while True:
            try:
                request = self._requests.get_nowait()
            except queue.Empty:
                return
            if not request.future.done():
                request.future.set_exception(error)


class Becker:
    """Asynchronous facade used by the Home Assistant cover platform."""

    def __init__(self, device_name=None, init_dummy=False, db_filename=None, callback=None):
        if init_dummy:
            raise BeckerCommandError("Random dummy sender initialization is no longer supported")
        self._worker = _BeckerWorker(device_name, db_filename, callback)
        self._worker.start()
        self._worker.wait_ready()

    @property
    def connected(self) -> bool:
        """Return whether the USB connection is currently open."""
        return self._worker.connected

    def close(self) -> None:
        """Stop the worker and close its USB and database resources."""
        self._worker.stop_worker()
        self._worker.join(timeout=10)
        if self._worker.is_alive():
            raise BeckerConnectionError("Becker worker did not stop in time")

    async def _submit(self, operation: str, *args: Any) -> Any:
        future = self._worker.submit(operation, *args)
        return await asyncio.wrap_future(future)

    async def send(self, channel: str, command: str, test: bool = False) -> None:
        """Validate and serialize one logical Becker command."""
        unit, parsed_channel = self._split_channel(channel)
        if not isinstance(command, str):
            raise BeckerCommandError("Becker command must be a string")
        await self._submit("send", unit, parsed_channel, command.upper(), test)

    async def move_up(self, channel: str) -> None:
        await self.send(channel, "UP")

    async def move_up_intermediate(self, channel: str) -> None:
        await self.send(channel, "UP2")

    async def move_down(self, channel: str) -> None:
        await self.send(channel, "DOWN")

    async def move_down_intermediate(self, channel: str) -> None:
        await self.send(channel, "DOWN2")

    async def stop(self, channel: str) -> None:
        await self.send(channel, "HALT")

    async def pair(self, channel: str) -> None:
        """Send exactly one TRAIN telegram for the selected unit/channel."""
        await self.send(channel, "TRAIN")

    async def list_units(self) -> list[list[str | int]]:
        return await self._submit("list_units")

    async def is_unit_configured(self, channel: str) -> bool:
        unit, _ = self._split_channel(channel)
        return bool(await self._submit("unit_configured", unit))

    async def init_unconfigured_unit(self, channel: str, name: str | None = None) -> bool:
        """Compatibility check that no longer mutates counters or sends packets."""
        configured = await self.is_unit_configured(channel)
        if not configured:
            unit, _ = self._split_channel(channel)
            _LOGGER.warning(
                "Sender unit %s%s is not configured; commands are rejected until "
                "a valid database is copied or TRAIN succeeds",
                unit,
                f" for {name}" if name else "",
            )
        return configured

    @staticmethod
    def _split_channel(channel: str) -> tuple[int, int]:
        if not isinstance(channel, str):
            raise BeckerCommandError("Channel must be a quoted string such as '2:4'")
        match = CHANNEL_PATTERN.fullmatch(channel.strip())
        if match is None:
            raise BeckerCommandError(
                "Channel must be '1' to '7', '15', or '<unit 1-5>:<channel>'"
            )
        return int(match.group("unit") or 1), int(match.group("channel"))
