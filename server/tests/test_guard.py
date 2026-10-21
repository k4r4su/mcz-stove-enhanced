"""Garde de température (rallumage automatique sous un seuil) et notifications."""
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app import guard
from app.guard import Guard, Situation, decide
from app.main import create_app
from app.planner import Planner
from app.push import Notifier
from test_commands import NODE, SETTINGS, FakeStove, make
from test_planning import TZ, at

CFG = guard.validate({"enabled": True, "threshold": 18, "start": "07:00", "end": "22:00", "after": "setpoint"})


def advice(now, cross_in=1800, relight_in=0, on_time=False):
    ts = now.timestamp()
    return {"cross_ts": ts + cross_in, "relight_ts": ts + relight_in, "on_time": on_time}


def sit(now, **kw):
    base = dict(local=now, lit=False, ambient=18.4, setpoint=21.0, blocked=None, handover=False, session=None, advice=advice(now))
    return Situation(**{**base, **kw})


def test_relights_when_the_room_will_drop_below_the_threshold():
    now = at(15)
    what, reason = decide(CFG, sit(now))
    assert what == "on" and "sous 18,0 °C vers 15:30" in reason
    assert decide(CFG, sit(now, ambient=17.6))[1] == "la pièce est à 17,6 °C, seuil 18,0 °C"


@pytest.mark.parametrize("change", [
    dict(advice=None),                                        # rien de prévu
    dict(lit=True),
    dict(blocked="vacances"),
    dict(last_off=at(14, 45).timestamp()),                    # éteint par la garde il y a 15 min
    dict(last_try=at(14, 55).timestamp()),                    # échec il y a 5 min
])
def test_does_not_relight(change):
    assert decide(CFG, sit(at(15), **change)) is None


def test_does_not_relight_when_a_planned_ignition_arrives_in_time_or_outside_hours():
    assert decide(CFG, sit(at(15), advice=advice(at(15), on_time=True))) is None
    assert decide(CFG, sit(at(15), advice=advice(at(15), relight_in=1200))) is None    # pas encore l'heure
    assert decide(CFG, sit(at(6, 30))) is None                                          # avant 7 h
    assert decide(CFG, sit(at(21, 15))) is None                                         # moins d'1 h avant la fin
    late = at(20, 0)
    assert decide(CFG, sit(late, advice=advice(late, cross_in=3 * 3600))) is None        # passerait sous le seuil après 22 h
    assert decide({**CFG, "enabled": False}, sit(at(15))) is None


def test_switches_off_once_the_setpoint_is_reached_after_an_hour():
    session = {"lit_ts": at(15).timestamp()}
    assert decide(CFG, sit(at(15, 40), lit=True, ambient=21.0, session=session)) is None             # moins d'1 h
    assert decide(CFG, sit(at(16, 5), lit=True, ambient=20.0, session=session)) is None              # pas encore 21 °C
    assert decide(CFG, sit(at(16, 5), lit=True, ambient=20.8, session=session)) == ("off", "consigne de 21,0 °C atteinte")
    keep = {**CFG, "after": "window"}
    assert decide(keep, sit(at(16, 5), lit=True, ambient=21.0, session=session)) is None
    assert decide(keep, sit(at(22, 0), lit=True, ambient=21.0, session=session)) == ("off", "fin de la garde (22:00)")


def test_hands_over_to_the_planning_and_forgets_a_stove_switched_off_elsewhere():
    session = {"lit_ts": at(15).timestamp()}
    assert decide(CFG, sit(at(16, 30), lit=True, ambient=21, session=session, handover=True))[0] == "end"
    assert decide(CFG, sit(at(16, 30), lit=False, session=session))[0] == "end"


def test_window_across_midnight():
    assert guard.window_end(at(23), "22:00", "06:00") == at(6, day=at(0) + timedelta(days=1))
    assert guard.window_end(at(5), "22:00", "06:00") == at(6)
    assert guard.window_end(at(12), "22:00", "06:00") is None


@pytest.mark.parametrize("bad", [dict(threshold=30), dict(threshold=18.3), dict(start="07:00", end="07:00"), dict(after="jamais")])
def test_invalid_guard_settings(bad):
    with pytest.raises(ValueError):
        guard.validate({**CFG, **bad})


