"""Programme INTERNE du poêle (celui de l'app Maestro+), lu sur le cloud en lecture seule, pour le comparer à nos plages.

Le format exact n'est connu que par le code de l'app (jours `WeekDay` contenant des `Rows` avec `StartTime`/`EndTime`) : la lecture est donc
tolérante (heures en minutes, millisecondes, « HH:MM(:SS) » ou date ISO ; jours en nombre .NET — 0 = dimanche — ou en nom anglais).
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

DOTNET_DAYS = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]


def to_minutes(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        v = int(value)
        if v > 1440:                       # millisecondes depuis minuit
            return (v // 60000) % 1440
        return v % 1440 if v != 1440 else 1440
    if isinstance(value, str):
        text = value.strip()
        try:
            if "T" in text:
                t = datetime.fromisoformat(text.replace("Z", "+00:00"))
                return t.hour * 60 + t.minute
            parts = [int(p) for p in text.split(":")[:2]]
            return parts[0] * 60 + (parts[1] if len(parts) > 1 else 0)
        except ValueError:
            return None
    return None


def to_monday_index(value: Any) -> int | None:
    """Jour .NET (0 = dimanche … 6 = samedi, ou le nom) → notre convention (0 = lundi … 6 = dimanche)."""
    if isinstance(value, str):
        if value.isdigit():
            value = int(value)
        elif value.capitalize() in DOTNET_DAYS:
            value = DOTNET_DAYS.index(value.capitalize())
        else:
            return None
    if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 6:
        return (value + 6) % 7
    return None


def hhmm(minutes: int) -> str:
    return f"{(minutes // 60) % 24:02d}:{minutes % 60:02d}"


def normalize(payload: Any) -> list[dict]:
    days = payload if isinstance(payload, list) else (payload or {}).get("Days") or (payload or {}).get("Items") or []
    slots = []
    for day in days if isinstance(days, list) else []:
        if not isinstance(day, dict):
            continue
        rows = day.get("Rows") if isinstance(day.get("Rows"), list) else [day]
        for row in rows:
            if not isinstance(row, dict):
                continue
            d = to_monday_index(row.get("WeekDay", day.get("WeekDay")))
            start = to_minutes(row.get("StartTime"))
            end = to_minutes(row.get("EndTime"))
            if end is None and start is not None and isinstance(row.get("DurationMilliSeconds", row.get("DurationMilliseconds")), (int, float)):
                end = (start + int(row.get("DurationMilliSeconds", row.get("DurationMilliseconds"))) // 60000) % 1440
            if d is None or start is None or end is None:
                continue
            comfort = row.get("ComfortTemperature")
            slots.append({"day": d, "start": hhmm(start), "end": hhmm(end),
                          "setpoint": float(comfort) if isinstance(comfort, (int, float)) and comfort > 0 else None})
    return sorted(slots, key=lambda s: (s["day"], s["start"]))


def shape(payload: Any) -> dict:
    """Aperçu du format brut (types et 2 premières lignes, sans identifiant) pour vérifier la lecture."""
    days = payload if isinstance(payload, list) else (payload or {}).get("Days") or (payload or {}).get("Items") or []
    sample = []
    for day in (days if isinstance(days, list) else [])[:2]:
        if isinstance(day, dict):
            rows = day.get("Rows") if isinstance(day.get("Rows"), list) else []
            sample.append({"WeekDay": day.get("WeekDay"),
                           "Rows": [{k: r.get(k) for k in ("WeekDay", "StartTime", "EndTime", "DurationMilliSeconds", "ComfortTemperature")}
                                    for r in rows[:2] if isinstance(r, dict)]})
    return {"type": type(payload).__name__, "keys": sorted(payload.keys()) if isinstance(payload, dict) else None, "sample": sample}
