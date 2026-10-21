"""Indicateurs : coût, chauffe comparée à la météo, refroidissement de la maison, confort, fumées."""
import math
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from app import insights
from app.main import create_app
from app.planner import Planner
from app.weather import WeatherError
from test_commands import FakeStove, SETTINGS, make
from test_planning import TZ, TUE, at

H = {"Authorization": "Bearer secret"}


def s(ts, lit, ambient, setpoint=21.0, level=3, fume=None, phase=None):
    return {"ts": ts, "phase": phase or ("on" if lit else "off"), "running": lit, "ambient": ambient, "setpoint": setpoint,
            "power_level": level, "fume_temp": fume, "pellet_low": False}


def test_cooling_learns_how_fast_the_house_loses_heat_at_night():
    k, outdoor = 0.03, 5.0                                  # par heure
    start = at(22, 30).timestamp()                          # éteint à 21 h 30, mesuré dès 22 h 30
    samples = [s(at(21, 30).timestamp() - 60, True, 21.0)]
    samples += [s(at(21, 30).timestamp() + m * 60, False, 21.0) for m in range(0, 60)]
    for m in range(0, 8 * 60):                              # 22 h 30 → 6 h 30
        t = outdoor + (21.0 - outdoor) * math.exp(-k * m / 60)
        samples.append(s(start + m * 60, False, round(t, 1)))
    hourly = {start + h * 3600: outdoor for h in range(-2, 10)}
    out = insights.cooling(samples, hourly, TZ)
    assert out["nights"] == 1 and out["k"] == pytest.approx(k, rel=0.1)
    assert out["per_hour"] == pytest.approx((21 - (outdoor + 16 * math.exp(-k * 8))) / 8, abs=0.05)
    zero = next(r for r in out["table"] if r["outdoor"] == 0)
    assert zero["hours_21_to_18"] == pytest.approx(math.log(21 / 18) / k, rel=0.1)
    assert insights.cooling(samples, {}, TZ)["k"] is None   # sans météo : seulement les °C/h


def test_comfort_measures_warmup_and_how_well_the_setpoint_holds():
    t0 = at(6, 50).timestamp()
    samples = [s(t0 - 60, False, 18.0)]
    for m in range(0, 180):
        a = min(21.0, 18.0 + max(0, m - 20) * 0.05)        # 20 min de démarrage puis 3 °C/h
        samples.append(s(t0 + m * 60, True, round(a, 2)))
    out = insights.comfort(samples, t0 - 3600)
    assert out["ignitions"] == 1 and out["median_minutes"] == pytest.approx(74, abs=3)
    assert out["held_pct"] == 100
    assert out["recent"][0]["from"] == 18.0


def test_weather_compare_kg_per_degree_day_week_over_week():
    today = TUE.date()
    days = [{"date": (today - timedelta(days=i)).isoformat(), "pellets_kg_est": 9.0 if i <= 7 else 10.0, "coverage": 1.0,
             "dju": 10.0} for i in range(15)]
    out = insights.weather_compare(days, today)
    assert out["kg_per_dju"] == 0.9 and out["change_pct"] == -10
    assert "change_pct" not in insights.weather_compare(days[:5], today)        # pas encore deux semaines


def test_fumes_compare_same_power_level():
    today = TUE.date()
    samples = []
    for back in range(20, -1, -1):
        day = at(8) - timedelta(days=back)
        hotter = 12 if back <= 6 else 0
        samples += [s(day.timestamp() + m * 60, True, 20, level=3, fume=150 + hotter) for m in range(30)]
        samples += [s(day.timestamp() + 7200 + m * 60, True, 20, level=3, fume=50, phase="turning-on") for m in range(5)]
    out = insights.fumes(samples, TZ, today)
    assert out["change"] == 12 and out["days"][-1]["avg"] == 162


@pytest.mark.asyncio
async def test_insights_api_with_price_and_without_weather():
    stove = FakeStove()
    svc = make(stove)
    await svc.refresh_once()

    class NoWeather:
        async def hourly_temps(self, days=31):
            raise WeatherError("hors ligne")
    api = TestClient(create_app(SETTINGS, svc, Planner(":memory:", TZ), weather=NoWeather()))
    sid = next(iter(svc.latest))
    assert api.put("/api/settings/pellet-price", headers=H, json={"bag_eur": 0}).status_code == 422
    assert api.put("/api/settings/pellet-price", headers=H, json={"bag_eur": 6.43, "bag_kg": 15}).json() == {"bag_eur": 6.43, "bag_kg": 15}
    body = api.get(f"/api/stoves/{sid}/insights", headers=H).json()
    assert body["weather_error"] == "hors ligne" and body["price"]["bag_eur"] == 6.43
    assert set(body["cost"]) == {"week", "month", "season", "month_pace"} and len(body["days"]) == 30


@pytest.mark.asyncio
async def test_history_can_carry_the_outdoor_temperature_of_the_same_period():
    import time
    stove = FakeStove()
    svc = make(stove)
    await svc.refresh_once()
    hour = time.time() // 3600 * 3600

    class Hourly:
        async def hourly_temps(self, days=31):
            return {hour - 3 * 86400: 1.0, hour - 3 * 3600: 6.0, hour - 3600: 7.5, hour: 8.0}
    api = TestClient(create_app(SETTINGS, svc, Planner(":memory:", TZ), weather=Hourly()))
    sid = next(iter(svc.latest))
    assert "outdoor" not in api.get(f"/api/stoves/{sid}/history?hours=24", headers=H).json()    # sans demande, pas d'appel météo
    body = api.get(f"/api/stoves/{sid}/history?hours=24&outdoor=1", headers=H).json()
    assert body["outdoor"] == [[hour - 3 * 3600, 6.0], [hour - 3600, 7.5], [hour, 8.0]] and body["weather_error"] is None

    class NoWeather:
        async def hourly_temps(self, days=31):
            raise WeatherError("hors ligne")
    api = TestClient(create_app(SETTINGS, svc, Planner(":memory:", TZ), weather=NoWeather()))
    body = api.get(f"/api/stoves/{sid}/history?hours=24&outdoor=1", headers=H).json()
    assert body["outdoor"] == [] and body["weather_error"] == "hors ligne" and len(body["samples"]) == 1
