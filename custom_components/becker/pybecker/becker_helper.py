"""Protocol and serial helpers for the Becker Centronic USB stick."""

from __future__ import annotations

import logging
import os
import re
import sys
from collections.abc import Callable
from typing import Any

import serial
import serial.tools.list_ports

STX = b"\x02"
ETX = b"\x03"

CODE_PREFIX = "0000000002010B"
CODE_SUFFIX = "000000"
CODE_21 = "021"
CODE_REMOTE = "01"

# Becker uses the product/manufacturer strings verbatim in the persistent Linux
# symlink. The mixed-case form matches current sticks, including the test unit.
DEFAULT_DEVICE_NAME = (
    "/dev/serial/by-id/"
    "usb-Becker-Antriebe_GmbH_CDC_RS232_v125_Centronic-if00"
)

MESSAGE = re.compile(
    STX
    + CODE_PREFIX.encode()
    + rb"[0-9A-F]{4}"
    + CODE_SUFFIX.encode()
    + rb"(?P<unit_id>[0-9A-F]{5})"
    + rb"[0-9A-F]{6}"
    + rb"(?P<channel>[0-9A-F])"
    + rb"00"
    + rb"(?P<command>[0-9A-F])"
    + rb"(?P<argument>[0-9A-F])"
    + rb"[0-9A-F]{2}"
    + ETX,
    re.I,
)

COMMANDS = {b"0": "RELEASE", b"1": "HALT", b"2": "UP", b"4": "DOWN", b"8": "TRAIN"}

_LOGGER = logging.getLogger(__name__)


def hex2(number: int) -> str:
    """Return the low byte as two uppercase hexadecimal characters."""
    return f"{number & 0xFF:02X}"


def hex4(number: int) -> str:
    """Return the low word as four uppercase hexadecimal characters."""
    return f"{number & 0xFFFF:04X}"


def checksum(code: str) -> str:
    """Append the Becker checksum to a 40-character payload."""
    if len(code) != 40:
        raise ValueError("Becker payload must be exactly 40 hexadecimal characters")
    try:
        code_sum = sum(int(code[index : index + 2], 16) for index in range(0, 40, 2))
    except ValueError as err:
        raise ValueError("Becker payload contains non-hexadecimal characters") from err
    return f"{code.upper()}{hex2(0x03 - code_sum)}"


def generate_code(
    channel: int,
    unit: list[str | int],
    command_code: int,
    with_checksum: bool = True,
) -> str:
    """Generate one Becker protocol packet without STX/ETX framing."""
    if not 0 <= channel <= 15:
        raise ValueError("Channel must fit in one hexadecimal digit")
    if not 0 <= command_code <= 0xFF:
        raise ValueError("Command must fit in one byte")

    unit_id = str(unit[0]).upper()
    if not re.fullmatch(r"[0-9A-F]{5}", unit_id):
        raise ValueError("Sender unit ID must contain exactly five hexadecimal characters")
    unit_increment = int(unit[1])
    remote = "00" if channel == 0 else CODE_REMOTE
    code = (
        f"{CODE_PREFIX}{hex4(unit_increment)}{CODE_SUFFIX}{unit_id}"
        f"{CODE_21}{remote}{hex2(channel)}00{hex2(command_code)}"
    )
    return checksum(code) if with_checksum else code


def finalize_code(code: str) -> bytes:
    """Add STX/ETX framing to a generated code."""
    return b"".join((STX, code.encode("ascii"), ETX))


class BeckerConnectionError(RuntimeError):
    """Raised when the Becker USB connection cannot safely complete an action."""


