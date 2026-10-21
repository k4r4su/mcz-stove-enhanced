"""Exposition Prometheus (texte) du dernier état connu, pour Grafana."""
from __future__ import annotations

from .snapshot import Snapshot


def render(snapshots: list[Snapshot]) -> str:
    lines: list[str] = []

    def gauge(name: str, help_: str, getter) -> None:
        lines.append(f"# HELP {name} {help_}")
        lines.append(f"# TYPE {name} gauge")
        for s in snapshots:
            value = getter(s)
            if value is not None:
                lines.append(f'{name}{{stove="{s.stove_id}"}} {float(value)}')

    gauge("mcz_ambient_celsius", "Température ambiante", lambda s: s.ambient)
    gauge("mcz_setpoint_celsius", "Consigne", lambda s: s.setpoint)
    gauge("mcz_power_level", "Niveau de puissance actuel", lambda s: s.power_level)
    gauge("mcz_fume_celsius", "Température des fumées", lambda s: s.fume_temp)
    gauge("mcz_running", "Poêle allumé (1) ou éteint (0)", lambda s: int(s.running))
    gauge("mcz_pellet_low", "Niveau de pellets bas", lambda s: int(s.pellet_low))
    gauge("mcz_connected", "Poêle joignable via le cloud", lambda s: int(s.connected))
    gauge("mcz_hours_to_service", "Heures avant l'entretien", lambda s: s.hours_to_service)
    return "\n".join(lines) + "\n"
