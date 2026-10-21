"""Historique des relevés (SQLite) : courbes et statistiques de l'app."""
from __future__ import annotations

import sqlite3
from pathlib import Path

from .commands import LIT_PHASES, UNLIT_PHASES
from .snapshot import Snapshot

SCHEMA = """
CREATE TABLE IF NOT EXISTS samples (
    ts REAL NOT NULL, stove_id TEXT NOT NULL, phase TEXT NOT NULL, running INTEGER NOT NULL,
    ambient REAL, setpoint REAL, power_level INTEGER, fume_temp REAL, pellet_low INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_samples ON samples (stove_id, ts);
"""


class History:
    def __init__(self, path: str) -> None:
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.executescript(SCHEMA)
        self._last: dict[str, tuple] = {}

    def record(self, s: Snapshot, min_interval: float = 60.0) -> bool:
        """Enregistre le relevé s'il a changé, ou si le dernier date de plus de `min_interval` secondes."""
        key = (s.phase, s.running, s.ambient, s.setpoint, s.power_level, s.pellet_low)
        previous = self._last.get(s.stove_id)
        if previous and previous[0] == key and s.fetched_at - previous[1] < min_interval:
            return False
        self._last[s.stove_id] = (key, s.fetched_at)
        self._db.execute(
            "INSERT INTO samples VALUES (?,?,?,?,?,?,?,?,?)",
            (s.fetched_at, s.stove_id, s.phase, int(s.running), s.ambient, s.setpoint, s.power_level, s.fume_temp, int(s.pellet_low)),
        )
        self._db.commit()
        return True

    def since(self, stove_id: str, ts: float) -> list[dict]:
        rows = self._db.execute(
            "SELECT ts, phase, running, ambient, setpoint, power_level, fume_temp, pellet_low "
            "FROM samples WHERE stove_id = ? AND ts >= ? ORDER BY ts", (stove_id, ts),
        ).fetchall()
        keys = ("ts", "phase", "running", "ambient", "setpoint", "power_level", "fume_temp", "pellet_low")
        out = [dict(zip(keys, row)) for row in rows]
        for r in out:
            r["running"], r["pellet_low"] = bool(r["running"]), bool(r["pellet_low"])  # SQLite stocke 0/1
            # Les relevés d'avant le 14/10/2026 ~8 h ont `running` d'après power_enabled, vrai même poêle éteint : la phase fait foi.
            if r["phase"] in LIT_PHASES or r["phase"] in UNLIT_PHASES:
                r["running"] = r["phase"] in LIT_PHASES
        return out
