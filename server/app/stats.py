"""Statistiques (heures de chauffe, températures) et estimation du niveau de pellets, calculées à partir de l'historique."""
from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

MAX_GAP = 300.0  # au-delà (serveur arrêté), on ne suppose rien sur ce qui s'est passé
# Consommation de la fiche MCZ : 0,60 kg/h à la puissance 1, 2,30 kg/h à la puissance 5, interpolée.
DATASHEET_KG_PER_HOUR = {1: 0.60, 2: 1.025, 3: 1.45, 4: 1.875, 5: 2.30}
# 20/10/2026 : avec la fiche, le niveau estimé restait au moins un tiers au-dessus du réservoir réel (constat du propriétaire) :
# le poêle brûle plus que la fiche (allumages, nettoyages du brasier, granulés). On compte 1,5 fois la fiche.
# 21/10/2026 : à 1,5 le niveau affichait 0 avec encore un peu de granulés au fond : 5 % de moins (1,425).
CONSUMPTION_FACTOR = 1.425
KG_PER_HOUR = {p: kg * CONSUMPTION_FACTOR for p, kg in DATASHEET_KG_PER_HOUR.items()}


def rate(power_level: int | None) -> float:
    return KG_PER_HOUR.get(int(power_level or 0), 0.0)


def _intervals(samples: list[dict], now_ts: float):
    """Chaque relevé vaut jusqu'au suivant (au plus MAX_GAP secondes)."""
    for i, s in enumerate(samples):
        end = samples[i + 1]["ts"] if i + 1 < len(samples) else now_ts
        dt = max(0.0, min(end - s["ts"], MAX_GAP))
        if dt:
            yield s, dt


def daily_stats(samples: list[dict], tz: ZoneInfo, days: int, now: datetime) -> list[dict]:
    now = now.astimezone(tz)
    first_day = now.date() - timedelta(days=days - 1)
    out = {first_day + timedelta(days=i): {"run_s": 0.0, "kg": 0.0, "amb_sum": 0.0, "amb_s": 0.0, "by_power": {}, "seen_s": 0.0} for i in range(days)}
    for s, dt in _intervals(samples, now.timestamp()):
        day = datetime.fromtimestamp(s["ts"], tz).date()
        if day not in out:
            continue
        d = out[day]
        d["seen_s"] += dt
        if s["ambient"] is not None and s["ambient"] > 0:
            d["amb_sum"] += s["ambient"] * dt
            d["amb_s"] += dt
        if s["running"]:
            d["run_s"] += dt
            d["kg"] += rate(s["power_level"]) * dt / 3600
            level = int(s["power_level"] or 0)
            d["by_power"][level] = d["by_power"].get(level, 0.0) + dt / 3600
    return [{"date": day.isoformat(), "run_hours": round(d["run_s"] / 3600, 2), "pellets_kg_est": round(d["kg"], 2),
             "avg_ambient": round(d["amb_sum"] / d["amb_s"], 1) if d["amb_s"] else None,
             "hours_by_power": {str(k): round(v, 2) for k, v in sorted(d["by_power"].items())},
             "coverage": round(min(1.0, d["seen_s"] / _day_seconds(day, tz, now)), 2)}
            for day, d in out.items()]


def _day_seconds(day, tz: ZoneInfo, now: datetime) -> float:
    """Durée de la journée écoulée (aujourd'hui : jusqu'à maintenant), pour la part couverte par des relevés."""
    start = datetime.combine(day, datetime.min.time(), tz)
    end = min(now, datetime.combine(day + timedelta(days=1), datetime.min.time(), tz))
    return max(1.0, (end - start).total_seconds())


def consumed_kg(samples: list[dict], since_ts: float, now_ts: float) -> float:
    window = [s for s in samples if s["ts"] >= since_ts]
    return sum(rate(s["power_level"]) * dt / 3600 for s, dt in _intervals(window, now_ts) if s["running"])


def pellet_level(refills: list[dict], samples: list[dict], capacity_kg: float, now_ts: float, current_power: int | None, running: bool) -> dict:
    """Niveau estimé : on repart du dernier remplissage saisi. Un premier sac est supposé versé dans un réservoir vide."""
    if not refills:
        return {"known": False, "capacity_kg": capacity_kg, "estimated": True}
    level: float | None = None
    last_ts = None
    for r in sorted(refills, key=lambda r: r["ts"]):
        if level is not None:
            level = max(0.0, level - consumed_between(samples, last_ts, r["ts"]))
        level = capacity_kg if r["full"] else min(capacity_kg, (level or 0.0) + (r["kg"] or 0.0))
        last_ts = r["ts"]
    level = max(0.0, level - consumed_between(samples, last_ts, now_ts))
    r_now = rate(current_power) if running else 0.0
    return {"known": True, "estimated": True, "capacity_kg": capacity_kg, "remaining_kg": round(level, 1),
            "hours_left": round(level / r_now, 1) if r_now else None, "last_refill_ts": last_ts}


def consumed_between(samples: list[dict], start_ts: float, end_ts: float) -> float:
    window = [s for s in samples if start_ts <= s["ts"] < end_ts]
    return sum(rate(s["power_level"]) * min(dt, max(0.0, end_ts - s["ts"])) / 3600
               for s, dt in _intervals(window, end_ts) if s["running"])