def test_plage_active_includes_the_anticipated_start():
    planner = Planner(":memory:", TZ)
    planner.lead_for = lambda p: 45
    planner.add_plage("Soir", [1], "18:00", "22:00", setpoint=21, anticipate=True)
    assert not planner.plage_active(at(17, 0))
    assert planner.plage_active(at(17, 20)) and planner.plage_active(at(21, 59))
    assert not planner.plage_active(at(22, 0))
    planner.set_skip("2026-10-13", True, at(12).date())
    assert not planner.plage_active(at(19))


# ---- Boucle complète, avec le faux poêle -------------------------------------------------------

async def setup(ambient=18.3):
    stove = FakeStove()
    stove.status.update(fase_op="off", power_enabled=False, temp_amb_install=ambient)
    svc = make(stove)
    await svc.refresh_once()
    planner = Planner(":memory:", TZ)
    planner.set_setting("guard", CFG)
    sent = []
    calls = []

    async def advise(stove_id, threshold):
        calls.append(threshold)
        return advice(datetime.fromtimestamp(svc.last_ok[stove_id], TZ), cross_in=600)

    async def insights(stove_id):
        return {"fumes": {"change": 17}}

    g = Guard(svc, planner, lambda kind, title, body="": sent.append((kind, title, body)), advise, insights)
    return stove, svc, planner, g, sent, calls


@pytest.mark.asyncio
async def test_guard_relights_then_switches_off_at_the_setpoint():
    stove, svc, planner, g, sent, calls = await setup()
    sid = NODE["Id"]
    now = at(15)
    svc.last_ok[sid] = now.timestamp()
    await g.tick(now)
    assert calls == [18.0] and stove.status["fase_op"] == "turning-on"
    assert planner.get_setting("guard_state")["sessions"][sid]["lit_ts"] == now.timestamp()
    entry = svc.journal.recent(1)[0]
    assert entry["origin"] == "garde" and entry["label"] == "Allumage" and "sous 18,0 °C vers 15:10" in entry["detail"]
    assert ("guard", "Garde : poêle rallumé") in [s[:2] for s in sent]

    stove.status.update(fase_op="on", temp_amb_install=21.0)
    await svc.refresh_once()
    later = at(16, 10)
    svc.last_ok[sid] = later.timestamp()
    await g.tick(later)
    assert stove.status["fase_op"] == "turning-off"
    state = planner.get_setting("guard_state")
    assert state["sessions"] == {} and state["last_off"][sid] == later.timestamp()
    assert sent[-1][:2] == ("guard", "Garde : poêle éteint")


@pytest.mark.asyncio
async def test_guard_does_nothing_on_vacation_and_in_alarm():
    stove, svc, planner, g, sent, calls = await setup()
    sid = NODE["Id"]
    planner.set_absence("pause", at(12), at(23).timestamp())
    svc.last_ok[sid] = at(15).timestamp()
    await g.tick(at(15))
    assert stove.sent == [] and calls == []
    planner.clear_absence()
    stove.status["IsInError"] = True
    await svc.refresh_once()
    svc.last_ok[sid] = at(15, 5).timestamp()
    await g.tick(at(15, 5))
    assert stove.sent == []


@pytest.mark.asyncio
async def test_watch_cold_room_once_offline_and_fumes():
    stove, svc, planner, g, sent, calls = await setup(ambient=17.5)
    planner.set_setting("guard", {**CFG, "enabled": False})
    sid = NODE["Id"]
    svc.last_ok[sid] = at(15).timestamp()
    await g.tick(at(15))
    await g.tick(at(15, 1))
    cold = [s for s in sent if s[0] == "cold"]
    assert len(cold) == 1 and cold[0][1] == "Pièce à 17,5 °C" and "garde de température est désactivée" in cold[0][2]
    assert [s[0] for s in sent].count("fumes") == 1                     # 15 h : contrôle du jour fait, une seule fois
    await g.tick(at(15, 40))                                             # plus de relevé depuis 40 min
    assert [s[1] for s in sent if s[0] == "offline"] == ["Poêle injoignable"]
    svc.last_ok[sid] = at(15, 41).timestamp()
    await g.tick(at(15, 41))
    assert [s[1] for s in sent if s[0] == "offline"] == ["Poêle injoignable", "Poêle de nouveau joignable"]


