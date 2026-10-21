import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app import commands
from app.cloud import CloudError, MczCloud
from app.config import Settings
from app.history import History
from app.main import create_app
from app.service import StoveService

SETTINGS = Settings(mcz_user="u", mcz_password="p", bridge_token="secret", poll_seconds=1, db_path=":memory:")
NODE = {"Id": "t§0§stove", "ModelId": "t§0§model", "SensorSetTypeId": "t§0§type"}


class FakeStove:
    """Poêle simulé : ignore les `lost` premières commandes (cas réel : commande perdue)."""

    def __init__(self, lost=0):
        self.lost = lost
        self.sent = []
        self.status = {"IsConnected": True, "fase_op": "on", "power_enabled": True, "set_amb1": 21, "set_pot_man": 1,
                       "temp_amb_install": 23.3, "mod_lav_att": "comfort", "ScheduledTurnOffTimestamp": 0}

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/Authorization/Login"):
            return httpx.Response(200, json={"Token": "tok", "ExpirationDateTime": "2099-01-01T00:00:00Z"})
        if path.endswith("/FirstVisibleObjectsPaginated"):
            return httpx.Response(200, json={"Items": [{"Node": NODE}]})
        if path.endswith("/Status"):
            return httpx.Response(200, json=self.status)
        if path.endswith("/State"):
            return httpx.Response(200, json={})
        if "ActivateProgram" in path:
            body = json.loads(request.content)
            self.sent.append(body)
            if self.lost > 0:
                self.lost -= 1
            else:
                sensor, value = body["Commands"][0]["SensorId"], body["Commands"][0]["Value"]
                key = {"9c7f0959-47a2-44ea-906d-f5d22218d00e": "power_enabled", "b866d2a9-d0ab-4558-8367-910c52261be1": "set_amb1",
                       "f7750f3e-9ff2-4351-b53a-6d1a9d1f6bc6": "set_pot_man"}[sensor]
                if sensor == "9c7f0959-47a2-44ea-906d-f5d22218d00e":
                    if body["ConfigurationId"].startswith("e373bb67") and value is True:   # bouton marche/arrêt : bascule
                        on = self.status["fase_op"] in {"off", "turning-off"}
                        self.status.update(power_enabled=on, fase_op="turning-on" if on else "turning-off")
                    return httpx.Response(200, json={"Result": "Command successfully activated"})
                self.status[key] = value
            return httpx.Response(200, json={"Result": "Command successfully activated"})
        return httpx.Response(404)


def make(stove: FakeStove) -> StoveService:
    client = httpx.AsyncClient(transport=httpx.MockTransport(stove.handler), base_url=SETTINGS.base_url)
    return StoveService(MczCloud(SETTINGS, client), History(":memory:"), 1, confirm_wait=0, confirm_checks=2, max_attempts=3)


@pytest.mark.asyncio
async def test_setpoint_confirmed_first_try():
    stove = FakeStove()
    svc = make(stove)
    await svc.refresh_once()
    result = await svc.send_command(NODE["Id"], "setpoint", 22.5)
    assert result["outcome"] == "confirmed" and result["attempts"] == 1
    assert result["snapshot"]["setpoint"] == 22.5
    body = stove.sent[0]
    assert body["ConfigurationId"] == "875fb696-4af1-42da-bb78-ebe57c90fe5c"
    assert body["ModelId"] == NODE["ModelId"] and body["Commands"] == [{"SensorId": "b866d2a9-d0ab-4558-8367-910c52261be1", "Value": 22.5}]


@pytest.mark.asyncio
async def test_lost_command_is_resent_until_confirmed():
    stove = FakeStove(lost=2)
    svc = make(stove)
    await svc.refresh_once()
    result = await svc.send_command(NODE["Id"], "setpoint", 22.0)
    assert result["outcome"] == "confirmed" and result["attempts"] == 3 and len(stove.sent) == 3
    assert result["snapshot"]["setpoint"] == 22.0


@pytest.mark.asyncio
async def test_power_toggle_is_sent_once_and_never_resent():
    stove = FakeStove(lost=99)
    svc = make(stove)
    svc.toggle_checks = 3
    await svc.refresh_once()
    result = await svc.send_command(NODE["Id"], "power", False)
    assert result["outcome"] == "not_confirmed" and len(stove.sent) == 1      # une seule bascule, sinon on rallumerait


@pytest.mark.asyncio
async def test_power_is_not_sent_when_already_in_that_state():
    stove = FakeStove()
    svc = make(stove)
    await svc.refresh_once()
    result = await svc.send_command(NODE["Id"], "power", True)              # déjà allumé
    assert result["outcome"] == "confirmed" and result["attempts"] == 0 and stove.sent == []
    stove.status.update(fase_op="turning-off", power_enabled=True)
    result = await svc.send_command(NODE["Id"], "power", False)             # déjà en train de s'éteindre
    assert result["attempts"] == 0 and stove.sent == []


@pytest.mark.asyncio
async def test_power_is_not_decided_on_an_unreadable_state():
    """Module endormi / cloud sans données : l'état vide passerait pour « éteint ». Ni « déjà éteint », ni bascule à l'aveugle."""
    stove = FakeStove()
    svc = make(stove)
    await svc.refresh_once()
    stove.status = {"IsConnected": False}
    for value in (False, True):
        with pytest.raises(CloudError):
            await svc.send_command(NODE["Id"], "power", value)
    assert stove.sent == []


