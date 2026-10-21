"""API du pont MCZ : état instantané (cache), historique, métriques."""
from __future__ import annotations

import hashlib
import hmac
import secrets
import logging
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from typing import Any

from pathlib import Path

import httpx
from fastapi import Body, Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel

from . import metrics
from .cloud import CloudError, MczCloud
from .config import Settings
from .history import History
from . import guard as guard_mod, insights, official, projection, stats
from .anticipation import Anticipator
from .journal import Journal
from .planner import Planner
from .push import Notifier
from .weather import Weather, WeatherError
from .scheduler import Scheduler
from .service import StoveService


class PlageIn(BaseModel):
    name: str
    days: list[int]
    start: str
    end: str
    setpoint: float | None = None
    mode: str | None = None
    enabled: bool = True
    anticipate: bool = False     # `start` = heure où il doit faire `setpoint` °C ; l'allumage part plus tôt


class TimerIn(BaseModel):
    action: Literal["on", "off"] = "off"
    time: str | None = None      # « 22:30 » : prochaine occurrence
    at_ts: float | None = None   # ou époque UNIX précise


class LoginIn(BaseModel):
    username: str
    password: str


SESSION_DAYS = 90
INSIGHT_DAYS = 30               # indicateurs : 30 derniers jours (le coût, lui, couvre la saison)
INSIGHTS_CACHE_SECONDS = 300
MAX_FAILURES, FAILURE_WINDOW = 5, 15 * 60


def digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class LocationIn(BaseModel):
    name: str = ""
    lat: float
    lon: float


class AbsenceIn(BaseModel):
    kind: Literal["pause", "vacances"]
    end_ts: float                     # pause : fin ; vacances : heure du retour
    start_ts: float | None = None     # vacances : départ (défaut : maintenant)
    preheat_minutes: int = 120
    setpoint: float = 21.0
    off_at_start: bool = True


class PriceIn(BaseModel):
    bag_eur: float
    bag_kg: float = 15.0


class SubscriptionIn(BaseModel):
    endpoint: str
    keys: dict[str, str]
    label: str = ""


class EndpointIn(BaseModel):
    endpoint: str | None = None


class GuardIn(BaseModel):
    enabled: bool = False
    threshold: float = 18.0
    start: str = "07:00"
    end: str = "22:00"
    after: Literal["setpoint", "window"] = "setpoint"


class RefillIn(BaseModel):
    kg: float | None = None
    full: bool = False


