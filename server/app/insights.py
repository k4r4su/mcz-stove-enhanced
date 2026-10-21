"""Indicateurs calculés sur l'historique : coût, chauffe comparée à la météo, refroidissement de la maison, confort.

Tout part des relevés déjà enregistrés (phase, température de la pièce, consigne, puissance) et, pour la météo passée,
des températures extérieures heure par heure d'Open-Meteo. Les kg (donc les €) sont estimés d'après la fiche MCZ.
"""
from __future__ import annotations

import math
import statistics
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

DJU_BASE = 18.0          # °C : base des degrés-jours unifiés (convention française)
MIN_COVERAGE = 0.8       # part de la journée couverte par des relevés pour qu'un jour compte dans une comparaison
GAP = 900                # s : au-delà, deux relevés ne se suivent plus (serveur arrêté)
NIGHT = (22, 7)          # refroidissement mesuré la nuit : ni soleil ni cuisine
RESIDUAL = 3600          # s : après l'extinction, le poêle chaud chauffe encore la pièce
MIN_NIGHT = 2 * 3600     # s : nuit trop courte → rien d'appris
REACHED = 0.3            # °C sous la consigne : consigne considérée comme atteinte
HELD = 0.5               # °C sous la consigne : consigne considérée comme tenue
WARMUP_MAX = 5 * 3600    # s : on suit chaque montée en température 5 h au plus


def _lit(s: dict) -> bool:
    return bool(s["running"])


def _amb(s: dict) -> float | None:
    a = s.get("ambient")
    return a if a is not None and a > 0 else None


def degree_days(mean: float | None) -> float | None:
    return None if mean is None else max(0.0, DJU_BASE - mean)


def outdoor_daily(hourly: dict[float, float], tz: ZoneInfo) -> dict[str, float]:
    """Moyenne extérieure de chaque jour (heure locale), à partir des relevés horaires."""
    by_day: dict[str, list[float]] = {}
    for ts, t in hourly.items():
        by_day.setdefault(datetime.fromtimestamp(ts, tz).date().isoformat(), []).append(t)
    return {d: round(sum(v) / len(v), 1) for d, v in by_day.items() if len(v) >= 18}


def outdoor_between(hourly: dict[float, float], start: float, end: float) -> float | None:
    vals = [t for ts, t in hourly.items() if start - 1800 <= ts <= end + 1800]
    return sum(vals) / len(vals) if vals else None


# ---- Chauffe et météo ---------------------------------------------------------------

def weather_compare(days: list[dict], today: date) -> dict | None:
    """kg de pellets par degré-jour : 7 derniers jours complets contre les 7 d'avant (à météo comparable)."""
    def window(first: int, last: int):
        picked = [d for d in days if first <= (today - date.fromisoformat(d["date"])).days <= last
                  and d.get("coverage", 0) >= MIN_COVERAGE and d.get("dju") is not None]
        kg, dju = sum(d["pellets_kg_est"] for d in picked), sum(d["dju"] for d in picked)
        return picked, kg, dju
    cur, kg1, dju1 = window(1, 7)
    prev, kg2, dju2 = window(8, 14)
    out: dict = {"kg_per_dju": round(kg1 / dju1, 2) if len(cur) >= 3 and dju1 >= 5 else None, "days": len(cur)}
    if out["kg_per_dju"] is not None and len(prev) >= 3 and dju2 >= 5 and kg2 > 0:
        before = kg2 / dju2
        out.update(prev_kg_per_dju=round(before, 2), change_pct=round((kg1 / dju1 - before) / before * 100))
    return out


# ---- Refroidissement de la maison -----------------------------------------------------

