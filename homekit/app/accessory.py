from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor

from pyhap.accessory import Accessory
from pyhap.const import CATEGORY_THERMOSTAT

from . import mapping
from .bridge import Bridge, BridgeError

log = logging.getLogger("homekit")


class StoveThermostat(Accessory):
    """Le poêle vu comme un thermostat : marche/arrêt (Éteint / Chauffage) et consigne. La confirmation reste gérée par mcz-bridge."""

    category = CATEGORY_THERMOSTAT

    def __init__(self, driver, bridge: Bridge, name: str = "Poêle", poll_seconds: float = 5.0) -> None:
        super().__init__(driver, name)
        self.bridge = bridge
        self.stove_id: str | None = None
        self._pool = ThreadPoolExecutor(max_workers=1)   # une commande à la fois, hors de la boucle HomeKit
        self.run_at_interval_seconds = poll_seconds
        service = self.add_preload_service("Thermostat")
        self.char_current_state = service.get_characteristic("CurrentHeatingCoolingState")
        self.char_target_state = service.get_characteristic("TargetHeatingCoolingState")
        self.char_current_temp = service.get_characteristic("CurrentTemperature")
        self.char_target_temp = service.get_characteristic("TargetTemperature")
        # Le poêle ne refroidit pas : Éteint (0) et Chauffage (1) seulement.
        self.char_target_state.override_properties(valid_values={"Off": 0, "Heat": 1})
        self.char_target_temp.override_properties(properties={"minValue": 5, "maxValue": 40, "minStep": 0.5})
        self.char_current_temp.override_properties(properties={"minValue": -10, "maxValue": 60})
        self.char_target_state.setter_callback = self._set_target_state
        self.char_target_temp.setter_callback = self._set_target_temp

    # ---- HomeKit → poêle -------------------------------------------------------

    def _set_target_state(self, value: int) -> None:
        self._pool.submit(self._send, "power", mapping.power_command(value))

    def _set_target_temp(self, value: float) -> None:
        self._pool.submit(self._send, "setpoint", mapping.setpoint_command(value))

    def _send(self, kind: str, value) -> None:
        if not self.stove_id:
            log.warning("commande %s ignorée : poêle pas encore connu", kind)
            return
        try:
            result = self.bridge.command(self.stove_id, kind, value)
            log.info("%s=%s → %s en %s essai(s)", kind, value, result.get("outcome"), result.get("attempts"))
        except Exception as err:
            log.warning("commande %s échouée : %s", kind, err)

    # ---- poêle → HomeKit ---------------------------------------------------------

    def apply(self, stove: dict) -> None:
        self.stove_id = stove.get("stove_id", self.stove_id)
        self.char_current_state.set_value(mapping.current_state(stove))
        self.char_target_state.set_value(mapping.target_state(stove))
        self.char_current_temp.set_value(mapping.current_temperature(stove))
        self.char_target_temp.set_value(mapping.target_temperature(stove))

    @Accessory.run_at_interval(5)
    async def run(self) -> None:
        try:
            stove = await self.driver.loop.run_in_executor(None, self.bridge.stove)
        except (BridgeError, OSError, Exception) as err:  # le réseau ou le serveur peut être indisponible : on garde les dernières valeurs
            log.warning("relevé impossible : %s", err)
            return
        if stove:
            self.apply(stove)
