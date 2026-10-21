"""Commandes utilisateur du poêle (liste blanche) : identifiants relevés par `tools/mczcloud.py dump` sur le poêle de référence.

Seules ces commandes existent. Les ~90 réglages installateur du modèle (reset usine, vitesses, temps…) ne sont volontairement pas exposés.
Toutes sont ABSOLUES (« consigne 21 », pas « +1 ») : renvoyer une commande déjà prise en compte est sans effet.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

MODES = {"manual": 0, "auto": 1, "overnight": 2, "comfort": 3}
# Nom renvoyé par `mod_lav_att` dans /Status pour chaque mode (« auto » s'appelle « dynamic » côté capteur).
MODE_STATUS_NAMES = {"manual": {"manual"}, "auto": {"auto", "dynamic"}, "overnight": {"overnight"}, "comfort": {"comfort"}}


@dataclass(frozen=True)
class Spec:
    config_id: str
    sensor_id: str
    wire_value: Any
    confirmed_by: Callable[[dict], bool]   # lit /Status : la commande est-elle visible dans l'état ?
    # Autres formes à essayer si le cloud refuse la première (HTTP 400) : (configuration, capteur, valeur)
    fallbacks: tuple[tuple[str, str, Any], ...] = ()
    # Bascule (comme le bouton marche/arrêt) : la renvoyer annulerait la première. Envoi unique, attente plus longue.
    toggle: bool = False


# Phases `fase_op` vues sur ce poêle. « cooling-down » (vu le 15/10 de 00:10 à 00:40, fumées 60 °C en baisse) suit une
# extinction : flamme éteinte, ventilation de refroidissement. Compté « allumé », il donnait de faux « Allumage Ailleurs ».
UNLIT_PHASES = {"off", "turning-off", "cooling-down"}
LIT_PHASES = {"on", "turning-on"}


def is_lit(status: dict) -> bool:
    """Poêle allumé ou en train de s'allumer ? D'après la phase ; power_enabled seulement si la phase manque."""
    phase = status.get("fase_op")
    if phase:
        return phase not in UNLIT_PHASES
    return bool(status.get("power_enabled"))


def phase_known(status: dict) -> bool:
    """Phase connue (ou absente) : sinon on ne sait pas si un appui sur marche/arrêt allumerait ou éteindrait."""
    phase = status.get("fase_op")
    return not phase or phase in UNLIT_PHASES or phase in LIT_PHASES


def build(kind: str, value: Any) -> Spec:
    """Valide la commande et prépare son envoi. Lève ValueError si elle est inconnue ou hors limites."""
    if kind == "power":
        if not isinstance(value, bool):
            raise ValueError("power attend true ou false")
        # Dans l'app Maestro+ (StoveTurnOnPage et notification d'extinction), ALLUMER comme ÉTEINDRE envoient la même chose :
        # configuration « Spegnimento » (CONFIGURATION_TURN_OFF, e373bb67), capteur com_on_off (SENSOR_TURN_OFF) = true.
        # C'est un appui sur le bouton marche/arrêt : une bascule. Vérifié le 13/10 pour l'extinction (passage en « turning-off »).
        # Booléen obligatoire (le cloud refuse 0/1) ; la configuration « Alexa » est acceptée par le cloud mais ignorée par ce poêle.
        # La phase `fase_op` fait foi : le 14/10 à 06:35, le poêle éteint renvoyait fase_op « off » avec power_enabled true
        # (ce drapeau ne veut pas dire « allumé »). power_enabled ne sert que si la phase manque.
        return Spec("e373bb67-fe69-4d49-b2f2-4b636fa68e37", "9c7f0959-47a2-44ea-906d-f5d22218d00e", True,
                    lambda s: is_lit(s) is value, toggle=True)
    if kind == "setpoint":
        number = _number(value)
        if not 5 <= number <= 40 or (number * 2) % 1:
            raise ValueError("consigne : entre 5 et 40 °C, par pas de 0,5")
        return Spec("875fb696-4af1-42da-bb78-ebe57c90fe5c", "b866d2a9-d0ab-4558-8367-910c52261be1", number,
                    lambda s: _close(s.get("set_amb1"), number))
    if kind == "power_level":
        level = _number(value)
        if level != int(level) or not 1 <= level <= 5:
            raise ValueError("puissance : entier de 1 à 5")
        return Spec("fa2e1c9e-83e3-4435-825b-cdfb50da92a7", "f7750f3e-9ff2-4351-b53a-6d1a9d1f6bc6", int(level),
                    lambda s: s.get("set_pot_man") == int(level))
    if kind == "mode":
        if value not in MODES:
            raise ValueError(f"mode : {', '.join(MODES)}")
        return Spec("d85b93a4-4cae-4745-925d-ef53547c661e", "bd064ae8-9091-4696-b137-5a4e243ebb91", MODES[value],
                    lambda s: s.get("mod_lav_att") in MODE_STATUS_NAMES[value])
    if kind == "sleep_timer":
        minutes = _number(value)
        if minutes != int(minutes) or not 0 <= minutes <= 480:
            raise ValueError("arrêt programmé : 0 (annule) à 480 minutes")
        # Pas de champ d'état fiable pour l'arrêt programmé : confirmé par `ScheduledTurnOffTimestamp`.
        return Spec("a088a0dd-4f34-4cc9-a4f1-3b0f1d182777", "dbb345f2-326d-4514-8d39-cc246fe5a930", int(minutes),
                    lambda s: (s.get("ScheduledTurnOffTimestamp", 0) != 0) is (minutes > 0))
    raise ValueError(f"commande inconnue : {kind}")


def _number(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("valeur numérique attendue")
    return float(value)


def _close(actual: Any, expected: float) -> bool:
    return isinstance(actual, (int, float)) and abs(float(actual) - expected) < 0.05
