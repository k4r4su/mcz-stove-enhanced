"""Traduction poêle ↔ thermostat HomeKit (fonctions pures, testées)."""
from __future__ import annotations

OFF, HEAT = 0, 1


def current_state(stove: dict) -> int:
    """CurrentHeatingCoolingState : 1 seulement quand le poêle chauffe (en marche)."""
    return HEAT if stove.get("running") and stove.get("phase") == "on" else OFF


def target_state(stove: dict) -> int:
    """TargetHeatingCoolingState : ce que l'utilisateur veut (allumé ou non), allumage en cours compris."""
    return HEAT if stove.get("running") else OFF


def current_temperature(stove: dict) -> float:
    value = stove.get("ambient")
    return float(value) if isinstance(value, (int, float)) and value > 0 else 20.0   # 0 = relevé vide (module endormi)


def target_temperature(stove: dict) -> float:
    value = stove.get("setpoint")
    return min(40.0, max(5.0, float(value))) if isinstance(value, (int, float)) and value > 0 else 20.0


def power_command(homekit_target: int) -> bool:
    """HomeKit propose Off(0) / Heat(1) / Cool(2) / Auto(3) : tout sauf Off allume."""
    return homekit_target != OFF


def setpoint_command(homekit_value: float) -> float:
    """Arrondi au demi-degré, dans les limites du poêle."""
    return min(40.0, max(5.0, round(homekit_value * 2) / 2))
