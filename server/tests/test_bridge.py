import time

import httpx
import pytest
from fastapi.testclient import TestClient

from app import metrics
from app.cloud import CloudError, MczCloud
from app.config import Settings
from app.history import History
from app.main import create_app
from app.service import StoveService
from app.snapshot import build_snapshot

SETTINGS = Settings(mcz_user="u", mcz_password="p", bridge_token="secret", poll_seconds=0.01, db_path=":memory:")
STATUS = {"IsConnected": True, "IsInError": False, "fase_op": "working", "power_enabled": True,
          "temp_amb_install": 22.4, "set_amb1": 21.0, "potenza_att": 3, "set_pot_man": 2,
          "sens_liv_pellet": False, "ore_prox_manut": 1868}
DETAIL = {"temp_fumi": 118.5}


def fake_cloud(login_status=200, calls=None):
    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append((request.method, request.url.path))
        path = request.url.path
        if path.endswith("/Authorization/Login"):
            if login_status != 200:
                return httpx.Response(login_status, json={"Message": "bad"})
            return httpx.Response(200, json={"Token": "tok", "ExpirationDateTime": "2099-01-01T00:00:00Z"})
        if request.headers.get("authorization") != "Bearer tok":
            return httpx.Response(401)
        if path.endswith("/FirstVisibleObjectsPaginated"):
            return httpx.Response(200, json={"Items": [{"Node": {"Id": "stove-1", "ModelId": "m"}}]})
        if path.endswith("/Status"):
            return httpx.Response(200, json=STATUS)
        if path.endswith("/State"):
            return httpx.Response(200, json=DETAIL)
        return httpx.Response(404)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url=SETTINGS.base_url)
    return MczCloud(SETTINGS, client)


def test_build_snapshot_maps_fields():
    s = build_snapshot("x", STATUS, DETAIL, 100.0)
    assert (s.ambient, s.setpoint, s.power_level, s.running, s.fume_temp) == (22.4, 21.0, 3, True, 118.5)


@pytest.mark.asyncio
async def test_refresh_populates_cache_and_history():
    svc = StoveService(fake_cloud(), History(":memory:"), 1)
    await svc.refresh_once()
    snap = svc.latest["stove-1"]
    assert snap.phase == "working" and snap.pellet_low is False
    assert len(svc.history.since("stove-1", 0)) == 1


@pytest.mark.asyncio
async def test_login_refused_raises_without_leaking_credentials():
    cloud = fake_cloud(login_status=403)
    with pytest.raises(CloudError) as err:
        await cloud.list_stoves()
    assert "403" in str(err.value) and "p" != str(err.value)


@pytest.mark.asyncio
async def test_expired_token_triggers_single_relogin():
    calls = []
    cloud = fake_cloud(calls=calls)
    await cloud.list_stoves()
    cloud._token = "stale"  # le cloud répond 401 → une reconnexion, puis succès
    await cloud.status("stove-1")
    assert [c for c in calls if c[1].endswith("/Authorization/Login")].__len__() == 2


def test_history_skips_unchanged_samples_within_interval():
    h = History(":memory:")
    s = build_snapshot("x", STATUS, DETAIL, 1000.0)
    assert h.record(s) is True
    assert h.record(build_snapshot("x", STATUS, DETAIL, 1010.0)) is False
    assert h.record(build_snapshot("x", STATUS, DETAIL, 1100.0)) is True


def test_metrics_text():
    text = metrics.render([build_snapshot("stove-1", STATUS, DETAIL, 1.0)])
    assert 'mcz_ambient_celsius{stove="stove-1"} 22.4' in text
    assert 'mcz_running{stove="stove-1"} 1.0' in text


@pytest.mark.asyncio
async def test_api_requires_token_and_serves_cache():
    svc = StoveService(fake_cloud(), History(":memory:"), 1)
    await svc.refresh_once()
    client = TestClient(create_app(SETTINGS, svc))
    assert client.get("/api/stoves").status_code == 401
    assert client.get("/metrics").status_code == 401
    ok = client.get("/api/stoves", headers={"Authorization": "Bearer secret"})
    assert ok.status_code == 200
    assert ok.json()["stoves"][0]["setpoint"] == 21.0
    hist = client.get("/api/stoves/stove-1/history?hours=1", headers={"Authorization": "Bearer secret"})
    assert hist.status_code == 200 and len(hist.json()["samples"]) == 1
    assert client.get("/api/stoves/nope/history", headers={"Authorization": "Bearer secret"}).status_code == 404
    assert client.get("/health").json()["ok"] is True


