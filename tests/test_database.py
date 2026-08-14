"""Tests for durable rolling-counter reservations."""

from __future__ import annotations

import sqlite3

from pybecker.database import Database


def test_reservation_is_committed_before_use(tmp_path) -> None:
    filename = tmp_path / "centronic-stick.db"
    database = Database(str(filename))
    first = database.reserve(2, 3, configure=True)
    database.close()

    assert first.code == "1737c"
    assert first.increment == 0
    with sqlite3.connect(filename) as connection:
        assert connection.execute(
            "SELECT increment, configured FROM unit WHERE rowid = 2"
        ).fetchone() == (3, 1)


def test_test_reservation_rolls_back(tmp_path) -> None:
    filename = tmp_path / "centronic-stick.db"
    database = Database(str(filename))
    database.reserve(1, 1, configure=True, test=True)
    assert database.get_unit(1).increment == 0
    assert database.get_unit(1).configured is False
    database.close()
