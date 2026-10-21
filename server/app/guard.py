"""Garde de température et surveillance, vérifiées chaque minute.

Garde : pendant les heures choisies, si la Prévision 12 h (projection.py) annonce que la pièce va passer sous le seuil
et qu'aucun allumage prévu n'arrive à temps, le poêle est rallumé à l'heure conseillée. Il s'éteint ensuite une fois la
consigne atteinte (au moins 1 h de marche) ou à la fin des heures de garde, au choix. Une plage qui commence ou un allumage
prévu dans l'heure reprend la main : la garde n'éteint jamais ce que le planning chauffe. Rien n'est fait en pause, en
vacances, un jour sauté, poêle en alarme ou injoignable.

Surveillance (notifications, voir push.py) : pièce sous le seuil poêle éteint, poêle injoignable, réservoir bas,
fumées nettement plus chaudes qu'au début du mois.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Awaitable, Callable

from .planner import Planner, parse_hhmm
from .service import StoveService

log = logging.getLogger("mcz.guard")

DEFAULTS = {"enabled": False, "threshold": 18.0, "start": "07:00", "end": "22:00", "after": "setpoint"}
MIN_RUN = 3600             # s : rallumé, le poêle chauffe au moins 1 h (et la garde ne rallume pas à moins d'1 h de sa fin)
COOLDOWN = 30 * 60         # s : pas de rallumage dans la demi-heure qui suit une extinction de la garde
RETRY = 15 * 60            # s : après un échec, on réessaie un quart d'heure plus tard
HANDOVER = 3600            # s : allumage prévu dans l'heure → on laisse allumé, le planning prend le relais
REACHED = 0.3              # °C sous la consigne : atteinte
OFFLINE_AFTER = 30 * 60    # s sans relevé exploitable → poêle injoignable
FUMES_ALERT = 15           # °C de plus à puissance égale (même seuil que la carte Fumées)
FUMES_EVERY = 7 * 86400    # s : une alerte fumées par semaine au plus


def config(raw: dict | None) -> dict:
    cfg = {**DEFAULTS, **{k: v for k, v in (raw or {}).items() if k in DEFAULTS}}
    cfg["threshold"] = float(cfg["threshold"])
    return cfg


def validate(cfg: dict) -> dict:
    cfg = config(cfg)
    if not 10 <= cfg["threshold"] <= 25 or (cfg["threshold"] * 2) % 1:
        raise ValueError("seuil : entre 10 et 25 °C, par pas de 0,5")
    parse_hhmm(cfg["start"]), parse_hhmm(cfg["end"])
    if cfg["start"] == cfg["end"]:
        raise ValueError("le début et la fin de la garde ne peuvent pas être à la même heure")
    if cfg["after"] not in ("setpoint", "window"):
        raise ValueError("après le rallumage : setpoint ou window")
    cfg["enabled"] = bool(cfg["enabled"])
    return cfg


def window_end(local: datetime, start: str, end: str) -> datetime | None:
    """Fin des heures de garde en cours (ou None hors de ces heures). Une garde 22:00 → 06:00 passe minuit."""
    s, e = parse_hhmm(start), parse_hhmm(end)
    for offset in (-1, 0):
        day = local.date() + timedelta(days=offset)
        a, b = datetime.combine(day, s, tzinfo=local.tzinfo), datetime.combine(day, e, tzinfo=local.tzinfo)
        if b <= a:
            b += timedelta(days=1)
        if a <= local < b:
            return b
    return None


def fmt(v: float) -> str:
    return f"{v:.1f}".replace(".", ",")


@dataclass
class Situation:
    local: datetime                 # maintenant, heure locale
    lit: bool
    ambient: float | None
    setpoint: float | None
    blocked: str | None             # alarme, injoignable, pause, vacances, jour sauté : pourquoi la garde ne rallume pas
    handover: bool                  # une plage chauffe ou un allumage est prévu dans l'heure
    session: dict | None            # rallumage en cours de la garde : {"lit_ts": …}
    advice: dict | None             # projection.advise(…) au seuil de la garde
    last_off: float | None = None   # dernière extinction par la garde
    last_try: float | None = None   # dernier envoi raté


def decide(cfg: dict, sit: Situation) -> tuple[str, str] | None:
    """("on" | "off" | "end", raison) ou None. `end` : la garde rend la main sans rien envoyer."""
    now = sit.local.timestamp()
    end = window_end(sit.local, cfg["start"], cfg["end"])
    if sit.last_try and now - sit.last_try < RETRY:
        return None
    if sit.session:
        if not sit.lit:
            return "end", "poêle éteint par ailleurs"
        if sit.handover:
            return "end", "le planning prend le relais"
        if end is None:
            return "off", f"fin de la garde ({cfg['end']})"
        reached = sit.ambient is not None and sit.setpoint is not None and sit.ambient >= sit.setpoint - REACHED
        if cfg["after"] == "setpoint" and reached and now - sit.session["lit_ts"] >= MIN_RUN:
            return "off", f"consigne de {fmt(sit.setpoint)} °C atteinte"
        return None
    if not cfg["enabled"] or sit.lit or sit.blocked or end is None or end.timestamp() - now < MIN_RUN:
        return None
    if sit.last_off and now - sit.last_off < COOLDOWN:
        return None
    a = sit.advice
    if not a or a.get("relight_ts") is None or a.get("on_time") or a["relight_ts"] > now + 60 or a["cross_ts"] >= end.timestamp():
        return None
    thr = fmt(cfg["threshold"])
    if sit.ambient is not None and (a["cross_ts"] <= now + 300 or sit.ambient < cfg["threshold"]):
        return "on", f"la pièce est à {fmt(sit.ambient)} °C, seuil {thr} °C"
    cross = datetime.fromtimestamp(a["cross_ts"], sit.local.tzinfo)
    return "on", f"la pièce passerait sous {thr} °C vers {cross:%H:%M}"


class Guard:
    def __init__(self, service: StoveService, planner: Planner, notify: Callable[..., None],
                 advise: Callable[[str, float], Awaitable[dict]], insights: Callable[[str], Awaitable[dict]],
                 tick_seconds: float = 60.0) -> None:
        self.service, self.planner, self.notify = service, planner, notify
        self.advise, self.insights, self.tick_seconds = advise, insights, tick_seconds
        self.started = time.time()
        self._task: asyncio.Task | None = None

    def state(self) -> dict:
        s = self.planner.get_setting("guard_state") or {}
        for k in ("sessions", "last_off", "last_try", "cold", "offline", "pellet"):
            s.setdefault(k, {})
        return s

    def blocked(self, stove_id: str, snap, local: datetime) -> str | None:
        now = local.timestamp()
        if snap.in_error:
            return "poêle en alarme"
        if not snap.connected or now - self.service.last_ok.get(stove_id, 0) > 600:
            return "poêle injoignable"
        absence = self.planner.absence(local)
        if absence and absence["start_ts"] <= now < absence["end_ts"]:
            return absence["kind"]
        if local.date().isoformat() in self.planner.skip_dates(local.date()):
            return "pas d'allumage aujourd'hui"
        return None

    def handover(self, local: datetime) -> bool:
        if self.planner.plage_active(local):
            return True
        return any(e["kind"] == "on" and e["ts"] - local.timestamp() <= HANDOVER for e in self.planner.upcoming(local, limit=10))

    async def tick(self, now: datetime | None = None) -> None:
        local = (now or datetime.now(self.planner.tz)).astimezone(self.planner.tz)
        cfg = config(self.planner.get_setting("guard"))
        st = self.state()
        for stove_id, snap in list(self.service.latest.items()):
            try:
                await self._guard(stove_id, snap, local, cfg, st)
            except Exception as err:
                log.warning("garde : %s", err)
            self._watch(stove_id, snap, local, cfg, st)
        await self._fumes(local, st)
        self.planner.set_setting("guard_state", st)

    async def _guard(self, stove_id: str, snap, local: datetime, cfg: dict, st: dict) -> None:
        session = st["sessions"].get(stove_id)
        if not session and not cfg["enabled"]:
            return
        blocked = self.blocked(stove_id, snap, local)
        advice = None
        if (not session and not snap.running and not blocked and snap.ambient and snap.ambient > 0
                and window_end(local, cfg["start"], cfg["end"]) is not None):
            advice = await self.advise(stove_id, cfg["threshold"])
        sit = Situation(local, snap.running, snap.ambient, snap.setpoint, blocked, self.handover(local), session, advice,
                        st["last_off"].get(stove_id), st["last_try"].get(stove_id))
        verdict = decide(cfg, sit)
        if verdict is None:
            return
        what, reason = verdict
        if what == "end":
            st["sessions"].pop(stove_id, None)
            return
        if await self._power(stove_id, what == "on", reason):
            st["last_try"].pop(stove_id, None)
            if what == "on":
                st["sessions"][stove_id] = {"lit_ts": local.timestamp(), "reason": reason}
                self.notify("guard", "Garde : poêle rallumé", reason[:1].upper() + reason[1:] + ".")
            else:
                st["sessions"].pop(stove_id, None)
                st["last_off"][stove_id] = local.timestamp()
                self.notify("guard", "Garde : poêle éteint", reason[:1].upper() + reason[1:] + ".")
        else:
            st["last_try"][stove_id] = local.timestamp()

    async def _power(self, stove_id: str, on: bool, reason: str) -> bool:
        job = self.service.track("power", on, "garde", "garde", f"garde · {reason}")
        try:
            result = await self.service.send_command(stove_id, "power", on, job["trace"])
        except Exception as err:
            self.service.finish_job(job, stove_id, error=str(err))
            return False
        self.service.finish_job(job, stove_id, result)
        return result["outcome"] == "confirmed"

    def _watch(self, stove_id: str, snap, local: datetime, cfg: dict, st: dict) -> None:
        now = local.timestamp()
        # Pièce froide, poêle éteint, pendant les heures de garde : une fois, puis plus rien tant qu'elle n'est pas remontée.
        thr, amb = cfg["threshold"], snap.ambient
        if st["cold"].get(stove_id):
            if snap.running or (amb is not None and amb >= thr + 0.5):
                st["cold"][stove_id] = False
        elif (not snap.running and amb is not None and 0 < amb < thr and window_end(local, cfg["start"], cfg["end"]) is not None):
            st["cold"][stove_id] = True
            why = self.blocked(stove_id, snap, local)
            self.notify("cold", f"Pièce à {fmt(amb)} °C", f"Sous le seuil de {fmt(thr)} °C, poêle éteint"
                        + (f" ({why})." if why else "." if cfg["enabled"] else ". La garde de température est désactivée."))
        # Poêle injoignable : plus de relevé exploitable depuis 30 min.
        last = self.service.last_ok.get(stove_id, self.started)
        if not st["offline"].get(stove_id) and now - last > OFFLINE_AFTER:
            st["offline"][stove_id] = True
            self.notify("offline", "Poêle injoignable", "Aucune nouvelle du poêle depuis 30 min (cloud MCZ ou Wi-Fi du poêle) : "
                                                         "les allumages programmés risquent de ne pas passer.")
        elif st["offline"].get(stove_id) and now - last < 120:
            st["offline"][stove_id] = False
            self.notify("offline", "Poêle de nouveau joignable", "Les relevés reprennent.")
        # Réservoir bas (capteur du poêle).
        if snap.pellet_low and not st["pellet"].get(stove_id):
            st["pellet"][stove_id] = True
            self.notify("pellet", "Réservoir de pellets bas", "Le capteur du poêle signale un niveau bas : pense à remettre un sac.")
        elif not snap.pellet_low and st["pellet"].get(stove_id):
            st["pellet"][stove_id] = False

    async def _fumes(self, local: datetime, st: dict) -> None:
        """Une fois par jour, à partir de 10 h : fumées plus chaudes de 15 °C à puissance égale → nettoyage conseillé."""
        today = local.date().isoformat()
        if local.hour < 10 or st.get("fumes_day") == today:
            return
        st["fumes_day"] = today
        for stove_id in list(self.service.latest):
            try:
                f = (await self.insights(stove_id)).get("fumes") or {}
            except Exception as err:
                log.warning("fumées : %s", err)
                continue
            if (f.get("change") or 0) >= FUMES_ALERT and local.timestamp() - st.get("fumes_ts", 0) >= FUMES_EVERY:
                st["fumes_ts"] = local.timestamp()
                self.notify("fumes", "Nettoyage du poêle conseillé", f"Fumées {f['change']} °C plus chaudes qu'au début du mois, "
                                                                     "à puissance égale : l'échangeur s'encrasse sans doute.")

    async def _loop(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception as err:
                log.warning("garde : %s", err)
            await asyncio.sleep(self.tick_seconds)

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop())

    def stop(self) -> None:
        if self._task:
            self._task.cancel()