def create_app(settings: Settings | None = None, service: StoveService | None = None, planner: Planner | None = None,
               weather: Weather | None = None, notifier: Notifier | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    tz = ZoneInfo(settings.timezone)
    planner = planner or Planner(settings.db_path, tz)
    notifier = notifier or Notifier(settings.db_path, settings.push_subject, planner.get_setting, planner.set_setting)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if service is None:
            # Longueurs seulement (jamais les valeurs) : repère un identifiant tronqué ou entre guillemets dans la stack.
            logging.getLogger("mcz").warning("Identifiants reçus : MCZ_USER %d caractères, MCZ_PASSWORD %d caractères",
                                             len(settings.mcz_user), len(settings.mcz_password))
            app.state.service = StoveService(MczCloud(settings), History(settings.db_path), settings.poll_seconds,
                                             journal=Journal(settings.db_path))
            app.state.service.start()
            app.state.service.notify = notifier.emit
            app.state.scheduler = Scheduler(app.state.service, planner)
            app.state.scheduler.start()
            app.state.guard = guard_mod.Guard(app.state.service, planner, notifier.emit, projection_for, insight_body)
            app.state.guard.start()
        else:
            app.state.service = service
        yield
        if service is None:
            app.state.guard.stop()
            app.state.scheduler.stop()
            await app.state.service.stop()

    started = time.time()
    app = FastAPI(title="mcz-bridge", lifespan=lifespan)
    if service is not None:
        app.state.service = service  # disponible même sans exécution du lifespan (tests)
        service.notify = notifier.emit
    app.state.notifier = notifier
    anticipator = Anticipator(lambda: app.state.service)
    planner.lead_for = anticipator.lead_for

    def require_token(authorization: str = Header(default="")) -> None:
        """Jeton de l'app iPhone (BRIDGE_TOKEN) ou session ouverte par identifiant / mot de passe dans la web app."""
        expected = f"Bearer {settings.bridge_token}"
        if hmac.compare_digest(authorization.encode(), expected.encode()):
            return
        token = authorization.removeprefix("Bearer ").strip()
        if token and planner.session_valid(digest(token)):
            return
        raise HTTPException(status_code=401, detail="Connexion requise")

    failures: dict[str, list[float]] = {}

    @app.get("/api/login")
    def login_status() -> dict:
        return {"enabled": bool(settings.web_user and settings.web_password)}

    @app.post("/api/login")
    def login(body: LoginIn, request: Request) -> dict:
        if not (settings.web_user and settings.web_password):
            raise HTTPException(status_code=404, detail="Connexion par identifiant non configurée (WEB_USER / WEB_PASSWORD)")
        ip = (request.headers.get("x-forwarded-for") or (request.client.host if request.client else "?")).split(",")[0].strip()
        now = time.time()
        recent = [t for t in failures.get(ip, []) if now - t < FAILURE_WINDOW]
        if len(recent) >= MAX_FAILURES:
            raise HTTPException(status_code=429, detail="Trop d'essais : réessaie dans 15 minutes")
        ok_user = hmac.compare_digest(body.username.strip().encode(), settings.web_user.encode())
        ok_pass = hmac.compare_digest(body.password.encode(), settings.web_password.encode())
        if not (ok_user and ok_pass):
            failures[ip] = recent + [now]
            raise HTTPException(status_code=401, detail="Identifiant ou mot de passe incorrect")
        failures.pop(ip, None)
        token = secrets.token_urlsafe(32)
        planner.add_session(digest(token), now + SESSION_DAYS * 86400)
        return {"token": token, "expires_days": SESSION_DAYS}

    @app.post("/api/logout")
    def logout(authorization: str = Header(default="")) -> dict:
        planner.drop_session(digest(authorization.removeprefix("Bearer ").strip()))
        return {"ok": True}

    @app.get("/health")
    def health() -> dict:
        svc: StoveService = app.state.service
        return {"ok": svc.last_error is None, "stoves": len(svc.latest), "error": svc.last_error,
                "uptime_seconds": round(time.time() - started)}

    @app.get("/api/stoves", dependencies=[Depends(require_token)])
    def stoves() -> dict:
        svc: StoveService = app.state.service
        now = time.time()
        return {"stoves": [{**s.to_json(), "age_seconds": round(now - s.fetched_at, 1)} for s in svc.latest.values()]}

    @app.get("/api/stoves/{stove_id}/history", dependencies=[Depends(require_token)])
    async def history(stove_id: str, hours: float = 24, outdoor: bool = False) -> dict:
        svc: StoveService = app.state.service
        if stove_id not in svc.latest:
            raise HTTPException(status_code=404, detail="Poêle inconnu")
        since = time.time() - hours * 3600
        body = {"samples": svc.history.since(stove_id, since)}
        if outdoor:   # température extérieure heure par heure sur la même période (courbe Température)
            hourly, weather_error = await outdoor_hourly()
            body["outdoor"] = [[ts, round(v, 1)] for ts, v in sorted(hourly.items()) if since - 3600 <= ts <= time.time() + 3600]
            body["weather_error"] = weather_error
        return body

    @app.get("/api/stoves/{stove_id}/raw", dependencies=[Depends(require_token)])
    async def raw(stove_id: str) -> dict:
        """Diagnostic : réponses brutes du cloud, numéro de série / SSID / MAC masqués."""
        svc: StoveService = app.state.service
        if stove_id not in svc.latest:
            raise HTTPException(status_code=404, detail="Poêle inconnu")
        hide = ("sm_sn", "ssid", "mac", "description", "uniquecode", "serial")
        def clean(d: dict) -> dict:
            return {k: ("***" if any(h in k.lower() for h in hide) else v) for k, v in d.items()}
        try:
            return {"status": clean(await svc.cloud.status(stove_id)), "detail": clean(await svc.cloud.detail(stove_id))}
        except CloudError as err:
            raise HTTPException(status_code=502, detail=str(err))

    @app.post("/api/stoves/{stove_id}/commands", dependencies=[Depends(require_token)])
    async def command(stove_id: str, kind: str = Body(...), value: Any = Body(...), wait: bool = Body(True)) -> dict:
        svc: StoveService = app.state.service
        try:
            if not wait:   # l'app suit ensuite /api/commands/{id} par des requêtes courtes
                return svc.start_command(stove_id, kind, value)
            return await svc.send_command(stove_id, kind, value)
        except KeyError:
            raise HTTPException(status_code=404, detail="Poêle inconnu")
        except ValueError as err:
            raise HTTPException(status_code=422, detail=str(err))
        except CloudError as err:
            raise HTTPException(status_code=502, detail=str(err))

    @app.get("/api/stoves/{stove_id}/official-schedule", dependencies=[Depends(require_token)])
    async def official_schedule(stove_id: str) -> dict:
        """Programme de l'app Maestro+ (lecture seule), à comparer à nos plages."""
        svc: StoveService = app.state.service
        snap = svc.latest.get(stove_id)
        if snap is None:
            raise HTTPException(status_code=404, detail="Poêle inconnu")
        try:
            raw = await svc.cloud.chrono(stove_id)
        except CloudError as err:
            raise HTTPException(status_code=502, detail=str(err))
        return {"enabled": snap.chrono_enabled, "slots": official.normalize(raw)}

    @app.get("/api/commands/{job_id}", dependencies=[Depends(require_token)])
    def command_status(job_id: str) -> dict:
        job = app.state.service.jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Commande inconnue")
        return job

    @app.get("/metrics", dependencies=[Depends(require_token)])
    def prometheus() -> Response:
        svc: StoveService = app.state.service
        return Response(metrics.render(list(svc.latest.values())), media_type="text/plain; version=0.0.4")


    # ---- Programmation ----------------------------------------------------------

    def guard(fn):
        try:
            return fn()
        except KeyError:
            raise HTTPException(status_code=404, detail="Introuvable")
        except ValueError as err:
            raise HTTPException(status_code=422, detail=str(err))

    @app.get("/api/plages", dependencies=[Depends(require_token)])
    def list_plages() -> dict:
        return {"plages": planner.list_plages()}

    @app.post("/api/plages", dependencies=[Depends(require_token)])
    def add_plage(body: PlageIn) -> dict:
        return guard(lambda: planner.add_plage(**body.model_dump()))

    @app.put("/api/plages/{plage_id}", dependencies=[Depends(require_token)])
    def update_plage(plage_id: int, body: PlageIn) -> dict:
        return guard(lambda: planner.update_plage(plage_id, **body.model_dump()))

    @app.delete("/api/plages/{plage_id}", dependencies=[Depends(require_token)])
    def delete_plage(plage_id: int) -> dict:
        guard(lambda: planner.delete_plage(plage_id))
        return {"ok": True}

    def meteo() -> Weather:
        async def lookup():
            svc: StoveService = app.state.service
            stove_id = next(iter(svc.latest), None)
            if stove_id is None:
                raise WeatherError("poêle pas encore connu")
            return await svc.cloud.location(stove_id)

        current = weather or getattr(app.state, "weather", None)
        if current is None:
            current = app.state.weather = Weather(tz, settings.weather_lat, settings.weather_lon, lookup,
                                                  saved_position=lambda: planner.get_setting("weather_location"))
        return current

    @app.get("/api/weather", dependencies=[Depends(require_token)])
    async def forecast() -> dict:
        try:
            return await meteo().forecast()
        except WeatherError as err:
            raise HTTPException(status_code=503, detail=str(err))

    @app.get("/api/settings/weather-location/search", dependencies=[Depends(require_token)])
    async def search_city(name: str) -> dict:
        """Ville → position (géocodage Open-Meteo), pour la web app (l'app iPhone géocode elle-même)."""
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get("https://geocoding-api.open-meteo.com/v1/search",
                                        params={"name": name.strip()[:80], "count": 1, "language": "fr"})
            hit = (resp.json().get("results") or [None])[0]
        except (httpx.HTTPError, ValueError):
            raise HTTPException(status_code=502, detail="Service de géocodage injoignable")
        if not hit:
            raise HTTPException(status_code=404, detail="Ville introuvable")
        label = ", ".join(x for x in (hit.get("name"), hit.get("country")) if x)
        return {"name": label, "lat": hit["latitude"], "lon": hit["longitude"]}

    @app.get("/api/settings/weather-location", dependencies=[Depends(require_token)])
    def get_weather_location() -> dict:
        return planner.get_setting("weather_location") or {}

    @app.put("/api/settings/weather-location", dependencies=[Depends(require_token)])
    def set_weather_location(body: LocationIn) -> dict:
        if not (-90 <= body.lat <= 90 and -180 <= body.lon <= 180):
            raise HTTPException(status_code=422, detail="Position invalide")
        planner.set_setting("weather_location", {"name": body.name.strip()[:80], "lat": round(body.lat, 3), "lon": round(body.lon, 3)})
        meteo = weather or getattr(app.state, "weather", None)
        if meteo is not None:
            meteo.invalidate()
        return {"ok": True}

    @app.get("/api/schedule/overview", dependencies=[Depends(require_token)])
    def overview() -> dict:
        """Ce qui va se passer et ce qui s'est passé : de quoi vérifier que les plages sont bien prises en compte."""
        now = datetime.now(tz)
        return {"upcoming": planner.upcoming(now, limit=8), "recent": planner.recent_runs(), "skips": planner.skip_dates(now.date()),
                "absence": planner.absence(now), "anticipation": anticipator.model().to_json(), "guard": guard_status()}

    def guard_status() -> dict:
        state = planner.get_setting("guard_state") or {}
        return {**guard_mod.config(planner.get_setting("guard")), "sessions": state.get("sessions", {})}

    @app.get("/api/settings/guard", dependencies=[Depends(require_token)])
    def get_guard() -> dict:
        return guard_status()

    @app.put("/api/settings/guard", dependencies=[Depends(require_token)])
    def set_guard(body: GuardIn) -> dict:
        cfg = guard(lambda: guard_mod.validate(body.model_dump()))
        before = guard_mod.config(planner.get_setting("guard"))
        planner.set_setting("guard", cfg)
        if cfg["enabled"] != before["enabled"] or (cfg["enabled"] and cfg != before):
            app.state.service.journal.add("app", "Garde activée" if cfg["enabled"] else "Garde désactivée", "info",
                                          f"sous {guard_mod.fmt(cfg['threshold'])} °C, de {cfg['start']} à {cfg['end']}" if cfg["enabled"] else "")
        return guard_status()

    @app.put("/api/schedule/skips/{day}", dependencies=[Depends(require_token)])
    def skip_day(day: str) -> dict:
        """« Pas d'allumage ce jour-là » : les allumages des plages de ce jour sont sautés, les plages ne changent pas."""
        return {"skips": guard(lambda: planner.set_skip(day, True, datetime.now(tz).date()))}

    @app.delete("/api/schedule/skips/{day}", dependencies=[Depends(require_token)])
    def unskip_day(day: str) -> dict:
        return {"skips": guard(lambda: planner.set_skip(day, False, datetime.now(tz).date()))}

    def _fmt(ts: float) -> str:
        return datetime.fromtimestamp(ts, tz).strftime("%d/%m %H:%M")

    @app.put("/api/schedule/absence", dependencies=[Depends(require_token)])
    def set_absence(body: AbsenceIn) -> dict:
        absence = guard(lambda: planner.set_absence(body.kind, datetime.now(tz), body.end_ts, body.start_ts, body.preheat_minutes,
                                                    body.setpoint, body.off_at_start))
        if absence["kind"] == "vacances":
            what = f"Vacances du {_fmt(absence['start_ts'])} au {_fmt(absence['end_ts'])}"
            how = f"préchauffage {absence['preheat_minutes']} min avant le retour" if absence["preheat_minutes"] else "sans préchauffage"
        else:
            what, how = f"Pause jusqu'au {_fmt(absence['end_ts'])}", "aucun allumage de plage"
        app.state.service.journal.add("app", what, "info", how)
        return absence

    @app.delete("/api/schedule/absence", dependencies=[Depends(require_token)])
    def cancel_absence() -> dict:
        if planner.absence(datetime.now(tz)):
            planner.clear_absence()
            app.state.service.journal.add("app", "Absence annulée", "info", "les plages reprennent")
        return {"ok": True}

    @app.get("/api/journal", dependencies=[Depends(require_token)])
    def journal(days: float = 7) -> dict:
        """Qui a fait quoi : nos commandes (app, planning, vacances), ce qui a changé sans nous, les alarmes."""
        return {"events": app.state.service.journal.recent(max(1.0, min(days, 90)))}

    @app.get("/api/stoves/{stove_id}/timers", dependencies=[Depends(require_token)])
    def timers(stove_id: str) -> dict:
        return {"timers": planner.pending_timers(stove_id)}

    @app.post("/api/stoves/{stove_id}/timers", dependencies=[Depends(require_token)])
    def add_timer(stove_id: str, body: TimerIn) -> dict:
        if stove_id not in app.state.service.latest:
            raise HTTPException(status_code=404, detail="Poêle inconnu")
        now = datetime.now(tz)
        return guard(lambda: planner.add_timer(stove_id, body.action, now, at_time=body.time, at_ts=body.at_ts))

    @app.delete("/api/timers/{timer_id}", dependencies=[Depends(require_token)])
    def cancel_timer(timer_id: int) -> dict:
        guard(lambda: planner.cancel_timer(timer_id))
        return {"ok": True}

    # ---- Statistiques et pellets -----------------------------------------------

    @app.get("/api/stoves/{stove_id}/stats", dependencies=[Depends(require_token)])
    def stove_stats(stove_id: str, days: int = 7) -> dict:
        svc: StoveService = app.state.service
        days = max(1, min(days, 90))
        now = datetime.now(tz)
        first = datetime.combine(now.date() - timedelta(days=days - 1), datetime.min.time(), tz)   # minuit du premier jour : journée complète
        samples = svc.history.since(stove_id, first.timestamp() - stats.MAX_GAP)
        return {"days": stats.daily_stats(samples, tz, days, now)}

    @app.get("/api/settings/pellet-price", dependencies=[Depends(require_token)])
    def get_pellet_price() -> dict:
        return planner.get_setting("pellet_price") or {}

    @app.put("/api/settings/pellet-price", dependencies=[Depends(require_token)])
    def set_pellet_price(body: PriceIn) -> dict:
        if not (0 < body.bag_eur <= 100 and 5 <= body.bag_kg <= 30):
            raise HTTPException(status_code=422, detail="Prix ou poids du sac invalide")
        price = {"bag_eur": round(body.bag_eur, 2), "bag_kg": round(body.bag_kg, 1)}
        planner.set_setting("pellet_price", price)
        return price

    insight_cache: dict[str, tuple[float, dict]] = {}

    async def outdoor_hourly() -> tuple[dict[float, float], str | None]:
        try:
            return await meteo().hourly_temps(), None
        except WeatherError as err:
            return {}, str(err)

    async def insight_body(stove_id: str) -> dict:
        """Partie des indicateurs tirée de l'historique (gardée 5 min) : le prix s'applique ensuite."""
        svc: StoveService = app.state.service
        now = datetime.now(tz)
        cached = insight_cache.get(stove_id)
        if cached and time.time() - cached[0] < INSIGHTS_CACHE_SECONDS:
            body = cached[1]
        else:
            hourly, weather_error = await outdoor_hourly()
            today = now.date()
            season = insights.season_start(today)
            samples = svc.history.since(stove_id, insights.midnight(season, tz) - stats.MAX_GAP)
            first_ts = samples[0]["ts"] if samples else None
            month = [s for s in samples if s["ts"] >= insights.midnight(today - timedelta(days=INSIGHT_DAYS), tz) - stats.MAX_GAP]
            outdoor = insights.outdoor_daily(hourly, tz)
            days = stats.daily_stats(month, tz, INSIGHT_DAYS, now)
            for d in days:
                d["outdoor"] = outdoor.get(d["date"])
                d["dju"] = insights.degree_days(d["outdoor"])
            now_ts = now.timestamp()
            body = {
                "days": days,
                "kg": {"week": stats.consumed_kg(samples, insights.midnight(insights.week_start(today), tz), now_ts),
                       "month": stats.consumed_kg(samples, insights.midnight(insights.month_start(today), tz), now_ts),
                       "season": stats.consumed_kg(samples, insights.midnight(season, tz), now_ts)},
                "season_since": max(insights.midnight(season, tz), first_ts) if first_ts else None,
                "weather": insights.weather_compare(days, today),
                "cooling": insights.cooling(month, hourly, tz),
                "comfort": insights.comfort(month, now_ts - INSIGHT_DAYS * 86400),
                "fumes": insights.fumes(month, tz, today),
                "weather_error": weather_error,
            }
            insight_cache[stove_id] = (time.time(), body)
        return body

    @app.get("/api/stoves/{stove_id}/insights", dependencies=[Depends(require_token)])
    async def stove_insights(stove_id: str) -> dict:
        """Coût, chauffe comparée à la météo, refroidissement de la maison, confort, fumées (30 jours ; coût sur la saison)."""
        if stove_id not in app.state.service.latest:
            raise HTTPException(status_code=404, detail="Poêle inconnu")
        body = await insight_body(stove_id)
        price = planner.get_setting("pellet_price") or None
        eur_per_kg = price["bag_eur"] / price["bag_kg"] if price else None
        cost = {k: insights.money(v, eur_per_kg) for k, v in body["kg"].items()}
        week_days = sum(1 for d in body["days"][-7:] if d["coverage"] > 0)
        cost["month_pace"] = round(cost["week"] / week_days * 30, 2) if cost["week"] is not None and week_days >= 3 else None
        return {**body, "kg": {k: round(v, 1) for k, v in body["kg"].items()}, "price": price, "cost": cost,
                "days": [{**d, "cost": insights.money(d["pellets_kg_est"], eur_per_kg)} for d in body["days"]]}

    @app.get("/api/stoves/{stove_id}/pellets", dependencies=[Depends(require_token)])
    def pellets(stove_id: str) -> dict:
        svc: StoveService = app.state.service
        snap = svc.latest.get(stove_id)
        if snap is None:
            raise HTTPException(status_code=404, detail="Poêle inconnu")
        refills = planner.refills(stove_id)
        now_ts = time.time()
        since = min((r["ts"] for r in refills), default=now_ts)
        samples = svc.history.since(stove_id, since)
        return stats.pellet_level(refills, samples, settings.pellet_capacity_kg, now_ts, snap.power_level, snap.running)

    @app.post("/api/stoves/{stove_id}/pellets/refill", dependencies=[Depends(require_token)])
    def refill(stove_id: str, body: RefillIn) -> dict:
        if stove_id not in app.state.service.latest:
            raise HTTPException(status_code=404, detail="Poêle inconnu")
        guard(lambda: planner.add_refill(stove_id, time.time(), kg=body.kg, full=body.full))
        return {"ok": True}

    @app.delete("/api/stoves/{stove_id}/pellets/refill/last", dependencies=[Depends(require_token)])
    def undo_refill(stove_id: str) -> dict:
        if not planner.undo_last_refill(stove_id):
            raise HTTPException(status_code=404, detail="Aucun remplissage à annuler")
        return {"ok": True}

    @app.get("/api/stoves/{stove_id}/projection", dependencies=[Depends(require_token)])
    async def stove_projection(stove_id: str, threshold: float = 19.0) -> dict:
        """Température de la pièce sur les 12 prochaines heures (avec le planning, et sans rallumage) : quand rallumer ?"""
        snap = app.state.service.latest.get(stove_id)
        if snap is None:
            raise HTTPException(status_code=404, detail="Poêle inconnu")
        if not snap.ambient or snap.ambient <= 0:
            raise HTTPException(status_code=503, detail="Température de la pièce inconnue")
        if not 10 <= threshold <= 25:
            raise HTTPException(status_code=422, detail="Seuil entre 10 et 25 °C")
        return await projection_for(stove_id, threshold)

    async def projection_for(stove_id: str, threshold: float) -> dict:
        """Prévision 12 h (aussi utilisée par la garde de température) ; le poêle doit être connu et sa température lue."""
        svc: StoveService = app.state.service
        snap = svc.latest[stove_id]
        now = time.time()
        lit_since = off_since = None
        recent = svc.history.since(stove_id, now - 6 * 3600)
        for prev, cur in zip(recent, recent[1:]):
            if not prev["running"] and cur["running"]:
                lit_since = cur["ts"]
            elif prev["running"] and not cur["running"]:
                off_since = cur["ts"]
        cooling = (await insight_body(stove_id)).get("cooling") or {}
        hourly, weather_error = await outdoor_hourly()
        model = anticipator.model()
        state = projection.State(snap.ambient, snap.running, snap.setpoint or 21.0,
                                 lit_since if snap.running else None, off_since if not snap.running else None)
        events = planner.upcoming(datetime.now(tz), limit=20)
        out = projection.advise(now, state, events, hourly, cooling.get("k"), model.startup_min, model.rate_per_min, threshold)
        return {**out, "weather_error": weather_error, "outdoor_now": projection.outdoor_at(hourly, now)}

    # ---- Notifications (Web Push) ------------------------------------------------------

    @app.get("/api/push/key", dependencies=[Depends(require_token)])
    def push_key() -> dict:
        return {"key": notifier.ensure_key(), "devices": len(notifier.subscriptions())}

    @app.post("/api/push/subscriptions", dependencies=[Depends(require_token)])
    def push_subscribe(body: SubscriptionIn) -> dict:
        guard(lambda: notifier.subscribe(body.endpoint, body.keys.get("p256dh", ""), body.keys.get("auth", ""), body.label))
        return {"ok": True, "devices": len(notifier.subscriptions())}

    @app.post("/api/push/unsubscribe", dependencies=[Depends(require_token)])
    def push_unsubscribe(body: EndpointIn) -> dict:
        notifier.unsubscribe(body.endpoint or "")
        return {"ok": True, "devices": len(notifier.subscriptions())}

    @app.post("/api/push/test", dependencies=[Depends(require_token)])
    async def push_test(body: EndpointIn) -> dict:
        """Notification d'essai, vers cet appareil seulement (`endpoint`) ou vers tous."""
        if body.endpoint and not any(s["endpoint"] == body.endpoint for s in notifier.subscriptions()):
            raise HTTPException(status_code=404, detail="Cet appareil n'est pas abonné : réactive les notifications")
        return await notifier.send("test", "Notifications du poêle", "Ça marche : les alertes arriveront ici.", only=body.endpoint, force=True)

    @app.get("/api/settings/notify", dependencies=[Depends(require_token)])
    def get_notify() -> dict:
        return notifier.prefs()

    @app.put("/api/settings/notify", dependencies=[Depends(require_token)])
    def set_notify(body: dict[str, bool] = Body(...)) -> dict:
        return notifier.set_prefs(body)

    # ---- Web app (mêmes fonctions que l'app iPhone), servie à la racine -------------------
    web = Path(__file__).parent / "web"

    @app.get("/sw.js", include_in_schema=False)
    def service_worker() -> FileResponse:
        """Service worker : reçoit les notifications (aucune mise en cache de la page)."""
        return FileResponse(web / "sw.js", media_type="text/javascript", headers={"Cache-Control": "no-cache"})

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(web / "index.html", headers={"Cache-Control": "no-cache"})

    @app.get("/manifest.webmanifest", include_in_schema=False)
    def manifest() -> FileResponse:
        return FileResponse(web / "manifest.webmanifest", media_type="application/manifest+json")

    @app.get("/icon.svg", include_in_schema=False)
    def icon() -> FileResponse:
        return FileResponse(web / "icon.svg", media_type="image/svg+xml")

    return app
