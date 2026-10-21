"""Programmation : plages hebdomadaires, minuteries ponctuelles (« éteindre à 22:30 »), remplissages de pellets.

Tout est stocké en SQLite (même fichier que l'historique) et évalué par `due()` : une fonction pure (heure donnée → actions à jouer), donc testable.

S'y ajoutent :
 - l'allumage anticipé : une plage `anticipate` donne l'heure où il doit faire chaud ; l'allumage part plus tôt, d'après `lead_for` (anticipation.py) ;
 - les jours sautés (« pas d'allumage ce jour-là ») ;
 - une absence : pause (aucun allumage jusqu'à une date) ou vacances (extinction au départ, aucun allumage, préchauffage avant le retour).
"""
from __future__ import annotations

import json
import sqlite3
import uuid
import time as clock
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from datetime import date as Date
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

GRACE_PLAGE = timedelta(minutes=10)   # un événement raté (serveur redémarré) est encore joué dans ce délai
GRACE_TIMER = timedelta(minutes=30)
MAX_LEAD = timedelta(minutes=180)      # un allumage anticipé ne part jamais plus de 3 h avant l'heure voulue

SCHEMA = """
CREATE TABLE IF NOT EXISTS plages (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, days TEXT NOT NULL,
    start TEXT NOT NULL, end TEXT NOT NULL, setpoint REAL, mode TEXT, enabled INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS timers (id INTEGER PRIMARY KEY AUTOINCREMENT, stove_id TEXT NOT NULL, action TEXT NOT NULL,
    at_ts REAL NOT NULL, status TEXT NOT NULL DEFAULT 'pending');
CREATE TABLE IF NOT EXISTS runs (key TEXT PRIMARY KEY, ts REAL NOT NULL, outcome TEXT NOT NULL, label TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS sessions (digest TEXT PRIMARY KEY, expires REAL NOT NULL);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS refills (id INTEGER PRIMARY KEY AUTOINCREMENT, stove_id TEXT NOT NULL, ts REAL NOT NULL, kg REAL, full INTEGER NOT NULL DEFAULT 0);
"""


@dataclass(frozen=True)
class Action:
    """Une chose à faire maintenant : une suite de commandes absolues pour chaque poêle visé."""
    key: str                                  # clé d'idempotence (ne rejoue pas deux fois le même événement)
    label: str
    steps: tuple[tuple[str, object], ...]     # [("power", True), ("setpoint", 21.5), ...]
    stove_id: str | None = None               # None = tous les poêles
    timer_id: int | None = None
    origin: str = "planning"                  # planning · vacances (pour le journal)
    detail: str = ""                          # d'où vient l'action, en clair (« plage Chauffage », « minuterie »…)
    skip_reason: str | None = None            # action sautée : rien n'est envoyé, on le note seulement


def parse_hhmm(value: str) -> time:
    try:
        hours, minutes = value.split(":")
        return time(int(hours), int(minutes))
    except (ValueError, TypeError):
        raise ValueError("heure attendue au format HH:MM")


