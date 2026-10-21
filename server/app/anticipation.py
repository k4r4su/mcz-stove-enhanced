"""Allumage anticipé : combien de minutes avant l'heure voulue faut-il allumer pour qu'il fasse `consigne` °C ?

Modèle simple, appris sur l'historique des allumages :
  avance = démarrage + (consigne − température actuelle − 0,3) / vitesse
 - démarrage : minutes entre l'allumage et les premiers +0,3 °C dans la pièce (le poêle doit d'abord chauffer) ;
 - vitesse : °C gagnés par minute ensuite.
Valeurs par défaut prudentes tant qu'aucun allumage n'a été observé. La température de la pièce au moment de décider
reflète déjà la nuit plus ou moins froide ; la météo n'entre pas dans le calcul (pas d'historique extérieur pour la caler).
"""
from __future__ import annotations

import statistics
import time
from dataclasses import dataclass
from typing import Callable

DEFAULT_STARTUP = 20.0        # min
DEFAULT_RATE = 0.04           # °C/min (2,4 °C/h)
MIN_LEAD, MAX_LEAD = 15, 180  # min
DETECT = 0.3                  # °C : première hausse qui montre que le poêle chauffe la pièce
MIN_RISE = 1.0                # °C : un allumage qui gagne moins n'apprend rien de fiable
WINDOW = 3 * 3600             # s : on suit chaque allumage 3 h au plus
DEFAULT_DELTA = 3.0           # °C à gagner quand la température de la pièce est inconnue


@dataclass(frozen=True)
class Model:
    startup_min: float
    rate_per_min: float
    episodes: int

    def to_json(self) -> dict:
        return {"startup_min": round(self.startup_min), "rate_per_hour": round(self.rate_per_min * 60, 1), "episodes": self.episodes}


def _lit(sample: dict) -> bool:
    phase = sample.get("phase")
    if phase and phase != "unknown":
        return phase not in ("off", "turning-off")
    return bool(sample.get("running"))


def learn(samples: list[dict]) -> Model:
    """Repère chaque allumage (éteint → allumé) et mesure démarrage et vitesse de montée tant que le poêle reste allumé."""
    startups, rates = [], []
    i = 1
    while i < len(samples):
        prev, cur = samples[i - 1], samples[i]
        if _lit(prev) or not _lit(cur) or cur["ts"] - prev["ts"] > 900 or prev.get("ambient") is None:
            i += 1
            continue
        t0, a0 = cur["ts"], prev["ambient"]
        startup, best_rise, best_min = None, 0.0, 0.0
        j = i
        while j < len(samples) and _lit(samples[j]) and samples[j]["ts"] - t0 <= WINDOW:
            ambient = samples[j].get("ambient")
            if ambient is not None:
                minutes, rise = (samples[j]["ts"] - t0) / 60, ambient - a0
                if startup is None and rise >= DETECT:
                    startup = minutes
                if rise > best_rise:
                    best_rise, best_min = rise, minutes
            j += 1
        if startup is not None and best_rise >= MIN_RISE and best_min > startup:
            startups.append(startup)
            rates.append((best_rise - DETECT) / (best_min - startup))
        i = max(j, i + 1)
    if not rates:
        return Model(DEFAULT_STARTUP, DEFAULT_RATE, 0)
    return Model(min(90.0, statistics.median(startups)), min(0.3, max(0.005, statistics.median(rates))), len(rates))


def lead_minutes(model: Model, ambient: float | None, setpoint: float) -> int:
    delta = DEFAULT_DELTA if ambient is None else max(0.0, setpoint - ambient)
    lead = model.startup_min + max(0.0, delta - DETECT) / model.rate_per_min
    return int(round(min(MAX_LEAD, max(MIN_LEAD, lead))))


class Anticipator:
    """Garde le modèle appris (recalculé toutes les 30 min sur 30 jours d'historique) et donne l'avance d'une plage."""

    def __init__(self, service: Callable[[], object], refresh_seconds: float = 1800) -> None:
        self._service, self._refresh = service, refresh_seconds
        self._model: Model | None = None
        self._at = 0.0

    def model(self) -> Model:
        if self._model is None or time.time() - self._at > self._refresh:
            svc = self._service()
            stove_id = next(iter(getattr(svc, "latest", {}) or {}), None)
            samples = svc.history.since(stove_id, time.time() - 30 * 86400) if stove_id else []
            self._model, self._at = learn(samples), time.time()
        return self._model

    def ambient(self) -> float | None:
        latest = getattr(self._service(), "latest", {}) or {}
        snap = next(iter(latest.values()), None)
        return snap.ambient if snap and snap.ambient and snap.ambient > 0 else None

    def lead_for(self, plage: dict) -> int:
        return lead_minutes(self.model(), self.ambient(), float(plage.get("setpoint") or 21.0))