def _nights(samples: list[dict], tz: ZoneInfo):
    """Morceaux de nuit (22 h → 7 h) où le poêle est éteint depuis au moins une heure, sans trou dans les relevés."""
    run: list[dict] = []
    off_since = None
    prev = None
    for s in samples:
        broken = prev is not None and s["ts"] - prev["ts"] > GAP
        if _lit(s) or broken:
            if len(run) > 1:
                yield run
            run = []
            off_since = None if _lit(s) else s["ts"]
            prev = s
            continue
        if off_since is None:
            off_since = s["ts"]
        hour = datetime.fromtimestamp(s["ts"], tz).hour
        at_night = hour >= NIGHT[0] or hour < NIGHT[1]
        if at_night and s["ts"] - off_since >= RESIDUAL and _amb(s) is not None:
            if run and datetime.fromtimestamp(run[0]["ts"] - 12 * 3600, tz).date() != datetime.fromtimestamp(s["ts"] - 12 * 3600, tz).date():
                yield run
                run = []
            run.append(s)
        elif run:
            yield run
            run = []
        prev = s
    if len(run) > 1:
        yield run


def _edge(points: list[dict], first: bool) -> float:
    """Température lissée sur les 15 premières (ou dernières) minutes : le capteur avance par pas."""
    t0 = points[0]["ts"] if first else points[-1]["ts"]
    vals = [_amb(p) for p in points if abs(p["ts"] - t0) <= 900]
    return statistics.median(vals)


def cooling(samples: list[dict], hourly: dict[float, float], tz: ZoneInfo) -> dict | None:
    """Vitesse à laquelle la pièce se refroidit, poêle éteint, la nuit.

    Loi de Newton : la pièce perd d'autant plus vite que l'écart avec dehors est grand. On en tire k (par heure),
    qui permet d'estimer la perte par n'importe quel temps : °C/h = k × (dedans − dehors).
    """
    nights = []
    for run in _nights(samples, tz):
        span = run[-1]["ts"] - run[0]["ts"]
        if span < MIN_NIGHT:
            continue
        t0, t1, hours = _edge(run, True), _edge(run, False), span / 3600
        if t1 > t0 + 0.2:          # la pièce s'est réchauffée : autre source de chaleur, on ignore
            continue
        out = outdoor_between(hourly, run[0]["ts"], run[-1]["ts"])
        k = None
        if out is not None and t0 - out >= 3 and t1 - out > 0.5:
            k = math.log((t0 - out) / (t1 - out)) / hours
        nights.append({"start_ts": run[0]["ts"], "end_ts": run[-1]["ts"], "from": round(t0, 1), "to": round(t1, 1),
                       "per_hour": round((t0 - t1) / hours, 2), "outdoor": round(out, 1) if out is not None else None, "k": k})
    if not nights:
        return None
    ks = [n["k"] for n in nights if n["k"] is not None]
    k = statistics.median(ks) if ks else None
    result = {"nights": len(nights), "per_hour": round(statistics.median(n["per_hour"] for n in nights), 2),
              "outdoor": _median_or_none([n["outdoor"] for n in nights if n["outdoor"] is not None]),
              "k": round(k, 4) if k else None, "last": {kk: v for kk, v in nights[-1].items() if kk != "k"}, "table": []}
    if k:
        for out in (10, 5, 0, -5):
            hours_21_to_18 = math.log((21 - out) / (18 - out)) / k
            result["table"].append({"outdoor": out, "per_hour": round(k * (20 - out), 2), "hours_21_to_18": round(hours_21_to_18, 1)})
    return result


def _median_or_none(values: list[float]) -> float | None:
    return round(statistics.median(values), 1) if values else None


# ---- Confort ------------------------------------------------------------------------