def test_password_can_be_given_in_base64(monkeypatch):
    import base64
    monkeypatch.setenv("MCZ_USER", "u")
    monkeypatch.setenv("BRIDGE_TOKEN", "t")
    monkeypatch.delenv("MCZ_PASSWORD", raising=False)
    monkeypatch.setenv("MCZ_PASSWORD_B64", base64.b64encode("pa$$w0rd$x".encode()).decode())
    assert Settings.from_env().mcz_password == "pa$$w0rd$x"


@pytest.mark.asyncio
async def test_empty_status_triggers_one_ping_then_rereads():
    state = {"pinged": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/Authorization/Login"):
            return httpx.Response(200, json={"Token": "tok", "ExpirationDateTime": "2099-01-01T00:00:00Z"})
        if path.endswith("/FirstVisibleObjectsPaginated"):
            return httpx.Response(200, json={"Items": [{"Node": {"Id": "s1", "ModelId": "m"}}]})
        if "/Program/Ping/" in path:
            state["pinged"] += 1
            return httpx.Response(200, json={})
        if path.endswith("/Status"):
            if state["pinged"]:
                return httpx.Response(200, json=STATUS)
            return httpx.Response(200, json={"IsConnected": False})  # module Wi-Fi endormi
        return httpx.Response(200, json={})

    cloud = MczCloud(SETTINGS, httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url=SETTINGS.base_url))
    svc = StoveService(cloud, History(":memory:"), 1, ping_wait=0)
    await svc.refresh_once()
    assert state["pinged"] == 1
    assert svc.latest["s1"].ambient == 22.4 and svc.latest["s1"].connected is True
    await svc.refresh_once()  # données présentes : pas de nouveau ping
    assert state["pinged"] == 1


@pytest.mark.asyncio
async def test_empty_status_is_not_recorded_in_history():
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/Authorization/Login"):
            return httpx.Response(200, json={"Token": "tok", "ExpirationDateTime": "2099-01-01T00:00:00Z"})
        if path.endswith("/FirstVisibleObjectsPaginated"):
            return httpx.Response(200, json={"Items": [{"Node": {"Id": "s1", "ModelId": "m"}}]})
        if path.endswith("/Status"):
            return httpx.Response(200, json={"IsConnected": False, "temp_amb_install": 0})
        return httpx.Response(200, json={})

    cloud = MczCloud(SETTINGS, httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url=SETTINGS.base_url))
    svc = StoveService(cloud, History(":memory:"), 1, ping_wait=0)
    await svc.refresh_once()
    assert svc.latest["s1"].connected is False       # l'app voit « non connecté »
    assert svc.history.since("s1", 0) == []          # mais l'historique reste propre


def test_web_app_is_served_without_token_but_data_still_requires_it():
    svc = StoveService(fake_cloud(), History(":memory:"), 1)
    client = TestClient(create_app(SETTINGS, svc))
    page = client.get("/")
    assert page.status_code == 200 and "Poêle" in page.text and "/api/stoves" in page.text
    assert client.get("/manifest.webmanifest").status_code == 200
    assert client.get("/icon.svg").headers["content-type"].startswith("image/svg+xml")
    assert client.get("/api/stoves").status_code == 401


def test_web_login_opens_a_session_and_blocks_brute_force():
    from dataclasses import replace
    settings = replace(SETTINGS, web_user="alice", web_password="pa$$word")
    svc = StoveService(fake_cloud(), History(":memory:"), 1)
    client = TestClient(create_app(settings, svc))
    assert client.get("/api/login").json() == {"enabled": True}
    assert client.post("/api/login", json={"username": "alice", "password": "faux"}).status_code == 401
    ok = client.post("/api/login", json={"username": "alice", "password": "pa$$word"})
    assert ok.status_code == 200
    session = {"Authorization": f"Bearer {ok.json()['token']}"}
    assert client.get("/api/stoves", headers=session).status_code == 200
    assert client.get("/api/stoves", headers={"Authorization": "Bearer secret"}).status_code == 200      # l'app iPhone garde son jeton
    client.post("/api/logout", headers=session)
    assert client.get("/api/stoves", headers=session).status_code == 401
    for _ in range(5):
        client.post("/api/login", json={"username": "x", "password": "y"})
    assert client.post("/api/login", json={"username": "alice", "password": "pa$$word"}).status_code == 429


def test_login_disabled_without_credentials():
    client = TestClient(create_app(SETTINGS, StoveService(fake_cloud(), History(":memory:"), 1)))
    assert client.get("/api/login").json() == {"enabled": False}
    assert client.post("/api/login", json={"username": "a", "password": "b"}).status_code == 404
