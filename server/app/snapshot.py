"""État du poêle tel que l'app le consomme (dérivé des champs `Status` / `State` du cloud)."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from .commands import is_lit


@dataclass(frozen=True)
class Snapshot:
    stove_id: str
    fetched_at: float            # époque UNIX de la lecture
    connected: bool              # le poêle joint-il le cloud ?
    in_error: bool
    phase: str                   # fase_op : on, off, turning-on, turning-off…
    mode: str | None             # mod_lav_att : manual, comfort, auto/dynamic, overnight, turbo
    scheduled_off: bool          # un arrêt programmé est en attente (ScheduledTurnOffTimestamp)
    chrono_enabled: bool         # le programme INTERNE du poêle (Maestro, crono_enabled) est actif : il tourne en parallèle de nos plages
    running: bool                # allumé ou en allumage, d'après fase_op (power_enabled seulement si la phase manque)
    ambient: float | None        # temp_amb_install (°C)
    setpoint: float | None       # set_amb1 (°C)
    power_level: int | None      # potenza_att
    manual_power: int | None     # set_pot_man
    fume_temp: float | None      # temp_fumi (°C), vient de /State
    pellet_low: bool             # sens_liv_pellet
    hours_to_service: int | None  # ore_prox_manut

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


def build_snapshot(stove_id: str, status: dict, detail: dict | None, now: float) -> Snapshot:
    detail = detail or {}
    return Snapshot(
        stove_id=stove_id,
        fetched_at=now,
        connected=bool(status.get("IsConnected", True)),
        in_error=bool(status.get("IsInError", False)),
        phase=str(status.get("fase_op") or "unknown"),
        mode=status.get("mod_lav_att") if isinstance(status.get("mod_lav_att"), str) else None,
        scheduled_off=bool(status.get("ScheduledTurnOffTimestamp")),
        chrono_enabled=bool(status.get("crono_enabled", False)),
        running=is_lit(status),
        ambient=_num(status.get("temp_amb_install")),
        setpoint=_num(status.get("set_amb1")),
        power_level=_int(status.get("potenza_att")),
        manual_power=_int(status.get("set_pot_man")),
        fume_temp=_num(detail.get("temp_fumi")),
        pellet_low=bool(status.get("sens_liv_pellet", False)),
        hours_to_service=_int(status.get("ore_prox_manut")),
    )


def _num(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _int(value: Any) -> int | None:
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None