class BeckerConnection:
    """Lazy, exclusive connection to a serial device or pySerial URL."""

    def __init__(self, device: str | None) -> None:
        self._device, self._is_serial = self._normalise_device(device)
        kwargs: dict[str, Any] = {
            "baudrate": 115200,
            "timeout": 0.05,
            "write_timeout": 1,
            "do_not_open": True,
        }
        if self._is_serial and os.name == "posix":
            kwargs["exclusive"] = True
        try:
            self._connection = serial.serial_for_url(self._device, **kwargs)
        except (serial.SerialException, ValueError) as err:
            raise BeckerConnectionError(
                f"Cannot prepare Becker connection for {self._device}"
            ) from err

    @property
    def is_serial(self) -> bool:
        return self._is_serial

    @property
    def device(self) -> str:
        return self._device

    @property
    def is_open(self) -> bool:
        return bool(self._connection.is_open)

    def open(self) -> None:
        """Open the device; missing USB devices remain retryable."""
        if self._connection.is_open:
            return
        try:
            self._connection.open()
        except (serial.SerialException, OSError) as err:
            self.close()
            raise BeckerConnectionError(
                f"Cannot open Becker connection {self.device}"
            ) from err

    def write(self, packet: bytes) -> None:
        """Write exactly one packet without retrying an ambiguous write."""
        self.open()
        try:
            written = self._connection.write(packet)
            self._connection.flush()
        except (serial.SerialException, OSError) as err:
            self.close()
            raise BeckerConnectionError(
                f"Write to Becker connection {self.device} failed; counter consumed"
            ) from err
        if written != len(packet):
            self.close()
            raise BeckerConnectionError(
                f"Short Becker write ({written}/{len(packet)} bytes); counter consumed"
            )

    def read(self) -> bytes:
        """Read available protocol data."""
        self.open()
        try:
            return self._connection.read(1024)
        except (serial.SerialException, OSError) as err:
            self.close()
            raise BeckerConnectionError(
                f"Read from Becker connection {self.device} failed"
            ) from err

    def close(self) -> None:
        """Close the connection if it is open."""
        try:
            if self._connection.is_open:
                self._connection.close()
        except (serial.SerialException, OSError):
            _LOGGER.debug("Error while closing %s", self.device, exc_info=True)

    @staticmethod
    def _normalise_device(device: str | None) -> tuple[str, bool]:
        if device is None:
            device = DEFAULT_DEVICE_NAME
        if not isinstance(device, str) or not device.strip():
            raise BeckerConnectionError("Becker device must be a non-empty string")
        device = device.strip()
        if device.startswith("/dev/") or (
            sys.platform.startswith("win") and device.upper().startswith("COM")
        ):
            return device, True
        if "/" in device or "://" in device:
            return device, False
        if ":" not in device:
            device = f"{device}:5000"
        return f"socket://{device}", False


class PacketParser:
    """Incrementally parse received Becker frames."""

    def __init__(self, callback: Callable[[re.Match[bytes]], Any] | None = None) -> None:
        self._callback = callback
        self._buffer = b""

    def feed(self, data: bytes) -> None:
        """Append bytes and emit every complete valid packet."""
        self._buffer += data
        end = 0
        for packet in MESSAGE.finditer(self._buffer):
            self.log(packet.group(0), "Received packet: ")
            if self._callback is not None:
                self._callback(packet)
            end = packet.end()
        if end:
            self._buffer = self._buffer[end:]
        elif len(self._buffer) > 4096:
            # Keep enough tail for a partial 44-byte frame without unbounded growth.
            self._buffer = self._buffer[-64:]

    @staticmethod
    def log(packet: bytes, text: str = "") -> None:
        """Log a decoded packet at debug level."""
        if not _LOGGER.isEnabledFor(logging.DEBUG):
            return
        match = MESSAGE.search(packet)
        if match is None:
            return
        command = COMMANDS.get(match.group("command"), match.group("command").decode())
        _LOGGER.debug(
            "%sunit_id: %s, channel: %s, command: %s, argument: %s, packet: %s",
            text,
            match.group("unit_id").decode(),
            match.group("channel").decode(),
            command,
            match.group("argument").decode(),
            match.group(0),
        )
