"""SQLite-backed rolling counter storage for Becker sender units."""

from __future__ import annotations

import logging
import os
import sqlite3
import time
from dataclasses import dataclass

NUMBER_FILE = "centronic-stick.num"
SQL_DB_FILE = "centronic-stick.db"
FILE_PATH = os.path.dirname(os.path.realpath(__file__))

_LOGGER = logging.getLogger(__name__)


class DatabaseError(RuntimeError):
    """Raised when sender state cannot be read or reserved safely."""


@dataclass(frozen=True, slots=True)
class Unit:
    """A Becker sender unit at the point before counters are reserved."""

    code: str
    increment: int
    configured: bool

    def as_legacy_list(self) -> list[str | int]:
        """Return the format expected by the original code generator."""
        return [self.code, self.increment, int(self.configured)]


class Database:
    """Own the sender database from a single worker thread.

    A counter reservation is committed with SQLite synchronous=FULL before any
    corresponding packet is written to the USB stick. A failed or ambiguous
    write therefore consumes a counter instead of risking its reuse.
    """

    def __init__(self, filename: str | None = None) -> None:
        self.filename = filename or os.path.join(FILE_PATH, SQL_DB_FILE)
        self.conn = sqlite3.connect(self.filename, timeout=10)
        self.conn.execute("PRAGMA synchronous=FULL")
        self.conn.execute("PRAGMA busy_timeout=10000")
        self.check()

    def __enter__(self) -> Database:
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()

    def close(self) -> None:
        """Close the database connection."""
        self.conn.close()

    def check(self) -> None:
        """Create the database schema when no unit table exists."""
        row = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='unit'"
        ).fetchone()
        if row is None:
            self.create()

    def create(self) -> None:
        """Create the database with the five sender IDs supported by the stick."""
        _LOGGER.info("Create Becker sender database at %s", self.filename)
        with self.conn:
            self.conn.execute(
                "CREATE TABLE unit ("
                "code NVARCHAR(5), increment INTEGER, configured BIT, "
                "executed INTEGER, UNIQUE(code))"
            )
            self.conn.executemany(
                "INSERT INTO unit VALUES (?, 0, 0, 0)",
                [(f"1737{suffix}",) for suffix in "bcdef"],
            )

    def get_unit(self, rowid: int) -> Unit:
        """Return one sender unit by its stable 1-based slot."""
        row = self.conn.execute(
            "SELECT code, increment, configured FROM unit WHERE rowid = ?", (rowid,)
        ).fetchone()
        if row is None:
            raise DatabaseError(f"Unknown Becker sender unit {rowid}; expected 1-5")
        return Unit(str(row[0]), int(row[1]), bool(row[2]))

    def get_all_units(self, *, configured_only: bool = True) -> list[Unit]:
        """Return sender units in their stable database order."""
        sql = "SELECT code, increment, configured FROM unit"
        if configured_only:
            sql += " WHERE configured = 1"
        sql += " ORDER BY rowid ASC"
        return [Unit(str(row[0]), int(row[1]), bool(row[2])) for row in self.conn.execute(sql)]

    def reserve(
        self,
        rowid: int,
        count: int,
        *,
        configure: bool = False,
        test: bool = False,
    ) -> Unit:
        """Durably reserve ``count`` rolling counters and return the first one.

        The returned unit contains the counter value from before the reservation.
        Callers generate consecutive frames from that value. In test mode the
        transaction is rolled back and the real database remains unchanged.
        """
        if count < 1:
            raise ValueError("Counter reservation must contain at least one frame")

        try:
            self.conn.execute("BEGIN IMMEDIATE")
            unit = self.get_unit(rowid)
            next_increment = unit.increment + count
            configured = unit.configured or configure
            self.conn.execute(
                "UPDATE unit SET increment = ?, configured = ?, executed = ? "
                "WHERE rowid = ?",
                (next_increment, int(configured), int(time.time()), rowid),
            )
            if test:
                self.conn.rollback()
            else:
                self.conn.commit()
            return unit
        except (sqlite3.Error, DatabaseError):
            self.conn.rollback()
            raise

    def output(self) -> None:
        """Log all sender units without relying on implicit SQLite ordering."""
        _LOGGER.info("%-10s%-18s%-12s", "code", "increment", "configured")
        for unit in self.get_all_units(configured_only=False):
            _LOGGER.info(
                "%-10s%-18s%-12s", unit.code, unit.increment, unit.configured
            )
