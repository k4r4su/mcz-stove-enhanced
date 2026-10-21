"""Exécute ce que `Planner.due` annonce : commandes absolues, confirmées par l'état du poêle, rejouées jusqu'à la fin du délai de grâce."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from .planner import Action, Planner
from .service import StoveService

log = logging.getLogger("mcz.scheduler")
# Après une extinction prévue trouvée « déjà éteint », on surveille ce délai : le 15/10, le poêle s'était arrêté seul à
# 10:46 (pièce à 24 °C pour 21 °C), l'extinction de 11:02 n'a donc rien envoyé, et il s'est rallumé seul à 11:16.
REARM_SECONDS = 3600


class Scheduler:
    def __init__(self, service: StoveService, planner: Planner, tick_seconds: float = 15.0) -> None:
        self.service, self.planner, self.tick_seconds = service, planner, tick_seconds
        self._task: asyncio.Task | None = None
        self._rearm: dict[str, dict] = {}   # poêle -> {since, until, action} : extinction à rejouer s'il se rallume seul

    async def tick(self, now: datetime | None = None) -> None:
        now = now or datetime.now(self.planner.tz)
        for action in self.planner.due(now):
            await self._run(action, now)
        await self._check_rearm(now)

    async def _check_rearm(self, now: datetime) -> None:
        """Le poêle se rallume de lui-même peu après une extinction prévue qui l'avait trouvé éteint : on l'éteint."""
        for stove_id, w in list(self._rearm.items()):
            snap = self.service.latest.get(stove_id)
            if now.timestamp() > w["until"] or self.service.last_own_power(stove_id) > w["since"] or self.planner.plage_active(now):
                del self._rearm[stove_id]          # délai passé, allumé depuis l'app ou la garde, ou une plage a repris la main
                continue
            if snap is None or not snap.running or snap.in_error:
                continue
            del self._rearm[stove_id]              # une seule tentative
            a = w["action"]
            detail = f"{a.detail} · le poêle s'est rallumé seul après la fin prévue"
            job = self.service.track("power", False, f"planning : {a.label}", a.origin, detail)
            try:
                self.service.finish_job(job, stove_id, await self.service.send_command(stove_id, "power", False, job["trace"]))
            except Exception as err:
                self.service.finish_job(job, stove_id, error=str(err))
                log.warning("%s (rallumage seul) : %s", a.label, err)

    async def _run(self, action: Action, now: datetime) -> None:
        if action.skip_reason:                             # sauté : rien à envoyer, on le note une fois
            self.service.journal.add(action.origin, action.label.split(" : ")[-1].capitalize(), "skipped",
                                     f"{action.detail} · {action.skip_reason}")
            self.planner.mark_done(action.key, now.timestamp(), f"sauté : {action.skip_reason}", action.label)
            return
        stove_ids = [action.stove_id] if action.stove_id else list(self.service.latest)
        all_ok = bool(stove_ids)
        outcome = "ok"
        for stove_id in stove_ids:
            snap = self.service.latest.get(stove_id)
            if snap is None:
                all_ok = False
                continue
            for kind, value in action.steps:
                if kind == "power" and value is True and snap.in_error:
                    outcome = "ignoré : poêle en alarme"   # on n'allume pas un poêle en défaut
                    self.service.journal.add(action.origin, "Allumage", "skipped", f"{action.detail} · poêle en alarme", stove_id)
                    break
                # La marche/arrêt vérifie elle-même l'état réel avant d'appuyer (bouton à bascule) : pas de raccourci ici.
                job = self.service.track(kind, value, f"planning : {action.label}", action.origin, action.detail)
                try:
                    result = await self.service.send_command(stove_id, kind, value, job["trace"])
                    self.service.finish_job(job, stove_id, result)
                except Exception as err:                   # cloud indisponible : on réessaiera au prochain tick
                    self.service.finish_job(job, stove_id, error=str(err))
                    log.warning("%s : %s", action.label, err)
                    all_ok = False
                    break
                if result["outcome"] != "confirmed":
                    all_ok = False
                    break
                if kind == "power" and value is False and result["attempts"] == 0:   # déjà éteint : il pourrait se rallumer seul
                    self._rearm[stove_id] = {"since": now.timestamp(), "until": now.timestamp() + REARM_SECONDS, "action": action}
                snap = self.service.latest.get(stove_id, snap)
        if all_ok:
            self.planner.mark_done(action.key, now.timestamp(), outcome, action.label)
            if action.timer_id is not None:
                self.planner.finish_timer(action.timer_id, "done")
            log.info("%s : fait (%s)", action.label, outcome)

    async def _loop(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception as err:
                log.warning("planificateur : %s", err)
            await asyncio.sleep(self.tick_seconds)

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop())

    def stop(self) -> None:
        if self._task:
            self._task.cancel()
