"""What the plant produced per day, month and year, kept in SQLite.

Figures come from two places: the Northbound API, which knows the past, and
the kiosk's running totals, which the bridge writes down as they grow. An API
figure always replaces a kiosk one for the same period.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any

SOURCES = {"kiosk": 1, "api": 2}


class Ledger:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS yields (span TEXT NOT NULL, key TEXT NOT NULL, kwh REAL NOT NULL, source TEXT NOT NULL, at REAL NOT NULL, PRIMARY KEY (span, key));
            CREATE TABLE IF NOT EXISTS fetched (span TEXT NOT NULL, key TEXT NOT NULL, at REAL NOT NULL, PRIMARY KEY (span, key));
            """
        )

    def put(self, span: str, key: str, kwh: float, source: str, at: float | None = None, running: bool = False) -> None:
        """Store a figure. For a finished period the better source wins (the API over the kiosk).
        For a period still `running` the total only grows, so the larger figure wins whoever gave it:
        the kiosk is minutes old there, the API can be hours behind."""
        row = self.db.execute("SELECT kwh, source FROM yields WHERE span = ? AND key = ?", (span, key)).fetchone()
        if row is not None:
            if running or row[1] == source == "kiosk":
                if kwh < row[0]:
                    return
            elif SOURCES[row[1]] > SOURCES[source]:
                return
        self.db.execute("INSERT OR REPLACE INTO yields VALUES (?, ?, ?, ?, ?)", (span, key, float(kwh), source, at or time.time()))
        self.db.commit()

    def items(self, span: str, prefix: str = "") -> list[dict[str, Any]]:
        rows = self.db.execute("SELECT key, kwh, source FROM yields WHERE span = ? AND key LIKE ? ORDER BY key", (span, prefix + "%")).fetchall()
        return [{"key": key, "kwh": round(kwh, 2), "source": source} for key, kwh, source in rows]

    def mark_fetched(self, span: str, key: str, at: float | None = None) -> None:
        self.db.execute("INSERT OR REPLACE INTO fetched VALUES (?, ?, ?)", (span, key, at or time.time()))
        self.db.commit()

    def fetched_at(self, span: str, key: str) -> float | None:
        row = self.db.execute("SELECT at FROM fetched WHERE span = ? AND key = ?", (span, key)).fetchone()
        return row[0] if row else None

    def forget_api(self) -> None:
        """Drop what the API account gave, when the account is changed or removed."""
        self.db.execute("DELETE FROM yields WHERE source = 'api'")
        self.db.execute("DELETE FROM fetched")
        self.db.commit()
