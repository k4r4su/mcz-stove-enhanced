import asyncio
import json
import os
import sys
import tempfile

import httpx
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from app import mapping
from app.bridge import Bridge

STOVE = {"stove_id": "s§1", "running": True, "phase": "on", "ambient": 23.8, "setpoint": 21.0}


def test_state_mapping():
    assert mapping.current_state(STOVE) == 1 and mapping.target_state(STOVE) == 1
    assert mapping.current_state({**STOVE, "phase": "turning-on"}) == 0 and mapping.target_state({**STOVE, "phase": "turning-on"}) == 1
    assert mapping.target_state({**STOVE, "running": False}) == 0


def test_empty_reading_does_not_report_zero_degrees():
    assert mapping.current_temperature({"ambient": 0.0}) == 20.0
    assert mapping.target_temperature({"setpoint": 0.0}) == 20.0
    assert mapping.current_temperature(STOVE) == 23.8


@pytest.mark.parametrize("hk,expected", [(0, False), (1, True), (3, True)])
def test_power_command(hk, expected):
    assert mapping.power_command(hk) is expected


@pytest.mark.parametrize("raw,expected", [(21.2, 21.0), (21.3, 21.5), (3.0, 5.0), (45.0, 40.0)])
def test_setpoint_rounds_to_half_degrees_within_limits(raw, expected):
    assert mapping.setpoint_command(raw) == expected


def test_bridge_client_sends_token_and_commands():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path, request.headers.get("authorization"), request.content))
        if request.method == "GET":
            return httpx.Response(200, json={"stoves": [STOVE]})
        return httpx.Response(200, json={"outcome": "confirmed", "attempts": 1})

    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://x", headers={"Authorization": "Bearer t"})
    bridge = Bridge("https://x", "t", client)
    assert bridge.stove()["running"] is True
    assert bridge.command("s§1", "power", False)["outcome"] == "confirmed"
    assert json.loads(seen[1][3]) == {"kind": "power", "value": False}
    assert seen[1][2] == "Bearer t"


def test_accessory_exposes_a_thermostat_and_follows_the_stove():
    from pyhap.accessory_driver import AccessoryDriver
    from app.accessory import StoveThermostat

    loop = asyncio.new_event_loop()
    driver = AccessoryDriver(port=51999, persist_file=os.path.join(tempfile.mkdtemp(), "state"), loop=loop, pincode=b"273-91-648")
    accessory = StoveThermostat(driver, bridge=None, name="Poêle")
    accessory.apply(STOVE)
    assert accessory.char_current_temp.value == 23.8
    assert accessory.char_target_temp.value == 21.0
    assert accessory.char_target_state.value == 1
    assert accessory.stove_id == "s§1"
    loop.close()