# ---- Notifications ---------------------------------------------------------------------------

def make_notifier(statuses=None):
    store = {}
    pushed = []

    def sender(sub, payload, pem, subject):
        pushed.append((sub["endpoint"], payload))
        return (statuses or {}).get(sub["endpoint"], 201)

    n = Notifier(":memory:", "https://poele.example", store.get, store.__setitem__, sender)
    return n, store, pushed


@pytest.mark.asyncio
async def test_notifier_sends_to_each_device_and_drops_expired_ones():
    n, store, pushed = make_notifier({"https://push.example/b": 410})
    key = n.ensure_key()
    assert len(key) == 87                                                # point P-256 non compressé, base64url
    again = Notifier(":memory:", "x", store.get, lambda k, v: None)
    assert again.ensure_key() == key                                     # la clé est gardée : les abonnements restent valables
    n.subscribe("https://push.example/a", "p", "s", "iPhone")
    n.subscribe("https://push.example/b", "p", "s", "iPad")
    out = await n.send("alarm", "Poêle en alarme", "défaut")
    assert out["sent"] == 1 and out["failed"] == 1 and "expiré" in out["errors"][0] and [s["label"] for s in n.subscriptions()] == ["iPhone"]
    assert '"tag": "alarm"' in pushed[0][1]
    store["notify"] = {"alarm": False}
    assert (await n.send("alarm", "x"))["skipped"] and (await n.send("external", "x"))["skipped"]   # décoché / désactivé par défaut
    with pytest.raises(ValueError):
        n.subscribe("http://insecure", "p", "s")


def test_vapid_key_signs_a_token():
    from py_vapid import Vapid02
    n, store, _ = make_notifier()
    n.ensure_key()
    headers = Vapid02.from_pem(n._pem).sign({"sub": "https://poele.example", "aud": "https://web.push.apple.com", "exp": 2_000_000_000})
    assert headers["Authorization"].startswith("vapid t=")


@pytest.mark.asyncio
async def test_alarm_and_unconfirmed_planned_ignition_are_notified():
    from app.scheduler import Scheduler
    stove = FakeStove(lost=5)
    stove.status.update(fase_op="off", power_enabled=False)
    svc = make(stove)
    got = []
    svc.notify = lambda kind, title, body="": got.append((kind, title, body))
    await svc.refresh_once()
    planner = Planner(":memory:", TZ)
    planner.add_plage("Matin", [1], "07:00", "09:00")
    sched = Scheduler(svc, planner)
    await sched.tick(at(7, 0, 5))
    assert got[-1][:2] == ("failed", "Allumage automatique non confirmé") and "Matin" in got[-1][2]
    stove.status.update(fase_op="off", power_enabled=False)
    await sched.tick(at(7, 0, 20))                        # le planning réessaie : pas de deuxième notification
    assert [g[0] for g in got].count("failed") == 1
    stove.status["IsInError"] = True
    await svc.refresh_once()
    assert got[-1][:2] == ("alarm", "Poêle en alarme")


@pytest.mark.asyncio
async def test_push_and_guard_api():
    stove = FakeStove()
    svc = make(stove)
    await svc.refresh_once()
    n, store, pushed = make_notifier()
    api = TestClient(create_app(SETTINGS, svc, Planner(":memory:", TZ), notifier=n))
    h = {"Authorization": "Bearer secret"}
    assert api.get("/api/push/key").status_code == 401
    assert len(api.get("/api/push/key", headers=h).json()["key"]) == 87
    sub = {"endpoint": "https://push.example/me", "keys": {"p256dh": "p", "auth": "s"}, "label": "iPhone"}
    assert api.post("/api/push/subscriptions", json=sub, headers=h).json()["devices"] == 1
    assert api.post("/api/push/test", json={"endpoint": sub["endpoint"]}, headers=h).json()["sent"] == 1
    assert api.post("/api/push/test", json={"endpoint": "https://push.example/other"}, headers=h).status_code == 404
    prefs = api.put("/api/settings/notify", json={"external": True, "nope": True}, headers=h).json()
    assert prefs["external"] is True and "nope" not in prefs and prefs["alarm"] is True
    assert api.post("/api/push/unsubscribe", json={"endpoint": sub["endpoint"]}, headers=h).json()["devices"] == 0

    assert api.get("/api/settings/guard", headers=h).json()["enabled"] is False
    assert api.put("/api/settings/guard", json={"enabled": True, "threshold": 30}, headers=h).status_code == 422
    body = api.put("/api/settings/guard", json={"enabled": True, "threshold": 18.5, "start": "08:00", "end": "21:30"}, headers=h).json()
    assert body["enabled"] and body["threshold"] == 18.5 and body["sessions"] == {}
    assert api.get("/api/schedule/overview", headers=h).json()["guard"]["start"] == "08:00"
    assert svc.journal.recent(1)[0]["label"] == "Garde activée"
    assert api.get("/sw.js").status_code == 200