def comfort(samples: list[dict], since_ts: float) -> dict | None:
    """Temps pour atteindre la consigne après chaque allumage, puis part du temps où elle est tenue."""
    warmups, held_s, total_s = [], 0.0, 0.0
    i = 1
    while i < len(samples):
        prev, cur = samples[i - 1], samples[i]
        if _lit(prev) or not _lit(cur) or cur["ts"] - prev["ts"] > GAP or cur["ts"] < since_ts:
            i += 1
            continue
        t0, a0 = cur["ts"], _amb(prev)
        sp0 = next((s["setpoint"] for s in samples[i:i + 30] if s.get("setpoint")), None)
        reached_at, best = None, a0
        cold = a0 is not None and sp0 is not None and a0 < sp0 - HELD
        j = i
        while j < len(samples) and _lit(samples[j]) and (j == i or samples[j]["ts"] - samples[j - 1]["ts"] <= GAP):
            s, a, sp = samples[j], _amb(samples[j]), samples[j].get("setpoint")
            if a is not None and sp:
                best = a if best is None else max(best, a)
                if reached_at is None and (a >= sp - REACHED or not cold):
                    reached_at = s["ts"]
                if reached_at is not None and j + 1 < len(samples):
                    dt = min(samples[j + 1]["ts"] - s["ts"], GAP)
                    total_s += dt
                    held_s += dt if a >= sp - HELD else 0
            j += 1
        end_ts = samples[j - 1]["ts"]
        if cold:
            warmups.append({"ts": t0, "from": round(a0, 1), "setpoint": sp0, "best": round(best, 1) if best is not None else None,
                            "minutes": round((reached_at - t0) / 60) if reached_at and reached_at - t0 <= WARMUP_MAX else None,
                            "lit_minutes": round((end_ts - t0) / 60)})
        i = max(j, i + 1)
    if not warmups and not total_s:
        return None
    times = [w["minutes"] for w in warmups if w["minutes"] is not None]
    return {"ignitions": len(warmups), "reached": len(times), "median_minutes": round(statistics.median(times)) if times else None,
            "held_pct": round(held_s / total_s * 100) if total_s >= 3600 else None, "recent": warmups[-5:][::-1]}


# ---- Coût ---------------------------------------------------------------------------

def season_start(today: date) -> date:
    """La saison de chauffe commence le 1er septembre."""
    return date(today.year if today.month >= 9 else today.year - 1, 9, 1)


def money(kg: float, eur_per_kg: float | None) -> float | None:
    return None if eur_per_kg is None else round(kg * eur_per_kg, 2)


def midnight(day: date, tz: ZoneInfo) -> float:
    return datetime.combine(day, datetime.min.time(), tz).timestamp()


def month_start(today: date) -> date:
    return today.replace(day=1)


def week_start(today: date) -> date:
    return today - timedelta(days=6)


# ---- Fumées -------------------------------------------------------------------------

def fumes(samples: list[dict], tz: ZoneInfo, today: date) -> dict | None:
    """Température des fumées en marche stable (phase « on »), par jour et à puissance égale.

    Comparée puissance par puissance entre la semaine passée et la plus ancienne semaine connue (30 jours au plus) :
    une hausse durable à puissance égale signale en général un échangeur encrassé.
    """
    per_day: dict[str, list[float]] = {}
    by_level: dict[tuple[str, int], list[float]] = {}
    for s in samples:
        f = s.get("fume_temp")
        if s.get("phase") != "on" or f is None or f <= 0:
            continue
        day = datetime.fromtimestamp(s["ts"], tz).date().isoformat()
        per_day.setdefault(day, []).append(f)
        if s.get("power_level"):
            by_level.setdefault((day, int(s["power_level"])), []).append(f)
    if not per_day:
        return None
    days = sorted(per_day)
    recent = {d for d in days if (today - date.fromisoformat(d)).days <= 6}
    oldest = set(days[:7]) - recent
    deltas, weights = [], []
    for level in {lv for (_, lv) in by_level}:
        now = [f for (d, lv), v in by_level.items() if lv == level and d in recent for f in v]
        then = [f for (d, lv), v in by_level.items() if lv == level and d in oldest for f in v]
        if len(now) >= 10 and len(then) >= 10:
            deltas.append(statistics.median(now) - statistics.median(then))
            weights.append(min(len(now), len(then)))
    change = round(sum(d * w for d, w in zip(deltas, weights)) / sum(weights)) if weights else None
    return {"days": [{"date": d, "avg": round(sum(v) / len(v))} for d, v in sorted(per_day.items())],
            "change": change, "since": min(oldest) if oldest and change is not None else None}
