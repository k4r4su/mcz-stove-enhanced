"""Journal « qui a fait quoi » : commandes (app, planning, vacances), changements venus d'ailleurs (Maestro, bouton, télécommande),
allumages sautés et alarmes. Gardé 90 jours, dans le même fichier SQLite que l'historique."""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

KEEP_DAYS = 90
SCHEMA = """
CREATE TABLE IF NOT EXISTS journal (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, stove_id TEXT NOT NULL DEFAULT '',
    origin TEXT NOT NULL, label TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '', outcome TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_journal_ts ON journal (ts);
"""
# origin : app · planning · vacances · externe · poele
# outcome : ok (confirmé) · noop (déjà dans cet état, rien envoyé) · failed (non confirmé / erreur) · skipped (sauté) · info

MODE_NAMES = {"manual": "Manuel", "auto": "Auto", "overnight": "Nuit", "comfort": "Confort"}


def describe(kind: str, value) -> str:
    """Libellé lisible d'une commande."""
    if kind == "power":
        return "Allumage" if value else "Extinction"
    if kind == "setpoint":
        return f"Consigne {float(value):.1f} °C".replace(".", ",")
    if kind == "mode":
        return f"Mode {MODE_NAMES.get(value, value)}"
    if kind == "power_level":
        return f"Puissance {value}"
    if kind == "sleep_timer":
        return f"Arrêt différé {value} min" if value else "Arrêt différé annulé"
    return f"{kind} = {value}"


class Journal:
    def __init__(self, path: str) -> None:
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.executescript(SCHEMA)
        self._db.execute("DELETE FROM journal WHERE ts < ?", (time.time() - KEEP_DAYS * 86400,))
        self._db.commit()

    def add(self, origin: str, label: str, outcome: str, detail: str = "", stove_id: str = "", ts: float | None = None) -> None:
        self._db.execute("INSERT INTO journal (ts, stove_id, origin, label, detail, outcome) VALUES (?,?,?,?,?,?)",
                         (ts if ts is not None else time.time(), stove_id, origin, label, detail, outcome))
        self._db.commit()

    def recent(self, days: float = 7, limit: int = 300) -> list[dict]:
        rows = self._db.execute("SELECT ts, origin, label, detail, outcome FROM journal WHERE ts >= ? ORDER BY ts DESC, id DESC LIMIT ?",
                                (time.time() - days * 86400, limit)).fetchall()
        return [dict(r) for r in rows]
