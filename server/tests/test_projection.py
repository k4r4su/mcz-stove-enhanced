"""Prévision de la température de la pièce : quand passera-t-elle sous le seuil, et quand rallumer ?"""
import math

import pytest

from app import projection
from app.projection import State

NOW = 1_800_000_000.0 - 1_800_000_000.0 % 3600     # pile sur une heure
HOURLY = {NOW + h * 3600: 5.0 for h in range(-2, 16)}
K, STARTUP, RATE = 0.03, 20.0, 0.04


def test_cooling_follows_the_learned_curve_after_the_stove_has_cooled():
    state = State(21.0, False, 21.0, None, NOW - 7200)              # éteint depuis 2 h
    out = projection.advise(NOW, state, [], HOURLY, K, STARTUP, RATE, threshold=19.0)
    hours = math.log((21 - 5) / (19 - 5)) / K                        # Newton : 21 → 19 °C avec 5 °C dehors
    assert out["cross_ts"] == pytest.approx(NOW + hours * 3600, abs=projection.STEP)
    assert out["relight_ts"] == pytest.approx(out["cross_ts"] - STARTUP * 60)
    assert out["next_on"] is None and out["on_time"] is False and out["learned"] is True


def test_residual_heat_holds_the_room_for_an_hour_after_switching_off():
    state = State(21.0, False, 21.0, None, NOW)                      # vient de s'éteindre
    out = projection.advise(NOW, state, [], HOURLY, K, STARTUP, RATE, threshold=19.0)
    at_45min = next(p for p in out["free"] if p["ts"] >= NOW + 2700)
    assert at_45min["temp"] == 21.0


def test_planned_ignition_in_time_and_heating_to_setpoint():
    state = State(20.5, False, 21.0, None, NOW - 7200)
    on = {"ts": NOW + 3 * 3600, "kind": "on", "setpoint": 21.5, "label": "Allumage"}
    out = projection.advise(NOW, state, [on], HOURLY, K, STARTUP, RATE, threshold=19.0)
    assert out["on_time"] is True and out["cross_ts"] is not None
    assert out["next_on"]["temp_then"] < 20.5
    assert out["next_on"]["reached_ts"] > on["ts"] + STARTUP * 60
    assert max(p["temp"] for p in out["plan"]) == pytest.approx(21.5)


def test_late_ignition_and_unknown_cooling_uses_defaults():
    state = State(19.5, False, 21.0, None, NOW - 7200)
    on = {"ts": NOW + 10 * 3600, "kind": "on", "setpoint": 21, "label": "Allumage"}
    out = projection.advise(NOW, state, [on], {}, None, STARTUP, RATE, threshold=19.0)
    assert out["learned"] is False and out["on_time"] is False and out["relight_ts"] < on["ts"]


def test_stove_on_stays_at_setpoint_until_planned_off():
    state = State(21.0, True, 21.0, NOW - 3600, None)
    off = {"ts": NOW + 2 * 3600, "kind": "off", "label": "Extinction"}
    out = projection.advise(NOW, state, [off], HOURLY, K, STARTUP, RATE, threshold=19.0)
    before = [p for p in out["plan"] if p["ts"] < off["ts"]]
    assert all(p["temp"] == 21.0 and p["lit"] for p in before)
    assert out["cross_ts"] > off["ts"] + projection.RESIDUAL


@pytest.mark.asyncio
async def test_projection_api():
    from fastapi.testclient import TestClient
    from app.main import create_app
    from app.planner import Planner
    from app.weather import WeatherError
    from test_commands import FakeStove, SETTINGS, make
    from test_planning import TZ

    class NoWeather:
        async def hourly_temps(self, days=31):
            raise WeatherError("hors ligne")

    stove = FakeStove()
    svc = make(stove)
    await svc.refresh_once()
    api = TestClient(create_app(SETTINGS, svc, Planner(":memory:", TZ), weather=NoWeather()))
    sid = next(iter(svc.latest))
    h = {"Authorization": "Bearer secret"}
    assert api.get(f"/api/stoves/{sid}/projection").status_code == 401
    assert api.get(f"/api/stoves/{sid}/projection?threshold=40", headers=h).status_code == 422
    body = api.get(f"/api/stoves/{sid}/projection?threshold=19", headers=h).json()
    assert len(body["plan"]) == len(body["free"]) == 12 * 3600 // projection.STEP + 1
    assert body["weather_error"] == "hors ligne" and body["learned"] is False


def test_when_the_room_reaches_the_setpoint():
    lit = State(19.0, True, 21.0, NOW - 3600, None)                  # allumé, déjà en chauffe
    out = projection.advise(NOW, lit, [], HOURLY, K, STARTUP, RATE, threshold=19.0)
    assert out["target"] == 21.0 and not out["reached_now"]
    assert out["reach_ts"] == pytest.approx(NOW + (2 - 0.3) / RATE * 60, abs=2 * projection.STEP)
    warm = projection.advise(NOW, State(21.0, True, 21.0, NOW - 3600, None), [], HOURLY, K, STARTUP, RATE, threshold=19.0)
    assert warm["reached_now"] and warm["reach_ts"] is None
    off = State(20.0, False, 20.0, None, NOW - 7200)                 # éteint, rallumage conseillé : quand fera-t-il 21 °C ?
    late = {"ts": NOW + 11 * 3600, "kind": "on", "setpoint": 21.0, "label": "Allumage"}
    out = projection.advise(NOW, off, [late], HOURLY, K, STARTUP, RATE, threshold=19.0)
    assert out["target"] == 21.0 and not out["on_time"]
    assert out["relight_ts"] < out["relight_reach_ts"] < late["ts"]


def test_relight_reach_counts_from_the_relight_when_the_stove_is_on_now():
    on_now = State(21.0, True, 21.0, NOW - 7200, None)
    off = {"ts": NOW + 1800, "kind": "off", "label": "Extinction"}
    out = projection.advise(NOW, on_now, [off], HOURLY, K, STARTUP, RATE, threshold=20.0)
    assert out["relight_ts"] and out["relight_reach_ts"] > out["relight_ts"] + STARTUP * 60
