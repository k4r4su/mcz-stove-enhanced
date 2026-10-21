from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from app import stats
from app.main import create_app
from app.planner import Planner
from app.scheduler import Scheduler
from test_commands import NODE, SETTINGS, FakeStove, make

TZ = ZoneInfo("Europe/Paris")
TUE = datetime(2026, 10, 13, tzinfo=TZ)   # mardi


def at(h, m=0, s=0, day=TUE):
    return day.replace(hour=h, minute=m, second=s)


def test_plage_starts_at_the_exact_minute_and_only_once():
    p = Planner(":memory:", TZ)
    p.add_plage("Matin", [0, 1, 2, 3, 4], "07:42", "09:30", setpoint=21.5)
    assert p.due(at(7, 41, 59)) == []
    [on] = p.due(at(7, 42, 5))
    assert on.steps == (("power", True), ("setpoint", 21.5))
    p.mark_done(on.key, 0, "ok")
    assert p.due(at(7, 43)) == []
    assert p.due(at(7, 53)) == []                      # hors délai de grâce, rien ne repart
    [off] = p.due(at(9, 30, 1))
    assert off.steps == (("power", False),)


def test_plage_skips_unselected_days_and_disabled():
    p = Planner(":memory:", TZ)
    p.add_plage("Week-end", [5, 6], "08:00", "10:00")
    p.add_plage("Coupée", [1], "08:00", "10:00", enabled=False)
    assert p.due(at(8, 0, 10)) == []                   # mardi


def test_plage_crossing_midnight_ends_next_day():
    p = Planner(":memory:", TZ)
    p.add_plage("Nuit", [0], "22:00", "06:00", mode="overnight")   # lundi soir → mardi matin
    monday = TUE - timedelta(days=1)
    [on] = p.due(at(22, 0, 3, day=monday))
    assert on.steps == (("power", True), ("mode", "overnight"))
    [off] = p.due(at(6, 0, 3))
    assert off.steps == (("power", False),)


def test_timer_takes_next_occurrence_and_replaces_previous():
    p = Planner(":memory:", TZ)
    t1 = p.add_timer("s", "off", at(21, 0), at_time="22:30")
    assert datetime.fromtimestamp(t1["at_ts"], TZ) == at(22, 30)
    t2 = p.add_timer("s", "off", at(23, 0), at_time="22:30")           # déjà passée aujourd'hui → demain
    assert datetime.fromtimestamp(t2["at_ts"], TZ) == at(22, 30) + timedelta(days=1)
    assert [t["id"] for t in p.pending_timers("s")] == [t2["id"]]       # la première est remplacée


def test_timer_missed_after_long_outage():
    p = Planner(":memory:", TZ)
    p.add_timer("s", "off", at(21, 0), at_time="22:30")
    assert p.due(at(23, 30)) == []
    assert p.pending_timers("s") == []


@pytest.mark.parametrize("bad", [dict(days=[], start="07:00", end="08:00"), dict(days=[7], start="07:00", end="08:00"),
                                 dict(days=[1], start="7h", end="08:00"), dict(days=[1], start="07:00", end="07:00"),
                                 dict(days=[1], start="07:00", end="08:00", setpoint=50)])
def test_invalid_plage_rejected(bad):
    with pytest.raises(ValueError):
        Planner(":memory:", TZ).add_plage("x", **bad)


@pytest.mark.asyncio
async def test_scheduler_turns_off_at_the_chosen_time_once():
    stove = FakeStove()
    svc = make(stove)
    await svc.refresh_once()
    planner = Planner(":memory:", TZ)
    planner.add_timer(NODE["Id"], "off", at(21, 0), at_time="22:30")
    sched = Scheduler(svc, planner)
    await sched.tick(at(22, 29))
    assert stove.sent == []
    await sched.tick(at(22, 30, 5))
    assert len(stove.sent) == 1 and stove.status["power_enabled"] is False
    assert planner.pending_timers() == []
    await sched.tick(at(22, 31))
    assert len(stove.sent) == 1


