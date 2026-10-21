from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
import pytest
from fastapi.testclient import TestClient

from app import weather
from app.main import create_app
from app.planner import Planner
from app.weather import Weather, WeatherError
from test_commands import SETTINGS, FakeStove, make

TZ = ZoneInfo("Europe/Paris")
NOW = datetime(2026, 10, 13, 20, 0, tzinfo=TZ)


def payload():
    times, temps, codes = [], [], []
    for d in ("2026-10-13", "2026-10-14", "2026-10-15"):
        for h in range(24):
            times.append(f"{d}T{h:02d}:00")
            temps.append({"2026-10-13": 12.0, "2026-10-14": 4.0 + (h / 2), "2026-10-15": 9.0}[d])
            codes.append(61)
    return {"hourly": {"time": times, "temperature_2m": temps, "apparent_temperature": [t - 2 for t in temps], "weather_code": codes},
            "daily": {"time": ["2026-10-13", "2026-10-14", "2026-10-15"], "temperature_2m_min": [10, 4, 8], "temperature_2m_max": [14, 15.5, 11]}}


def test_parse_gives_today_and_tomorrow_at_the_three_slots():
    days = weather.parse(payload(), TZ, NOW)
    tomorrow = days[1]
    assert tomorrow["label"] == "Demain" and tomorrow["date"] == "2026-10-14"
    assert tomorrow["full_label"] == "Mercredi 14 octobre"
    assert [d["label"] for d in days] == ["Aujourd'hui", "Demain", "Jeudi 15 octobre"]   # jours suivants datés (données s'arrêtant au 15)
    assert [(s["hour"], s["temp"]) for s in tomorrow["slots"]] == [(7, 7.5), (12, 10.0), (18, 13.0)]
    assert tomorrow["min"] == 4 and tomorrow["max"] == 15.5
    assert tomorrow["advice"] == "Chauffage utile toute la journée"


@pytest.mark.parametrize("temps,text", [([5, 8, 9], "Chauffage utile toute la journée"), ([18, 20, 19], "Pas de chauffage nécessaire"),
                                        ([6, 18, 8], "Chauffage utile le matin et le soir"), ([6, 17, 17], "Chauffage utile surtout le matin"),
                                        ([17, 17, 9], "Chauffage utile surtout le soir")])
def test_advice(temps, text):
    assert weather.advice(temps) == text


@pytest.mark.asyncio
async def test_forecast_uses_configured_position_and_caches():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(dict(request.url.params))
        return httpx.Response(200, json=payload())

    async def no_lookup():
        raise AssertionError("la position configurée doit suffire")

    clock = {"t": NOW.timestamp()}
    meteo = Weather(TZ, 48.5, 7.5, no_lookup, httpx.AsyncClient(transport=httpx.MockTransport(handler)), clock=lambda: clock["t"])
    first = await meteo.forecast()
    await meteo.forecast()
    assert len(calls) == 1 and calls[0]["latitude"] == "48.5000" and first["source"].startswith("configurée")
    clock["t"] += 3600
    await meteo.forecast()
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_position_comes_from_the_stove_sheet_otherwise():
    async def lookup():
        return 47.1, 7.2

    meteo = Weather(TZ, None, None, lookup, httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=payload()))),
                    clock=lambda: NOW.timestamp())
    assert (await meteo.forecast())["source"].startswith("fiche du poêle")


@pytest.mark.asyncio
async def test_weather_api_reports_missing_position_clearly():
    async def lookup():
        raise RuntimeError("absente")

    stove = FakeStove()
    svc = make(stove)
    await svc.refresh_once()
    meteo = Weather(TZ, None, None, lookup)
    api = TestClient(create_app(SETTINGS, svc, Planner(":memory:", TZ), weather=meteo))
    resp = api.get("/api/weather", headers={"Authorization": "Bearer secret"})
    assert resp.status_code == 503 and "WEATHER_LAT" in resp.json()["detail"]
    assert api.get("/api/weather").status_code == 401


@pytest.mark.asyncio
async def test_position_chosen_in_the_app_wins_and_is_stored():
    stove = FakeStove()
    svc = make(stove)
    await svc.refresh_once()
    planner = Planner(":memory:", TZ)
    seen = []

    def handler(request):
        seen.append(dict(request.url.params))
        return httpx.Response(200, json=payload())

    async def lookup():
        return 41.9, 12.5          # « Italie » : ne doit plus servir

    meteo = Weather(TZ, None, None, lookup, httpx.AsyncClient(transport=httpx.MockTransport(handler)),
                    clock=lambda: NOW.timestamp(), saved_position=lambda: planner.get_setting("weather_location"))
    api = TestClient(create_app(SETTINGS, svc, planner, weather=meteo))
    h = {"Authorization": "Bearer secret"}
    assert api.put("/api/settings/weather-location", headers=h, json={"name": "Colmar", "lat": 48.0792, "lon": 7.3581}).status_code == 200
    body = api.get("/api/weather", headers=h).json()
    assert body["source"] == "choisie dans l'app : Colmar" and seen[-1]["latitude"] == "48.0790"
    assert api.get("/api/settings/weather-location", headers=h).json()["name"] == "Colmar"
    assert api.put("/api/settings/weather-location", headers=h, json={"lat": 120, "lon": 0}).status_code == 422