@pytest.mark.asyncio
async def test_unreadable_state_does_not_confirm_an_extinction():
    class Flaky(FakeStove):
        def handler(self, request):
            if request.url.path.endswith("/Status") and self.sent:
                return httpx.Response(200, json={"IsConnected": False})    # relectures vides après l'appui
            return super().handler(request)
    stove = Flaky(lost=99)                                                  # la bascule se perd, le poêle reste allumé
    svc = make(stove)
    svc.toggle_checks = 3
    await svc.refresh_once()
    result = await svc.send_command(NODE["Id"], "power", False)
    assert result["outcome"] == "not_confirmed" and len(stove.sent) == 1


@pytest.mark.asyncio
async def test_power_on_after_off_uses_the_same_button():
    stove = FakeStove()
    stove.status.update(power_enabled=False, fase_op="off")
    svc = make(stove)
    await svc.refresh_once()
    result = await svc.send_command(NODE["Id"], "power", True)
    assert result["outcome"] == "confirmed" and stove.status["fase_op"] == "turning-on"
    assert stove.sent[0]["Commands"] == [{"SensorId": "9c7f0959-47a2-44ea-906d-f5d22218d00e", "Value": True}]


@pytest.mark.asyncio
async def test_power_on_when_off_even_if_power_flag_says_true():
    stove = FakeStove()
    stove.status.update(power_enabled=True, fase_op="off")                 # relevé réel du 14/10 à 06:35
    svc = make(stove)
    await svc.refresh_once()
    result = await svc.send_command(NODE["Id"], "power", True)
    assert result["outcome"] == "confirmed" and result["attempts"] == 1 and len(stove.sent) == 1
    assert stove.status["fase_op"] == "turning-on"


@pytest.mark.asyncio
async def test_command_never_taken_reports_not_confirmed():
    stove = FakeStove(lost=99)
    svc = make(stove)
    await svc.refresh_once()
    result = await svc.send_command(NODE["Id"], "setpoint", 23.0)
    assert result["outcome"] == "not_confirmed" and len(stove.sent) == 3


@pytest.mark.parametrize("kind,value", [("setpoint", 4.5), ("setpoint", 21.3), ("setpoint", "21"), ("power_level", 0), ("power_level", 6),
                                        ("power", 1), ("mode", "turbo"), ("sleep_timer", 481), ("factory_reset", True)])
def test_invalid_commands_rejected(kind, value):
    with pytest.raises(ValueError):
        commands.build(kind, value)


@pytest.mark.asyncio
async def test_command_api_validates_and_requires_token():
    stove = FakeStove()
    svc = make(stove)
    await svc.refresh_once()
    api = TestClient(create_app(SETTINGS, svc))
    url = "/api/stoves/" + "t%C2%A70%C2%A7stove" + "/commands"
    assert api.post(url, json={"kind": "setpoint", "value": 22}).status_code == 401
    bad = api.post(url, json={"kind": "setpoint", "value": 99}, headers={"Authorization": "Bearer secret"})
    assert bad.status_code == 422 and stove.sent == []
    unknown = api.post("/api/stoves/nope/commands", json={"kind": "setpoint", "value": 22}, headers={"Authorization": "Bearer secret"})
    assert unknown.status_code == 404


@pytest.mark.asyncio
async def test_power_off_is_confirmed_by_the_shutdown_phase_even_if_power_flag_lags():
    class Lagging(FakeStove):
        def handler(self, request):
            response = super().handler(request)
            if "ActivateProgram" in request.url.path:
                self.status.update(power_enabled=True, fase_op="turning-off")   # le drapeau tarde, la phase a déjà changé
            return response

    stove = Lagging()
    svc = make(stove)
    await svc.refresh_once()
    result = await svc.send_command(NODE["Id"], "power", False)
    assert result["outcome"] == "confirmed" and result["attempts"] == 1


@pytest.mark.asyncio
async def test_power_uses_the_official_apps_configuration_with_a_boolean():
    stove = FakeStove()
    svc = make(stove)
    await svc.refresh_once()
    await svc.send_command(NODE["Id"], "power", False)
    body = stove.sent[0]
    assert body["ConfigurationId"] == "e373bb67-fe69-4d49-b2f2-4b636fa68e37"          # Spegnimento
    assert body["Commands"] == [{"SensorId": "9c7f0959-47a2-44ea-906d-f5d22218d00e", "Value": True}]   # « éteins » = true
    assert type(body["Commands"][0]["Value"]) is bool     # le cloud refuse 0/1 : « is not a valid boolean »


@pytest.mark.asyncio
async def test_background_command_returns_at_once_and_reports_outcome():
    import asyncio
    stove = FakeStove()
    svc = make(stove)
    await svc.refresh_once()
    job = svc.start_command(NODE["Id"], "power", False)
    assert job["status"] == "pending"
    for _ in range(50):
        await asyncio.sleep(0)
        if svc.jobs[job["id"]]["status"] != "pending":
            break
    assert svc.jobs[job["id"]]["status"] == "confirmed"
    assert svc.jobs[job["id"]]["snapshot"]["running"] is False
    with pytest.raises(ValueError):
        svc.start_command(NODE["Id"], "setpoint", 99)
    with pytest.raises(KeyError):
        svc.start_command("nope", "power", False)