@pytest.mark.asyncio
async def test_scheduler_does_not_resend_power_when_already_on_but_applies_setpoint():
    stove = FakeStove()
    svc = make(stove)
    await svc.refresh_once()
    planner = Planner(":memory:", TZ)
    planner.add_plage("Matin", [1], "07:00", "09:00", setpoint=22.5)
    await Scheduler(svc, planner).tick(at(7, 0, 5))
    assert [c["Commands"][0]["Value"] for c in stove.sent] == [22.5]


@pytest.mark.asyncio
async def test_scheduler_never_starts_a_stove_in_alarm():
    stove = FakeStove()
    stove.status.update(power_enabled=False, IsInError=True)
    svc = make(stove)
    await svc.refresh_once()
    planner = Planner(":memory:", TZ)
    planner.add_plage("Matin", [1], "07:00", "09:00")
    await Scheduler(svc, planner).tick(at(7, 0, 5))
    assert stove.sent == []


@pytest.mark.asyncio
async def test_unconfirmed_off_is_retried_on_next_tick():
    stove = FakeStove(lost=1)           # premier tick : l'unique appui est perdu
    svc = make(stove)
    await svc.refresh_once()
    planner = Planner(":memory:", TZ)
    planner.add_timer(NODE["Id"], "off", at(21, 0), at_time="22:30")
    sched = Scheduler(svc, planner)
    await sched.tick(at(22, 30, 5))
    assert len(planner.pending_timers()) == 1       # pas confirmé : la minuterie reste
    await sched.tick(at(22, 30, 20))
    assert planner.pending_timers() == [] and stove.status["power_enabled"] is False


def sample(ts, running=True, level=3, ambient=22.0):
    return {"ts": ts, "phase": "on", "running": running, "ambient": ambient, "setpoint": 21, "power_level": level, "fume_temp": 100, "pellet_low": False}


def test_daily_stats_integrate_running_time_and_pellets():
    start = at(8).timestamp()
    samples = [sample(start + i * 60) for i in range(60)]     # une heure pile à la puissance 3
    day = stats.daily_stats(samples, TZ, 2, at(9, 0))[-1]
    assert day["run_hours"] == pytest.approx(1.0, abs=0.02)
    assert day["pellets_kg_est"] == pytest.approx(1.45 * 1.425, abs=0.05)   # fiche MCZ × 1,425
    assert day["hours_by_power"] == {"3": pytest.approx(1.0, abs=0.02)}


def test_pellet_level_from_refill_and_consumption():
    start = at(8).timestamp()
    samples = [sample(start + i * 60) for i in range(60)]
    out = stats.pellet_level([{"ts": start - 1, "kg": None, "full": True}], samples, 16.0, start + 3600, 3, True)
    assert out["known"] and out["remaining_kg"] == pytest.approx(16 - 1.45 * 1.425, abs=0.2)
    assert out["hours_left"] == pytest.approx((16 - 1.45 * 1.425) / (1.45 * 1.425), abs=0.3)
    assert stats.pellet_level([], samples, 16.0, start, 3, True)["known"] is False


@pytest.mark.asyncio
async def test_planning_api():
    stove = FakeStove()
    svc = make(stove)
    await svc.refresh_once()
    api = TestClient(create_app(SETTINGS, svc, Planner(":memory:", TZ)))
    h = {"Authorization": "Bearer secret"}
    sid = "t%C2%A70%C2%A7stove"
    assert api.get("/api/plages").status_code == 401
    ok = api.post("/api/plages", headers=h, json={"name": "Matin", "days": [0, 1], "start": "07:42", "end": "09:00", "setpoint": 21.5})
    assert ok.status_code == 200 and ok.json()["start"] == "07:42"
    assert api.post("/api/plages", headers=h, json={"name": "x", "days": [], "start": "07:00", "end": "08:00"}).status_code == 422
    assert len(api.get("/api/plages", headers=h).json()["plages"]) == 1
    assert api.delete("/api/plages/99", headers=h).status_code == 404
    timer = api.post(f"/api/stoves/{sid}/timers", headers=h, json={"action": "off", "time": "22:30"})
    assert timer.status_code == 200
    assert len(api.get(f"/api/stoves/{sid}/timers", headers=h).json()["timers"]) == 1
    assert api.delete(f"/api/timers/{timer.json()['id']}", headers=h).status_code == 200
    assert api.post(f"/api/stoves/{sid}/timers", headers=h, json={"action": "off", "time": "25:99"}).status_code == 422
    assert api.get(f"/api/stoves/{sid}/pellets", headers=h).json()["known"] is False
    assert api.post(f"/api/stoves/{sid}/pellets/refill", headers=h, json={"kg": 15}).status_code == 200
    assert api.get(f"/api/stoves/{sid}/pellets", headers=h).json()["known"] is True
    assert api.post(f"/api/stoves/{sid}/pellets/refill", headers=h, json={"kg": 99}).status_code == 422
    assert api.get(f"/api/stoves/{sid}/stats?days=3", headers=h).status_code == 200