class Planner:
    def __init__(self, path: str, tz: ZoneInfo) -> None:
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.tz = tz
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.executescript(SCHEMA)
        for migration in ("ALTER TABLE runs ADD COLUMN label TEXT NOT NULL DEFAULT ''",        # bases plus anciennes
                          "ALTER TABLE plages ADD COLUMN anticipate INTEGER NOT NULL DEFAULT 0"):
            try:
                self._db.execute(migration)
            except sqlite3.OperationalError:
                pass
        # Minutes d'avance d'un allumage anticipé pour cette plage (remplacé par l'estimation apprise, voir anticipation.py).
        self.lead_for: Callable[[dict], int] = lambda plage: 60

    # ---- Plages hebdomadaires -------------------------------------------------

    def list_plages(self) -> list[dict]:
        return [self._plage(r) for r in self._db.execute("SELECT * FROM plages ORDER BY id")]

    @staticmethod
    def _plage(row: sqlite3.Row) -> dict:
        return {"id": row["id"], "name": row["name"], "days": json.loads(row["days"]), "start": row["start"], "end": row["end"],
                "setpoint": row["setpoint"], "mode": row["mode"], "enabled": bool(row["enabled"]), "anticipate": bool(row["anticipate"])}

    def _validate_plage(self, name: str, days: list[int], start: str, end: str, setpoint: float | None, mode: str | None,
                        anticipate: bool = False) -> None:
        if not days or any(d not in range(7) for d in days):
            raise ValueError("jours : liste de 0 (lundi) à 6 (dimanche)")
        parse_hhmm(start); parse_hhmm(end)
        if start == end:
            raise ValueError("l'allumage et l'extinction ne peuvent pas être à la même heure")
        if setpoint is not None and not (5 <= setpoint <= 40 and (setpoint * 2) % 1 == 0):
            raise ValueError("consigne : entre 5 et 40 °C, par pas de 0,5")
        if mode is not None and mode not in ("manual", "auto", "overnight", "comfort"):
            raise ValueError("mode inconnu")
        if not name.strip():
            raise ValueError("nom vide")
        if anticipate and setpoint is None:
            raise ValueError("allumage anticipé : indique la température voulue (consigne)")

    def add_plage(self, name: str, days: list[int], start: str, end: str, setpoint: float | None = None,
                  mode: str | None = None, enabled: bool = True, anticipate: bool = False) -> dict:
        self._validate_plage(name, days, start, end, setpoint, mode, anticipate)
        cur = self._db.execute("INSERT INTO plages (name, days, start, end, setpoint, mode, enabled, anticipate) VALUES (?,?,?,?,?,?,?,?)",
                               (name.strip(), json.dumps(sorted(set(days))), start, end, setpoint, mode, int(enabled), int(anticipate)))
        self._db.commit()
        return self.get_plage(cur.lastrowid)

    def get_plage(self, plage_id: int) -> dict:
        row = self._db.execute("SELECT * FROM plages WHERE id = ?", (plage_id,)).fetchone()
        if row is None:
            raise KeyError(plage_id)
        return self._plage(row)

    def update_plage(self, plage_id: int, name: str, days: list[int], start: str, end: str, setpoint: float | None,
                     mode: str | None, enabled: bool, anticipate: bool = False) -> dict:
        self.get_plage(plage_id)
        self._validate_plage(name, days, start, end, setpoint, mode, anticipate)
        self._db.execute("UPDATE plages SET name=?, days=?, start=?, end=?, setpoint=?, mode=?, enabled=?, anticipate=? WHERE id=?",
                         (name.strip(), json.dumps(sorted(set(days))), start, end, setpoint, mode, int(enabled), int(anticipate), plage_id))
        self._db.commit()
        return self.get_plage(plage_id)

    def delete_plage(self, plage_id: int) -> None:
        self.get_plage(plage_id)
        self._db.execute("DELETE FROM plages WHERE id = ?", (plage_id,))
        self._db.commit()

    # ---- Minuteries ponctuelles ----------------------------------------------

    def add_timer(self, stove_id: str, action: str, now: datetime, at_time: str | None = None, at_ts: float | None = None) -> dict:
        """« Éteindre à 22:30 » : `at_time` désigne la prochaine occurrence de cette heure (aujourd'hui, sinon demain)."""
        if action not in ("on", "off"):
            raise ValueError("action : on ou off")
        if at_time is not None:
            t = parse_hhmm(at_time)
            target = datetime.combine(now.date(), t, tzinfo=self.tz)
            if target <= now:
                target += timedelta(days=1)
            at_ts = target.timestamp()
        if at_ts is None or at_ts <= now.timestamp():
            raise ValueError("heure absente ou déjà passée")
        # Une seule minuterie en attente par poêle et par action : la nouvelle remplace l'ancienne.
        self._db.execute("UPDATE timers SET status='cancelled' WHERE stove_id=? AND action=? AND status='pending'", (stove_id, action))
        cur = self._db.execute("INSERT INTO timers (stove_id, action, at_ts) VALUES (?,?,?)", (stove_id, action, at_ts))
        self._db.commit()
        return self._timer(self._db.execute("SELECT * FROM timers WHERE id=?", (cur.lastrowid,)).fetchone())

    @staticmethod
    def _timer(row: sqlite3.Row) -> dict:
        return {"id": row["id"], "stove_id": row["stove_id"], "action": row["action"], "at_ts": row["at_ts"], "status": row["status"]}

    def pending_timers(self, stove_id: str | None = None) -> list[dict]:
        rows = self._db.execute("SELECT * FROM timers WHERE status='pending' ORDER BY at_ts").fetchall()
        return [self._timer(r) for r in rows if stove_id is None or r["stove_id"] == stove_id]

    def cancel_timer(self, timer_id: int) -> None:
        cur = self._db.execute("UPDATE timers SET status='cancelled' WHERE id=? AND status='pending'", (timer_id,))
        self._db.commit()
        if cur.rowcount == 0:
            raise KeyError(timer_id)

    def finish_timer(self, timer_id: int, status: str) -> None:
        self._db.execute("UPDATE timers SET status=? WHERE id=?", (status, timer_id))
        self._db.commit()

    # ---- Exécutions déjà faites ----------------------------------------------

    def is_done(self, key: str) -> bool:
        return self._db.execute("SELECT 1 FROM runs WHERE key=?", (key,)).fetchone() is not None

    def mark_done(self, key: str, ts: float, outcome: str, label: str = "") -> None:
        self._db.execute("INSERT OR REPLACE INTO runs (key, ts, outcome, label) VALUES (?,?,?,?)", (key, ts, outcome, label))
        self._db.commit()

    def recent_runs(self, limit: int = 8) -> list[dict]:
        rows = self._db.execute("SELECT ts, outcome, label FROM runs WHERE label != '' ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()
        return [{"ts": r["ts"], "outcome": r["outcome"], "label": r["label"]} for r in rows]

    # ---- Jours sautés et absences --------------------------------------------------

    def skip_dates(self, today: Date) -> list[str]:
        """Jours (AAAA-MM-JJ) où les allumages des plages sont sautés ; les jours passés sont oubliés."""
        return sorted(d for d in (self.get_setting("skips") or {}).get("dates", []) if d >= today.isoformat())

    def set_skip(self, day: str, skip: bool, today: Date) -> list[str]:
        try:
            parsed = Date.fromisoformat(day)
        except ValueError:
            raise ValueError("date attendue au format AAAA-MM-JJ")
        if parsed < today:
            raise ValueError("ce jour est déjà passé")
        dates = set(self.skip_dates(today))
        if skip:
            dates.add(parsed.isoformat())
        else:
            dates.discard(parsed.isoformat())
        self.set_setting("skips", {"dates": sorted(dates)})
        return sorted(dates)

    def absence(self, now: datetime) -> dict | None:
        """Pause ou vacances en cours ou à venir ; oubliée une fois terminée."""
        a = self.get_setting("absence")
        if a and now.timestamp() > a["end_ts"] + GRACE_PLAGE.total_seconds():
            self.clear_absence()
            return None
        return a or None

    def set_absence(self, kind: str, now: datetime, end_ts: float, start_ts: float | None = None, preheat_minutes: int = 120,
                    setpoint: float = 21.0, off_at_start: bool = True) -> dict:
        """Pause : aucun allumage de plage jusqu'à `end_ts`. Vacances : extinction au départ, aucun allumage, puis
        préchauffage `preheat_minutes` avant le retour (`end_ts`) pour trouver le logement chaud. Une seule absence à la fois."""
        if kind not in ("pause", "vacances"):
            raise ValueError("type : pause ou vacances")
        now_ts = now.timestamp()
        start_ts = now_ts if kind == "pause" or start_ts is None else max(start_ts, now_ts)
        if end_ts <= now_ts:
            raise ValueError("la fin doit être dans le futur")
        if end_ts <= start_ts:
            raise ValueError("le retour doit être après le départ")
        if end_ts - start_ts > 120 * 86400:
            raise ValueError("120 jours au plus")
        if kind == "vacances":
            if not 0 <= preheat_minutes <= 720:
                raise ValueError("préchauffage : entre 0 et 12 h avant le retour")
            if not (5 <= setpoint <= 40 and (setpoint * 2) % 1 == 0):
                raise ValueError("consigne : entre 5 et 40 °C, par pas de 0,5")
        absence = {"id": uuid.uuid4().hex[:8], "kind": kind, "start_ts": start_ts, "end_ts": end_ts,
                   "preheat_minutes": int(preheat_minutes) if kind == "vacances" else 0,
                   "setpoint": float(setpoint) if kind == "vacances" else None,
                   "off_at_start": bool(off_at_start) if kind == "vacances" else False}
        self.set_setting("absence", absence)
        return absence

    def clear_absence(self) -> None:
        self._db.execute("DELETE FROM settings WHERE key='absence'")
        self._db.commit()

    def _on_skip_reason(self, target: datetime, now: datetime, absence: dict | None) -> str | None:
        """Pourquoi l'allumage prévu à `target` est sauté (ou None)."""
        if target.date().isoformat() in self.skip_dates(now.date() - timedelta(days=1)):
            return "pas d'allumage ce jour-là"
        if absence and absence["start_ts"] <= target.timestamp() < absence["end_ts"]:
            return "pause" if absence["kind"] == "pause" else "vacances"
        return None

    @staticmethod
    def _in_preheat(absence: dict | None, ts: float) -> bool:
        """Extinction de plage pendant le préchauffage du retour de vacances : on ne coupe pas."""
        return bool(absence and absence["kind"] == "vacances" and absence["preheat_minutes"]
                    and absence["end_ts"] - absence["preheat_minutes"] * 60 <= ts < absence["end_ts"])

    def _occurrences(self, now: datetime, offsets):
        """Chaque plage active, jour par jour : (plage, allumage, extinction)."""
        for p in self.list_plages():
            if not p["enabled"]:
                continue
            start_t, end_t = parse_hhmm(p["start"]), parse_hhmm(p["end"])
            for offset in offsets:
                day = now.date() + timedelta(days=offset)
                if day.weekday() not in p["days"]:
                    continue
                start_dt = datetime.combine(day, start_t, tzinfo=self.tz)
                end_dt = datetime.combine(day, end_t, tzinfo=self.tz)
                if end_dt <= start_dt:
                    end_dt += timedelta(days=1)
                yield p, start_dt, end_dt

    # ---- Ce qui va se passer ---------------------------------------------------------

    def upcoming(self, now: datetime, limit: int = 6) -> list[dict]:
        """Prochains événements : plages actives des 8 prochains jours, minuteries, vacances, du plus proche au plus lointain."""
        now = now.astimezone(self.tz)
        absence = self.absence(now)
        events: list[dict] = []
        for p, start_dt, end_dt in self._occurrences(now, range(-1, 8)):
            if start_dt > now and not self.is_done(f"plage:{p['id']}:on:{start_dt.isoformat()}"):
                reason = self._on_skip_reason(start_dt, now, absence)
                detail = f" ({p['setpoint']:g} °C)" if p["setpoint"] is not None else ""
                if reason == "pas d'allumage ce jour-là":
                    events.append({"ts": start_dt.timestamp(), "kind": "skip", "label": "Allumage sauté (jour sauté)"})
                elif reason is None and p["anticipate"]:
                    fire = max(now, start_dt - timedelta(minutes=self.lead_for(p)))
                    events.append({"ts": fire.timestamp(), "kind": "on", "setpoint": p["setpoint"],
                                   "label": f"Allumage anticipé : {p['setpoint']:g} °C à {start_dt:%H:%M}", "target_ts": start_dt.timestamp()})
                elif reason is None:
                    events.append({"ts": start_dt.timestamp(), "kind": "on", "label": f"Allumage{detail}", "setpoint": p["setpoint"]})
            if end_dt > now and not self._in_preheat(absence, end_dt.timestamp()):
                events.append({"ts": end_dt.timestamp(), "kind": "off", "label": "Extinction"})
        for t in self.pending_timers():
            events.append({"ts": t["at_ts"], "kind": t["action"], "label": ("Allumage" if t["action"] == "on" else "Extinction") + " (minuterie)"})
        if absence and absence["kind"] == "vacances":
            if absence["off_at_start"] and absence["start_ts"] > now.timestamp():
                events.append({"ts": absence["start_ts"], "kind": "off", "label": "Départ en vacances : extinction"})
            preheat = absence["end_ts"] - absence["preheat_minutes"] * 60
            if absence["preheat_minutes"] and preheat > now.timestamp():
                events.append({"ts": preheat, "kind": "on", "label": f"Retour de vacances : préchauffage ({absence['setpoint']:g} °C)",
                               "setpoint": absence["setpoint"]})
        return sorted(events, key=lambda e: e["ts"])[:limit]

    def plage_active(self, now: datetime) -> bool:
        """Une plage chauffe en ce moment (allumage anticipé compris, jours sautés et absences exclus) : le planning a la main."""
        now = now.astimezone(self.tz)
        absence = self.absence(now)
        for p, start_dt, end_dt in self._occurrences(now, (-1, 0, 1)):
            fire = start_dt - timedelta(minutes=max(0, self.lead_for(p))) if p["anticipate"] else start_dt
            if fire <= now < end_dt and self._on_skip_reason(start_dt, now, absence) is None:
                return True
        return False

    # ---- Qu'y a-t-il à faire maintenant ? ----------------------------------------

    def due(self, now: datetime) -> list[Action]:
        now = now.astimezone(self.tz)
        absence = self.absence(now)
        actions: list[Action] = []
        for p, start_dt, end_dt in self._occurrences(now, (-1, 0, 1)):   # hier : plage qui passe minuit ; demain : allumage anticipé
            where = f"plage « {p['name']} »"
            key = f"plage:{p['id']}:on:{start_dt.isoformat()}"
            if now - start_dt <= GRACE_PLAGE and not self.is_done(key):
                fire = start_dt
                if p["anticipate"] and now >= start_dt - MAX_LEAD:
                    fire = start_dt - timedelta(minutes=max(0, self.lead_for(p)))
                if now >= fire:
                    reason = self._on_skip_reason(start_dt, now, absence)
                    if reason:
                        actions.append(Action(key, f"{p['name']} : allumage sauté", (), detail=where, skip_reason=reason))
                    else:
                        steps: list[tuple[str, object]] = [("power", True)]
                        if p["mode"]:
                            steps.append(("mode", p["mode"]))
                        if p["setpoint"] is not None:
                            steps.append(("setpoint", float(p["setpoint"])))
                        label = f"{p['name']} : allumage" + (f" anticipé ({p['setpoint']:g} °C à {start_dt:%H:%M})" if p["anticipate"] else "")
                        actions.append(Action(key, label, tuple(steps), detail=where))
            key = f"plage:{p['id']}:off:{end_dt.isoformat()}"
            if now >= end_dt and now - end_dt <= GRACE_PLAGE and not self.is_done(key):
                if self._in_preheat(absence, end_dt.timestamp()):
                    actions.append(Action(key, f"{p['name']} : extinction sautée", (), detail=where, skip_reason="préchauffage du retour de vacances"))
                else:
                    actions.append(Action(key, f"{p['name']} : extinction", (("power", False),), detail=where))
        for t in self.pending_timers():
            when = datetime.fromtimestamp(t["at_ts"], self.tz)
            if now >= when:
                if now - when > GRACE_TIMER:
                    self.finish_timer(t["id"], "missed")  # le serveur était arrêté trop longtemps
                    continue
                actions.append(Action(f"timer:{t['id']}", f"Minuterie : {'allumage' if t['action'] == 'on' else 'extinction'}",
                                      (("power", t["action"] == "on"),), stove_id=t["stove_id"], timer_id=t["id"], detail="minuterie"))
        if absence and absence["kind"] == "vacances":
            start = datetime.fromtimestamp(absence["start_ts"], self.tz)
            end = datetime.fromtimestamp(absence["end_ts"], self.tz)
            key = f"absence:{absence['id']}:off"
            if absence["off_at_start"] and now >= start and now - start <= GRACE_TIMER and not self.is_done(key):
                actions.append(Action(key, "Vacances : extinction au départ", (("power", False),), origin="vacances", detail="départ en vacances"))
            key = f"absence:{absence['id']}:preheat"
            if absence["preheat_minutes"] and end - timedelta(minutes=absence["preheat_minutes"]) <= now < end + GRACE_PLAGE and not self.is_done(key):
                actions.append(Action(key, "Vacances : préchauffage avant le retour", (("power", True), ("setpoint", float(absence["setpoint"]))),
                                      origin="vacances", detail="retour de vacances"))
        return sorted(actions, key=lambda a: a.key)

    # ---- Remplissages de pellets -------------------------------------------------

    def add_refill(self, stove_id: str, ts: float, kg: float | None = None, full: bool = False) -> None:
        if not full and (kg is None or not 0 < kg <= 40):
            raise ValueError("kg : entre 0 et 40, ou full=true")
        self._db.execute("INSERT INTO refills (stove_id, ts, kg, full) VALUES (?,?,?,?)", (stove_id, ts, kg, int(full)))
        self._db.commit()

    def undo_last_refill(self, stove_id: str) -> bool:
        """Annule le dernier remplissage saisi (appui par erreur)."""
        row = self._db.execute("SELECT id FROM refills WHERE stove_id=? ORDER BY ts DESC, id DESC LIMIT 1", (stove_id,)).fetchone()
        if row is None:
            return False
        self._db.execute("DELETE FROM refills WHERE id=?", (row["id"],))
        self._db.commit()
        return True

    def refills(self, stove_id: str) -> list[dict]:
        rows = self._db.execute("SELECT ts, kg, full FROM refills WHERE stove_id=? ORDER BY ts", (stove_id,)).fetchall()
        return [{"ts": r["ts"], "kg": r["kg"], "full": bool(r["full"])} for r in rows]

    # ---- Réglages divers (position météo) ------------------------------------------

    def get_setting(self, key: str) -> dict | None:
        row = self._db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return json.loads(row["value"]) if row else None

    def set_setting(self, key: str, value: dict) -> None:
        self._db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?,?)", (key, json.dumps(value)))
        self._db.commit()

    # ---- Sessions de la web app (seule l'empreinte du jeton est stockée) -------------------

    def add_session(self, digest: str, expires: float) -> None:
        self._db.execute("DELETE FROM sessions WHERE expires < ?", (clock.time(),))
        self._db.execute("INSERT INTO sessions (digest, expires) VALUES (?,?)", (digest, expires))
        self._db.commit()

    def session_valid(self, digest: str) -> bool:
        row = self._db.execute("SELECT expires FROM sessions WHERE digest=?", (digest,)).fetchone()
        return row is not None and row["expires"] > clock.time()

    def drop_session(self, digest: str) -> None:
        self._db.execute("DELETE FROM sessions WHERE digest=?", (digest,))
        self._db.commit()
