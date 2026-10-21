"""Relève régulière de l'état : cache en mémoire + historique."""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import replace
from typing import Callable

from . import commands
from .cloud import CloudError, MczCloud
from .history import History
from .journal import Journal, describe
from .snapshot import Snapshot, build_snapshot

log = logging.getLogger("mcz")
OWN_POWER_WINDOW = 300     # s : un changement marche/arrêt dans ce délai après notre appui vient de nous
OWN_SETPOINT_WINDOW = 180  # s : idem pour la consigne
EXTERNAL = "Maestro, bouton du poêle ou télécommande"
# Champs bruts de /Status qui décrivent la marche du poêle (phase, sous-phase, état, marche demandée, Start&Stop).
STATE_KEYS = ("fase_op", "sub_fase_op", "stato_stufa", "power_enabled", "att_eco", "mod_lav_att")


def raw_state(status: dict) -> str:
    """« état brut : fase_op=off, … » : noté au journal pour comprendre un changement que l'on n'a pas commandé."""
    return "état brut : " + ", ".join(f"{k}={status[k]}" for k in STATE_KEYS if k in status)


class StoveService:
    def __init__(self, cloud: MczCloud, history: History, poll_seconds: float,
                 confirm_wait: float = 1.5, confirm_checks: int = 8, max_attempts: int = 3,
                 ping_wait: float = 2.0, ping_min_interval: float = 15.0, toggle_checks: int = 40,
                 journal: Journal | None = None) -> None:
        self.journal = journal or Journal(":memory:")
        self.notify: Callable[..., None] = lambda kind, title, body="": None   # notifications (push.Notifier.emit)
        self.last_ok: dict[str, float] = {}         # dernier relevé exploitable de chaque poêle
        self._seen: dict[str, dict] = {}            # dernier état observé, pour repérer ce qui change sans nous
        self._own: dict[tuple[str, str], float] = {}  # (poêle, power|setpoint) → heure de notre dernier envoi
        self.toggle_checks = toggle_checks
        self.ping_wait, self.ping_min_interval = ping_wait, ping_min_interval
        self._last_ping: dict[str, float] = {}
        self.confirm_wait, self.confirm_checks, self.max_attempts = confirm_wait, confirm_checks, max_attempts
        self._locks: dict[str, asyncio.Lock] = {}
        self._form_index: dict[str, int] = {}
        self.jobs: dict[str, dict] = {}            # commandes lancées en arrière-plan (suivies par l'app)
        self._job_tasks: set[asyncio.Task] = set()
        self._failed_notified: dict[str, float] = {}   # échec déjà notifié, par commande automatique (le planning réessaie)
        self.cloud = cloud
        self.history = history
        self.poll_seconds = poll_seconds
        self.latest: dict[str, Snapshot] = {}
        self.last_error: str | None = None
        self._stoves: list[dict] = []
        self._task: asyncio.Task | None = None

    async def refresh_once(self) -> None:
        if not self._stoves:
            self._stoves = await self.cloud.list_stoves()
        for stove in self._stoves:
            stove_id = stove["Id"]
            status = await self._status_awake(stove_id)
            try:
                detail = await self.cloud.detail(stove_id)
            except CloudError:
                detail = None  # l'état détaillé est facultatif
            snap = build_snapshot(stove_id, status, detail, time.time())
            self.latest[stove_id] = snap
            if not self._is_empty(status):
                self.history.record(snap)  # un relevé vide (module endormi) ne doit pas salir les courbes avec des zéros
                self.last_ok[stove_id] = snap.fetched_at
                self._observe(stove_id, status)
        self.last_error = None

    @staticmethod
    def _is_empty(status: dict) -> bool:
        """Le module Wi-Fi du poêle dort : le cloud renvoie un état sans données (pas de température) ou « non connecté »."""
        return not status.get("IsConnected", True) or "temp_amb_install" not in status

    async def _status_awake(self, stove_id: str) -> dict:
        status = await self.cloud.status(stove_id)
        if self._is_empty(status) and time.time() - self._last_ping.get(stove_id, 0) >= self.ping_min_interval:
            self._last_ping[stove_id] = time.time()
            try:
                await self.cloud.ping(stove_id)
                await asyncio.sleep(self.ping_wait)
                status = await self.cloud.status(stove_id)
            except CloudError as err:
                log.warning("ping impossible : %s", err)
        return status

    async def _store(self, stove_id: str, status: dict) -> Snapshot:
        snap = build_snapshot(stove_id, status, None, time.time())
        old = self.latest.get(stove_id)
        if old and snap.fume_temp is None:
            snap = replace(snap, fume_temp=old.fume_temp)  # /State n'est pas relu à chaque vérification
        self.latest[stove_id] = snap
        if not self._is_empty(status):
            self.history.record(snap)
            self.last_ok[stove_id] = snap.fetched_at
            self._observe(stove_id, status)
        return snap

    def _observe(self, stove_id: str, status: dict) -> None:
        """Note au journal ce qui change sans commande de notre part : allumage / extinction / consigne venus d'ailleurs, alarmes."""
        now = time.time()
        lit = commands.is_lit(status)
        setpoint = status.get("set_amb1")
        error = bool(status.get("IsInError", False))
        raw = tuple(status.get(k) for k in STATE_KEYS)
        prev = self._seen.get(stove_id)
        if not commands.phase_known(status):
            # Phase jamais vue : on ne sait pas si le poêle brûle. Notée une fois, sans conclure « allumé / éteint ailleurs ».
            if prev is not None and prev.get("raw", (None,))[0] != status.get("fase_op"):
                self.journal.add("poele", f"Phase inconnue « {status.get('fase_op')} »", "info", raw_state(status), stove_id)
            if prev is not None:
                lit = prev["lit"]
        self._seen[stove_id] = {"lit": lit, "setpoint": setpoint, "error": error, "raw": raw}
        if prev is None or raw != prev.get("raw"):
            log.info("poêle %s : %s, pièce %s °C", stove_id[-6:], raw_state(status), status.get("temp_amb_install"))
        if prev is None:
            return
        if lit != prev["lit"] and now - self._own.get((stove_id, "power"), 0) > OWN_POWER_WINDOW:
            self.journal.add("externe", "Allumage" if lit else "Extinction", "info",
                             f"{'en alarme' if error and not lit else EXTERNAL} · {raw_state(status)}", stove_id)
            if not error:
                self.notify("external", "Poêle allumé ailleurs" if lit else "Poêle éteint ailleurs", f"{EXTERNAL} : sans commande de l'app.")
        if (isinstance(setpoint, (int, float)) and isinstance(prev["setpoint"], (int, float)) and abs(setpoint - prev["setpoint"]) >= 0.25
                and now - self._own.get((stove_id, "setpoint"), 0) > OWN_SETPOINT_WINDOW):
            self.journal.add("externe", describe("setpoint", setpoint), "info", EXTERNAL, stove_id)
        if error != prev["error"]:
            self.journal.add("poele", "Alarme" if error else "Fin d'alarme", "failed" if error else "info",
                             "le poêle signale un défaut" if error else "", stove_id)
            if error:
                self.notify("alarm", "Poêle en alarme", "Le poêle signale un défaut : regarde son écran. Aucun allumage automatique tant qu'il est en alarme.")

    async def send_command(self, stove_id: str, kind: str, value, trace: list | None = None) -> dict:
        """Envoie une commande absolue, vérifie qu'elle apparaît dans l'état du poêle, la renvoie sinon (max `max_attempts`)."""
        spec = commands.build(kind, value)  # ValueError si inconnue ou hors limites
        stove = next((s for s in self._stoves if s["Id"] == stove_id), None)
        if stove is None:
            raise KeyError(stove_id)
        async with self._locks.setdefault(stove_id, asyncio.Lock()):  # une commande à la fois par poêle
            max_attempts, checks = self.max_attempts, self.confirm_checks
            if spec.toggle:
                # Bascule : ne rien envoyer si le poêle est déjà dans l'état voulu (ou y va), puis un seul envoi.
                status = await self._status_awake(stove_id)
                snap = await self._store(stove_id, status)
                if trace is not None:
                    trace.append({"avant": {"fase_op": status.get("fase_op"), "power_enabled": status.get("power_enabled")}})
                if self._is_empty(status):
                    # État illisible (module endormi, cloud sans données) : il passerait pour « éteint ». On n'appuie pas
                    # à l'aveugle sur une bascule et on ne conclut pas « déjà éteint » : erreur, le planning réessaiera.
                    raise CloudError("état du poêle illisible (module Wi-Fi endormi ?), rien envoyé")
                if not commands.phase_known(status):
                    raise CloudError(f"phase « {status.get('fase_op')} » inconnue : marche ou arrêt incertain, rien envoyé")
                if spec.confirmed_by(status):
                    log.info("commande %s=%s : déjà dans cet état, rien envoyé", kind, value)
                    return {"outcome": "confirmed", "attempts": 0, "snapshot": snap.to_json(), "raw": raw_state(status)}
                max_attempts, checks = 1, self.toggle_checks
            if kind in ("power", "setpoint"):
                self._own[(stove_id, kind)] = time.time()
            forms = [(spec.config_id, spec.sensor_id, spec.wire_value), *spec.fallbacks]
            start = self._form_index.get(kind, 0)          # on repart de la forme qui a marché la dernière fois
            forms = forms[start:] + forms[:start]
            for attempt in range(1, max_attempts + 1):
                for config_id, sensor_id, wire in forms:
                    try:
                        await self.cloud.activate(stove, config_id, sensor_id, wire)
                        if trace is not None:
                            trace.append({"essai": attempt, "forme": config_id[:8], "valeur": wire, "cloud": "accepté"})
                        if (config_id, sensor_id, wire) != (spec.config_id, spec.sensor_id, spec.wire_value):
                            log.info("commande %s : forme de secours acceptée (%s, %r)", kind, config_id[:8], wire)
                        self._form_index[kind] = [(spec.config_id, spec.sensor_id, spec.wire_value), *spec.fallbacks].index((config_id, sensor_id, wire))
                        break
                    except CloudError as err:
                        log.warning("commande %s refusée (%s, %r) : %s", kind, config_id[:8], wire, err)
                        if trace is not None:
                            trace.append({"essai": attempt, "forme": config_id[:8], "valeur": wire, "cloud": f"refusé : {err}"})
                        if "HTTP 400" not in str(err) or (config_id, sensor_id, wire) == forms[-1]:
                            raise
                for _ in range(checks):
                    await asyncio.sleep(self.confirm_wait)
                    status = await self._status_awake(stove_id)
                    snap = await self._store(stove_id, status)
                    log.info("commande %s=%s essai %d : fase_op=%s power_enabled=%s set_amb1=%s mode=%s", kind, value, attempt,
                             status.get("fase_op"), status.get("power_enabled"), status.get("set_amb1"), status.get("mod_lav_att"))
                    if trace is not None:
                        trace.append({"vu": {"fase_op": status.get("fase_op"), "power_enabled": status.get("power_enabled"),
                                             "IsConnected": status.get("IsConnected")}})
                    if not self._is_empty(status) and spec.confirmed_by(status):   # un état vide ne confirme rien
                        return {"outcome": "confirmed", "attempts": attempt, "snapshot": snap.to_json()}
            return {"outcome": "not_confirmed", "attempts": max_attempts, "snapshot": self.latest[stove_id].to_json()}

    def last_own_power(self, stove_id: str) -> float:
        """Heure de notre dernier appui sur marche/arrêt (app, planning, garde), 0 si aucun."""
        return self._own.get((stove_id, "power"), 0)

    def track(self, kind: str, value, source: str, origin: str = "app", detail: str = "") -> dict:
        """Fiche de suivi d'une commande (app ou planning), le temps que l'app suive sa confirmation ; le journal garde la trace."""
        now = time.time()
        for old in [k for k, j in self.jobs.items() if now - j["started"] > 7 * 86400]:
            del self.jobs[old]
        job = {"id": uuid.uuid4().hex[:12], "kind": kind, "value": value, "status": "pending", "attempts": 0,
               "error": None, "snapshot": None, "started": now, "trace": [], "source": source, "origin": origin, "detail": detail}
        self.jobs[job["id"]] = job
        return job

    def finish_job(self, job: dict, stove_id: str, result: dict | None = None, error: str | None = None) -> None:
        """Clôt la fiche et l'inscrit au journal : confirmé, déjà fait (rien envoyé), non confirmé ou erreur."""
        if error is not None:
            job.update(status="error", error=error)
            outcome, note = "failed", f"erreur : {error}"
        else:
            job.update(status=result["outcome"], attempts=result["attempts"], snapshot=result["snapshot"])
            if result["outcome"] != "confirmed":
                outcome, note = "failed", "le poêle n'a pas confirmé"
            elif result["attempts"] == 0:
                outcome, note = "noop", " · ".join(x for x in ("déjà dans cet état, rien envoyé", result.get("raw")) if x)
            else:
                outcome, note = "ok", ""
        detail = " · ".join(x for x in (job.get("detail"), note) if x)
        self.journal.add(job.get("origin", "app"), describe(job["kind"], job["value"]), outcome, detail, stove_id)
        if outcome == "failed" and job["kind"] == "power" and job.get("origin", "app") != "app":   # depuis l'app, on le voit à l'écran
            key = f"{job['source']}:{job['value']}"
            if time.time() - self._failed_notified.get(key, 0) > 3600:   # le planning réessaie toutes les 15 s : une notification suffit
                self._failed_notified[key] = time.time()
                what = "Allumage" if job["value"] else "Extinction"
                self.notify("failed", f"{what} automatique non confirmé", detail[:1].upper() + detail[1:] if detail else note)

    def start_command(self, stove_id: str, kind: str, value) -> dict:
        """Lance la commande en arrière-plan et rend aussitôt un suivi : aucune requête HTTP ne reste ouverte pendant la confirmation."""
        commands.build(kind, value)                      # ValueError tout de suite si invalide
        if not any(s["Id"] == stove_id for s in self._stoves):
            raise KeyError(stove_id)
        job = self.track(kind, value, "app")

        async def run() -> None:
            try:
                result = await self.send_command(stove_id, kind, value, job["trace"])
                self.finish_job(job, stove_id, result)
            except Exception as err:
                self.finish_job(job, stove_id, error=str(err))
                log.warning("commande %s=%s en erreur : %s", kind, value, err)

        task = asyncio.create_task(run())
        self._job_tasks.add(task)
        task.add_done_callback(self._job_tasks.discard)
        return job

    async def _loop(self) -> None:
        while True:
            try:
                await self.refresh_once()
            except Exception as err:  # le cloud peut être indisponible : on garde le cache et on réessaie
                self.last_error = str(err)
                log.warning("relevé impossible : %s", err)
            await asyncio.sleep(self.poll_seconds)

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
        await self.cloud.aclose()