def test_upcoming_lists_next_events_in_order_with_timers():
    p = Planner(":memory:", TZ)
    p.add_plage("Matin", [1, 2], "06:45", "08:30", setpoint=21.5)      # mardi, mercredi
    p.add_timer("s", "off", at(18, 0), at_time="22:30")
    events = p.upcoming(at(7, 0))                                       # mardi 07:00 (le démarrage de 06:45 est passé)
    assert [e["label"] for e in events][:3] == ["Extinction", "Extinction (minuterie)", "Allumage (21.5 °C)"]
    assert events[0]["ts"] < events[1]["ts"] < events[2]["ts"]


def test_recent_runs_record_label_and_old_databases_are_migrated(tmp_path):
    import sqlite3
    path = str(tmp_path / "old.sqlite")
    old = sqlite3.connect(path)
    old.executescript("CREATE TABLE runs (key TEXT PRIMARY KEY, ts REAL NOT NULL, outcome TEXT NOT NULL);")
    old.commit(); old.close()
    p = Planner(path, TZ)                                               # ajoute la colonne manquante
    p.mark_done("k", 100.0, "ok", "Matin : allumage")
    assert p.recent_runs() == [{"ts": 100.0, "outcome": "ok", "label": "Matin : allumage"}]


@pytest.mark.asyncio
async def test_overview_api_and_chrono_flag():
    stove = FakeStove()
    stove.status["crono_enabled"] = True
    svc = make(stove)
    await svc.refresh_once()
    planner = Planner(":memory:", TZ)
    planner.add_plage("Soir", list(range(7)), "17:15", "22:30")
    api = TestClient(create_app(SETTINGS, svc, planner))
    h = {"Authorization": "Bearer secret"}
    body = api.get("/api/schedule/overview", headers=h).json()
    assert len(body["upcoming"]) >= 2 and body["recent"] == []
    assert api.get("/api/stoves", headers=h).json()["stoves"][0]["chrono_enabled"] is True


def test_undo_last_refill():
    p = Planner(":memory:", TZ)
    assert p.undo_last_refill("s") is False
    p.add_refill("s", 100.0, kg=15)
    p.add_refill("s", 200.0, full=True)
    assert p.undo_last_refill("s") is True
    assert p.refills("s") == [{"ts": 100.0, "kg": 15.0, "full": False}]


def test_old_samples_marked_running_while_off_do_not_count_as_heating():
    """Avant le 14/10/2026, `running` venait de power_enabled (vrai poêle éteint) : la phase enregistrée fait foi."""
    from app.history import History
    from app.snapshot import build_snapshot
    h = History(":memory:")
    start = at(0).timestamp()
    for i in range(0, 8 * 60, 1):                                 # minuit → 8 h : éteint, mais power_enabled vrai
        h.record(build_snapshot("s", {"fase_op": "off", "temp_amb_install": 19.0}, None, start + i * 60), min_interval=60)
    h._db.execute("UPDATE samples SET running = 1")                # comme les relevés de l'ancienne version
    for i in range(8 * 60, 9 * 60):                                # 8 h → 9 h : allumé
        h.record(build_snapshot("s", {"fase_op": "on", "temp_amb_install": 20.0}, None, start + i * 60), min_interval=60)
    day = stats.daily_stats(h.since("s", start), TZ, 1, at(9, 0))[-1]
    assert day["run_hours"] == pytest.approx(1.0, abs=0.05)
