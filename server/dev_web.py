"""Serveur de démonstration LOCAL pour la web app : faux poêle, planning et météo d'exemple (jeton « dev »).

    .venv/bin/python dev_web.py      puis http://localhost:8765/?token=dev
N'est pas copié dans l'image Docker.
"""
import asyncio
import json
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import uvicorn

sys.path.insert(0, str(Path(__file__).parent / "tests"))
from app.cloud import MczCloud
from app.config import Settings
from app.history import History
from app.main import create_app
from app.planner import Planner
from app.service import StoveService
from app.snapshot import build_snapshot
from app.weather import Weather
from test_commands import FakeStove
from test_weather import payload

TZ = ZoneInfo("Europe/Paris")
settings = Settings(mcz_user="u", mcz_password="p", bridge_token="dev", db_path=":memory:")
stove = FakeStove()
stove.status.update(crono_enabled=True, temp_amb_install=23.8, ore_prox_manut=2940)
cloud = MczCloud(settings, httpx.AsyncClient(transport=httpx.MockTransport(stove.handler), base_url=settings.base_url))
svc = StoveService(cloud, History(":memory:"), 3, confirm_wait=1, confirm_checks=2)
planner = Planner(":memory:", TZ)
planner.add_plage("Matin", [0, 1, 2, 3, 4], "06:35", "09:25", setpoint=21)
planner.add_plage("Soirée", list(range(7)), "17:15", "22:30", setpoint=21.5)
planner.add_refill("t§0§stove", time.time() - 6 * 3600, kg=15)


def meteo_handler(request):
    data = payload()
    now = datetime.now(TZ)
    times, temps, codes = [], [], []
    for i in range(9):
        d = (now + timedelta(days=i)).date().isoformat()
        for h in range(24):
            times.append(f"{d}T{h:02d}:00"); temps.append(6 + i + 8 * (1 if 10 < h < 19 else 0)); codes.append([0, 2, 3, 61][(i + h) % 4])
    data["hourly"] = {"time": times, "temperature_2m": temps, "apparent_temperature": temps, "weather_code": codes}
    data["daily"] = {"time": [t[:10] for t in times[::24]], "temperature_2m_min": [5] * 9, "temperature_2m_max": [16] * 9}
    return httpx.Response(200, json=data)


async def no_lookup():
    return 47.6, 7.3

weather = Weather(TZ, None, None, no_lookup, httpx.AsyncClient(transport=httpx.MockTransport(meteo_handler)))


async def chrono(_):
    return [{"WeekDay": d, "Rows": [{"StartTime": "07:00:00", "EndTime": "09:30:00", "ComfortTemperature": 21}]} for d in range(1, 7)]
cloud.chrono = chrono


async def seed():
    await svc.refresh_once()
    now = time.time()
    for i in range(0, 24 * 60, 10):     # 24 h d'historique d'exemple
        st = dict(stove.status, temp_amb_install=20 + 2.5 * __import__("math").sin(i / 180), power_enabled=(i // 60) % 8 < 5)
        svc.history.record(build_snapshot("t§0§stove", st, {"temp_fumi": 120}, now - 86400 + i * 60), min_interval=0)

app = create_app(settings, svc, planner, weather=weather)
asyncio.run(seed())
uvicorn.run(app, host="127.0.0.1", port=8765, log_level="warning")
