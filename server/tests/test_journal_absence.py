"""Journal « qui a fait quoi », jours sautés, pause / vacances, allumage anticipé."""
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from app import anticipation
from app.main import create_app
from app.planner import Planner
from app.scheduler import Scheduler
from test_commands import NODE, SETTINGS, FakeStove, make
from test_planning import TZ, TUE, at

H = {"Authorization": "Bearer secret"}


def off_stove() -> FakeStove:
    stove = FakeStove()
    stove.status.update(fase_op="off", power_enabled=True)   # éteint (power_enabled reste vrai, comme le vrai poêle)
    return stove


# ---- Journal ----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_journal_records_our_commands_with_their_outcome():
    stove = off_stove()
    svc = make(stove)
    await svc.refresh_once()
    planner = Planner(":memory:", TZ)
    planner.add_plage("Matin", [1], "07:00", "09:00", setpoint=21.5)
    await Scheduler(svc, planner).tick(at(7, 0, 5))
    events = svc.journal.recent()
    assert [(e["origin"], e["label"], e["outcome"]) for e in reversed(events)] == [
        ("planning", "Allumage", "ok"), ("planning", "Consigne 21,5 °C", "ok")]
    assert events[-1]["detail"] == "plage « Matin »"
    await Scheduler(svc, planner).tick(at(9, 0, 5))       # extinction
    await svc.send_command(NODE["Id"], "power", False)    # déjà éteint : rien envoyé
    job = svc.track("power", False, "app")
    svc.finish_job(job, NODE["Id"], {"outcome": "confirmed", "attempts": 0, "snapshot": None})
    assert svc.journal.recent()[0]["outcome"] == "noop" and "rien envoyé" in svc.journal.recent()[0]["detail"]


@pytest.mark.asyncio
async def test_journal_spots_changes_made_elsewhere_but_not_ours():
    stove = off_stove()
    svc = make(stove)
    await svc.refresh_once()
    stove.status.update(fase_op="turning-on")              # quelqu'un allume avec Maestro ou le bouton
    await svc.refresh_once()
    stove.status.update(set_amb1=19.0)                     # consigne changée ailleurs
    await svc.refresh_once()
    labels = [(e["origin"], e["label"]) for e in reversed(svc.journal.recent())]
    assert labels == [("externe", "Allumage"), ("externe", "Consigne 19,0 °C")]
    assert "fase_op=turning-on" in svc.journal.recent()[-1]["detail"]   # état brut noté pour comprendre d'où ça vient
    await svc.send_command(NODE["Id"], "power", False)     # notre extinction : pas « externe »
    await svc.refresh_once()
    assert [e["origin"] for e in svc.journal.recent()].count("externe") == 2


@pytest.mark.asyncio
async def test_journal_notes_alarms():
    stove = FakeStove()
    svc = make(stove)
    await svc.refresh_once()
    stove.status.update(IsInError=True)
    await svc.refresh_once()
    assert svc.journal.recent()[0]["label"] == "Alarme"


# ---- Jours sautés -------------------------------------------------------------------

@pytest.mark.asyncio
async def test_skipped_day_sends_nothing_and_is_noted_once():
    stove = off_stove()
    svc = make(stove)
    await svc.refresh_once()
    planner = Planner(":memory:", TZ)
    planner.add_plage("Matin", [1, 2], "07:00", "09:00", setpoint=21)
    planner.set_skip(TUE.date().isoformat(), True, TUE.date())
    sched = Scheduler(svc, planner)
    await sched.tick(at(7, 0, 5))
    await sched.tick(at(7, 0, 20))
    assert stove.sent == []
    assert [(e["label"], e["outcome"]) for e in svc.journal.recent()] == [("Allumage sauté", "skipped")]
    assert planner.upcoming(at(6, 0) - timedelta(days=1))[0]["kind"] in ("skip", "off")
    await sched.tick(at(7, 0, 5) + timedelta(days=1))      # mercredi : reprend normalement
    assert stove.sent


def test_skip_rejects_past_days_and_forgets_them():
    p = Planner(":memory:", TZ)
    with pytest.raises(ValueError):
        p.set_skip("2026-10-12", True, TUE.date())
    p.set_skip("2026-10-14", True, TUE.date())
    assert p.skip_dates(TUE.date()) == ["2026-10-14"]
    assert p.skip_dates(TUE.date() + timedelta(days=2)) == []


# ---- Pause et vacances ------------------------------------------------------------------

def test_pause_skips_starts_but_keeps_stops():
    p = Planner(":memory:", TZ)
    p.add_plage("Matin", [0, 1, 2, 3, 4], "07:00", "09:00")
    p.set_absence("pause", at(6), end_ts=at(0).timestamp() + 2 * 86400)        # jusqu'à jeudi 00:00
    [on] = p.due(at(7, 0, 5))
    assert on.skip_reason == "pause" and on.steps == ()
    [off] = p.due(at(9, 0, 5))
    assert off.steps == (("power", False),)
    [on_thu] = p.due(at(7, 0, 5) + timedelta(days=2))
    assert on_thu.skip_reason is None


