"""Prévision de la température de la pièce sur les 12 prochaines heures, pour décider quand rallumer.

Deux scénarios simulés par pas de 5 min :
 - « planning » : le poêle suit les allumages et extinctions prévus (plages, minuteries, vacances) ;
 - « sans rallumage » : les extinctions prévues ont lieu, mais aucun allumage.
Poêle éteint, la pièce perd k × (dedans − dehors) °C par heure (k appris les nuits précédentes, dehors = prévision Open-Meteo) ;
la première heure après l'extinction, le poêle encore chaud la maintient. Allumé : rien pendant le démarrage, puis la vitesse de
montée apprise (anticipation.py) jusqu'à la consigne, qui est ensuite tenue.
"""
from __future__ import annotations

from dataclasses import dataclass

STEP = 300                 # s
HORIZON = 12 * 3600        # s
RESIDUAL = 3600            # s : chaleur du poêle après extinction
DEFAULT_K = 0.03           # par heure, tant qu'aucune nuit n'a été mesurée (≈ 0,4 °C/h avec 6 °C dehors)
DEFAULT_OUTDOOR = 8.0      # °C, si la météo est indisponible
REACHED = 0.3              # °C sous la consigne : atteinte


@dataclass
class State:
    ambient: float
    lit: bool
    setpoint: float
    lit_since: float | None     # début de l'allumage en cours
    off_since: float | None     # début de l'extinction en cours


def outdoor_at(hourly: dict[float, float], ts: float) -> float | None:
    """Température extérieure à `ts`, interpolée entre les deux heures qui l'encadrent."""
    if not hourly:
        return None
    h0 = ts - ts % 3600
    a, b = hourly.get(h0), hourly.get(h0 + 3600)
    if a is not None and b is not None:
        return a + (b - a) * (ts - h0) / 3600
    return a if a is not None else b


def simulate(now: float, state: State, events: list[dict], hourly: dict[float, float], k: float,
             startup_min: float, rate_per_min: float, allow_on: bool = True, horizon: float = HORIZON) -> list[dict]:
    temp, lit, setpoint = state.ambient, state.lit, state.setpoint
    lit_since, off_since = state.lit_since, state.off_since
    if lit and lit_since is None:
        lit_since = now - startup_min * 60          # inconnu : supposé déjà en chauffe
    if not lit and off_since is None:
        off_since = now - RESIDUAL
    pending = sorted((e for e in events if now < e["ts"] <= now + horizon), key=lambda e: e["ts"])
    points = [{"ts": now, "temp": round(temp, 2), "lit": lit}]
    t = now
    while t < now + horizon:
        t += STEP
        while pending and pending[0]["ts"] <= t:
            e = pending.pop(0)
            if e["kind"] == "on" and allow_on and not lit:
                lit, lit_since = True, e["ts"]
                setpoint = e.get("setpoint") or setpoint
            elif e["kind"] == "off" and lit:
                lit, off_since = False, e["ts"]
        out = outdoor_at(hourly, t)
        out = DEFAULT_OUTDOOR if out is None else out
        loss = k * max(0.0, temp - out) * STEP / 3600
        if lit:
            if t - lit_since < startup_min * 60:      # démarrage : la pièce continue de refroidir
                temp -= loss
            elif temp < setpoint:
                temp = min(setpoint, temp + rate_per_min * STEP / 60)
            else:                                      # consigne atteinte : le poêle module pour la tenir
                temp = max(setpoint, temp - loss)
        elif t - off_since >= RESIDUAL:                # la première heure, le poêle encore chaud maintient la pièce
            temp -= loss
        points.append({"ts": t, "temp": round(temp, 2), "lit": lit})
    return points


def advise(now: float, state: State, events: list[dict], hourly: dict[float, float], k: float | None,
           startup_min: float, rate_per_min: float, threshold: float) -> dict:
    learned = k is not None
    k = k if learned else DEFAULT_K
    plan = simulate(now, state, events, hourly, k, startup_min, rate_per_min)
    free = simulate(now, state, events, hourly, k, startup_min, rate_per_min, allow_on=False)
    next_on = next((e for e in sorted(events, key=lambda e: e["ts"]) if e["kind"] == "on" and now < e["ts"] <= now + HORIZON), None)
    cross = next((p for p in free if not p["lit"] and p["temp"] < threshold), None)
    relight = None
    if cross:
        # Rallumer assez tôt pour que la chaleur arrive avant de passer sous le seuil (la pièce refroidit encore pendant le démarrage).
        relight = max(now, (cross["ts"] - startup_min * 60) // STEP * STEP)
    at_next_on = None
    if next_on:
        at_next_on = min(free, key=lambda p: abs(p["ts"] - next_on["ts"]))["temp"]
    # Quand fera-t-il la consigne ? Allumé : la consigne en cours ; éteint : celle du prochain allumage prévu.
    target = state.setpoint if state.lit or not next_on else (next_on.get("setpoint") or state.setpoint)
    reached_now = state.lit and state.ambient >= target - REACHED
    reached = reach_time(plan, target)
    on_time = bool(next_on and (cross is None or next_on["ts"] <= relight))
    relight_reach = None
    if relight is not None and not on_time:
        extra = [*events, {"ts": max(relight, now + 1), "kind": "on", "setpoint": target}]
        relight_reach = reach_time(simulate(now, state, extra, hourly, k, startup_min, rate_per_min), target, after=relight)
    return {
        "threshold": threshold, "learned": learned, "k": round(k, 4), "startup_min": round(startup_min),
        "plan": plan, "free": free,
        "cross_ts": cross["ts"] if cross else None, "relight_ts": relight,
        "next_on": {**next_on, "temp_then": at_next_on, "reached_ts": reached} if next_on else None,
        "on_time": on_time,
        "target": target, "reached_now": reached_now, "reach_ts": None if reached_now else reached, "relight_reach_ts": relight_reach,
        "min_free": round(min(p["temp"] for p in free), 1), "min_plan": round(min(p["temp"] for p in plan), 1), "end_ts": now + HORIZON,
    }


def reach_time(points: list[dict], target: float, after: float = 0.0) -> float | None:
    """Premier instant (après `after`) où la pièce, poêle allumé, arrive à la consigne (à 0,3 °C près)."""
    return next((p["ts"] for p in points[1:] if p["ts"] > after and p["lit"] and p["temp"] >= target - REACHED), None)
