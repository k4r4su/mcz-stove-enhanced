"""Pont HomeKit : publie le poêle comme thermostat sur le réseau local (mDNS), à jumeler une fois avec l'app Maison."""
from __future__ import annotations

import logging
import os
import random
import signal
from pathlib import Path

from pyhap.accessory_driver import AccessoryDriver

from .accessory import StoveThermostat
from .bridge import Bridge

DATA = Path(os.environ.get("DATA_DIR", "/data"))


def pin() -> str:
    """Code de jumelage : variable HOMEKIT_PIN, sinon généré une fois et gardé dans /data."""
    wanted = os.environ.get("HOMEKIT_PIN", "").strip()
    if wanted:
        return wanted
    file = DATA / "pin.txt"
    if file.exists():
        return file.read_text().strip()
    banned = {"000-00-000", "111-11-111", "222-22-222", "333-33-333", "444-44-444", "555-55-555", "666-66-666", "777-77-777",
              "888-88-888", "999-99-999", "123-45-678", "876-54-321"}
    while True:
        digits = f"{random.SystemRandom().randrange(10**8):08d}"
        code = f"{digits[:3]}-{digits[3:5]}-{digits[5:]}"
        if code not in banned:
            break
    DATA.mkdir(parents=True, exist_ok=True)
    file.write_text(code)
    return code


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    DATA.mkdir(parents=True, exist_ok=True)
    code = pin()
    driver = AccessoryDriver(port=int(os.environ.get("HOMEKIT_PORT", "51827")), persist_file=str(DATA / "accessory.state"),
                             pincode=code.encode())
    bridge = Bridge(os.environ["BRIDGE_URL"], os.environ["BRIDGE_TOKEN"])
    driver.add_accessory(StoveThermostat(driver, bridge, name=os.environ.get("HOMEKIT_NAME", "Poêle")))
    signal.signal(signal.SIGTERM, driver.signal_handler)
    logging.getLogger("homekit").warning("Code de jumelage HomeKit : %s (Maison → Ajouter un accessoire → Plus d'options)", code)
    driver.start()


if __name__ == "__main__":
    main()