@pytest.mark.asyncio
async def test_vacation_turns_off_skips_plages_and_preheats_two_hours_before_return():
    stove = FakeStove()                                   # allumé au départ
    svc = make(stove)
    await svc.refresh_once()
    planner = Planner(":memory:", TZ)
    planner.add_plage("Soir", list(range(7)), "17:00", "19:00", setpoint=21)
    back = at(18, 30) + timedelta(days=3)                  # retour vendredi 18:30
    planner.set_absence("vacances", at(10), end_ts=back.timestamp(), start_ts=at(10).timestamp(), preheat_minutes=120, setpoint=22)
    sched = Scheduler(svc, planner)
    await sched.tick(at(10, 0, 5))                         # départ : extinction
    assert stove.status["fase_op"] == "turning-off"
    stove.status["fase_op"] = "off"
    await sched.tick(at(17, 0, 5))                         # plage du soir : sautée
    assert len(stove.sent) == 1
    upcoming = planner.upcoming(at(17, 1))
    assert any(e["label"] == "Retour de vacances : préchauffage (22 °C)" for e in upcoming)
    await sched.tick(back - timedelta(minutes=120) + timedelta(seconds=5))   # vendredi 16:30 : préchauffage
    assert stove.status["fase_op"] == "turning-on" and stove.status["set_amb1"] == 22
    stove.status["fase_op"] = "on"
    sent = len(stove.sent)
    await sched.tick(back.replace(hour=17, minute=0, second=5))   # plage du soir pendant le préchauffage : allumage sauté
    await sched.tick(back.replace(hour=19, minute=0, second=5))   # après le retour : extinction normale de la plage
    assert len(stove.sent) == sent + 1 and stove.status["fase_op"] == "turning-off"
    assert planner.absence(back + timedelta(hours=1)) is None      # terminée, oubliée
    origins = {e["origin"] for e in svc.journal.recent()}
    assert {"vacances", "planning"} <= origins


def test_absence_validation():
    p = Planner(":memory:", TZ)
    with pytest.raises(ValueError):
        p.set_absence("vacances", at(10), end_ts=at(9).timestamp())
    with pytest.raises(ValueError):
        p.set_absence("vacances", at(10), end_ts=at(12).timestamp(), preheat_minutes=900)
    with pytest.raises(ValueError):
        p.set_absence("sabbat", at(10), end_ts=at(12).timestamp())


# ---- Allumage anticipé ------------------------------------------------------------------

def episode(t0, minutes, startup, rate, a0=18.0):
    """Relevés d'un allumage : éteint puis allumé, la pièce gagne `rate` °C/min après `startup` min."""
    out = [{"ts": t0 - 60, "phase": "off", "running": False, "ambient": a0}]
    for m in range(0, minutes + 1, 2):
        out.append({"ts": t0 + m * 60, "phase": "on", "running": True, "ambient": a0 + max(0.0, (m - startup) * rate)})
    return out


def test_learn_heating_speed_from_history():
    t0 = at(6).timestamp()
    samples = episode(t0, 120, startup=12, rate=0.05) + episode(t0 + 86400, 120, startup=14, rate=0.05)
    model = anticipation.learn(samples)
    assert model.episodes == 2
    assert 15 <= model.startup_min <= 22
    assert model.rate_per_min == pytest.approx(0.05, abs=0.01)
    assert anticipation.lead_minutes(model, 18.0, 21.0) == pytest.approx(model.startup_min + 2.7 / model.rate_per_min, abs=1)
    assert anticipation.lead_minutes(model, 22.0, 21.0) == round(model.startup_min)     # déjà chaud : juste le temps de démarrer
    assert anticipation.lead_minutes(anticipation.learn([]), None, 21) <= anticipation.MAX_LEAD


def test_anticipated_plage_starts_early_once():
    p = Planner(":memory:", TZ)
    p.add_plage("Matin", [1], "07:00", "09:00", setpoint=21, anticipate=True)
    p.lead_for = lambda plage: 38
    assert p.due(at(6, 21)) == []
    [on] = p.due(at(6, 22, 5))
    assert on.steps[0] == ("power", True) and "anticipé" in on.label
    p.mark_done(on.key, 0, "ok")
    assert p.due(at(6, 40)) == [] and p.due(at(7, 0, 5)) == []
    [event] = [e for e in p.upcoming(at(6, 0)) if e["kind"] == "on"]
    assert event["label"] == "Allumage anticipé : 21 °C à 07:00"
    with pytest.raises(ValueError):
        p.add_plage("Sans consigne", [1], "07:00", "09:00", anticipate=True)


# ---- API ------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_api_journal_skips_absence_and_no_public_diagnostics():
    stove = FakeStove()
    svc = make(stove)
    await svc.refresh_once()
    planner = Planner(":memory:", TZ)
    api = TestClient(create_app(SETTINGS, svc, planner))
    assert api.get("/diag/commands").status_code == 404
    assert api.get("/api/journal").status_code == 401
    tomorrow = (TUE.now(TZ) + timedelta(days=1)).date().isoformat()
    assert api.put(f"/api/schedule/skips/{tomorrow}", headers=H).json()["skips"] == [tomorrow]
    assert api.put("/api/schedule/skips/hier", headers=H).status_code == 422
    end = (TUE.now(TZ) + timedelta(days=5)).timestamp()
    assert api.put("/api/schedule/absence", headers=H, json={"kind": "vacances", "end_ts": end}).status_code == 200
    overview = api.get("/api/schedule/overview", headers=H).json()
    assert overview["skips"] == [tomorrow] and overview["absence"]["kind"] == "vacances"
    assert set(overview["anticipation"]) == {"startup_min", "rate_per_hour", "episodes"}
    assert api.delete("/api/schedule/absence", headers=H).status_code == 200
    labels = [e["label"] for e in api.get("/api/journal", headers=H).json()["events"]]
    assert labels[0] == "Absence annulée" and labels[1].startswith("Vacances du")
    plage = api.post("/api/plages", headers=H, json={"name": "M", "days": [1], "start": "07:00", "end": "08:00", "setpoint": 21, "anticipate": True})
    assert plage.json()["anticipate"] is True