def test_cooling_down_is_not_lit_and_unknown_phases_are_flagged():
    from app import commands
    assert commands.is_lit({"fase_op": "cooling-down"}) is False             # vu le 15/10 00:10–00:40, fumées en baisse
    assert commands.phase_known({"fase_op": "cooling-down"}) and not commands.phase_known({"fase_op": "mystery"})


@pytest.mark.asyncio
async def test_unknown_phase_is_noted_but_not_called_a_change_elsewhere_and_never_toggled():
    from app.cloud import CloudError
    stove = FakeStove()
    stove.status.update(fase_op="off", power_enabled=False)
    svc = make(stove)
    await svc.refresh_once()
    stove.status.update(fase_op="mystery")
    await svc.refresh_once()
    await svc.refresh_once()
    entries = svc.journal.recent()
    assert [e["label"] for e in entries] == ["Phase inconnue « mystery »"] and "fase_op=mystery" in entries[0]["detail"]
    with pytest.raises(CloudError):
        await svc.send_command(NODE["Id"], "power", True)
    assert stove.sent == []


@pytest.mark.asyncio
async def test_planned_extinction_is_replayed_when_the_stove_relights_by_itself():
    """15/10 : arrêté seul à 10:46, extinction de 11:02 « déjà éteint », rallumé seul à 11:16 → on l'éteint."""
    from app.scheduler import Scheduler
    stove = FakeStove()
    stove.status.update(fase_op="off", power_enabled=True)
    svc = make(stove)
    await svc.refresh_once()
    planner = Planner(":memory:", TZ)
    planner.add_plage("Matin", [1], "07:00", "09:00")
    planner.mark_done(f"plage:1:on:{at(7, 0).isoformat()}", at(7, 0).timestamp(), "ok")
    sched = Scheduler(svc, planner)
    await sched.tick(at(9, 0, 5))
    assert stove.sent == []                                                 # déjà éteint : rien envoyé
    stove.status.update(fase_op="turning-on")                               # il repart tout seul
    await svc.refresh_once()
    await sched.tick(at(9, 14))
    assert len(stove.sent) == 1 and stove.status["fase_op"] == "turning-off"
    assert "rallumé seul" in svc.journal.recent()[0]["detail"]
    stove.status.update(fase_op="on")                                       # une seule tentative
    await svc.refresh_once()
    await sched.tick(at(9, 20))
    assert len(stove.sent) == 1


@pytest.mark.asyncio
async def test_no_replay_after_the_watch_window_or_when_lit_from_the_app():
    from app.scheduler import Scheduler
    stove = FakeStove()
    stove.status.update(fase_op="off", power_enabled=False)
    svc = make(stove)
    await svc.refresh_once()
    planner = Planner(":memory:", TZ)
    planner.add_plage("Matin", [1], "07:00", "09:00")
    planner.mark_done(f"plage:1:on:{at(7, 0).isoformat()}", at(7, 0).timestamp(), "ok")
    sched = Scheduler(svc, planner)
    await sched.tick(at(9, 0, 5))
    await svc.send_command(NODE["Id"], "power", True)                       # allumé depuis l'app : on le laisse
    await sched.tick(at(9, 10))
    assert len(stove.sent) == 1 and stove.status["fase_op"] == "turning-on"
