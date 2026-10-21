"""Prévisions météo (Open-Meteo : gratuit, sans compte) à 7 h, 12 h et 18 h, pour décider d'allumer ou non le poêle.

Seule la position (latitude/longitude) quitte le container, vers api.open-meteo.com. Elle vient de WEATHER_LAT/WEATHER_LON si définies,
sinon de la fiche du poêle dans le cloud MCZ.
"""
from __future__ import annotations

import math
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx

SLOT_HOURS = (7, 12, 18)
HEATING_BELOW = 15.0      # sous cette température extérieure, un chauffage est en général utile
CACHE_SECONDS = 1800
DAY_NAMES = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]
MONTHS = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août", "septembre", "octobre", "novembre", "décembre"]
FORECAST_DAYS = 8   # aujourd'hui + 7 jours (onglet Planning)
PAST_DAYS = 31      # météo passée : chauffe comparée à la météo, refroidissement de la maison
PAST_CACHE_SECONDS = 3600


def day_label(day, offset: int) -> str:
    base = f"{DAY_NAMES[day.weekday()]} {day.day} {MONTHS[day.month - 1]}"
    return ("Aujourd'hui" if offset == 0 else "Demain" if offset == 1 else base[0].upper() + base[1:])


def distance_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 6371 * 2 * math.asin(math.sqrt(h))


class WeatherError(RuntimeError):
    pass


def advice(temps: list[float]) -> str:
    """Phrase d'aide à la décision à partir des températures des trois créneaux (7 h, 12 h, 18 h)."""
    cold = [t < HEATING_BELOW for t in temps]
    if all(cold):
        return "Chauffage utile toute la journée"
    if not any(cold):
        return "Pas de chauffage nécessaire"
    if cold[0] and cold[2] and not cold[1]:
        return "Chauffage utile le matin et le soir"
    if cold[0] and not cold[2]:
        return "Chauffage utile surtout le matin"
    if cold[2] and not cold[0]:
        return "Chauffage utile surtout le soir"
    return "Chauffage utile par moments"


def parse(payload: dict, tz: ZoneInfo, now: datetime) -> list[dict]:
    hourly = payload.get("hourly") or {}
    times = hourly.get("time") or []
    temps = hourly.get("temperature_2m") or []
    feels = hourly.get("apparent_temperature") or []
    codes = hourly.get("weather_code") or []
    index = {t: i for i, t in enumerate(times)}
    out = []
    for offset in range(FORECAST_DAYS):
        day = (now.astimezone(tz) + timedelta(days=offset)).date()
        label = day_label(day, offset)
        slots = []
        for hour in SLOT_HOURS:
            i = index.get(f"{day.isoformat()}T{hour:02d}:00")
            if i is None or i >= len(temps) or temps[i] is None:
                continue
            slots.append({"hour": hour, "temp": round(float(temps[i]), 1),
                          "feels_like": round(float(feels[i]), 1) if i < len(feels) and feels[i] is not None else None,
                          "code": int(codes[i]) if i < len(codes) and codes[i] is not None else None})
        daily = payload.get("daily") or {}
        dates = daily.get("time") or []
        lo = hi = None
        if day.isoformat() in dates:
            j = dates.index(day.isoformat())
            lo = daily.get("temperature_2m_min", [None] * (j + 1))[j]
            hi = daily.get("temperature_2m_max", [None] * (j + 1))[j]
        if not slots:
            continue
        out.append({"date": day.isoformat(), "label": label, "full_label": day_label(day, 2), "min": lo, "max": hi, "slots": slots,
                    "advice": advice([s["temp"] for s in slots]) if len(slots) == len(SLOT_HOURS) else None})
    return out


class Weather:
    def __init__(self, tz: ZoneInfo, lat: float | None, lon: float | None, location_lookup,
                 client: httpx.AsyncClient | None = None, clock=time.time, saved_position=lambda: None) -> None:
        self.tz, self._lat, self._lon, self._lookup = tz, lat, lon, location_lookup
        self._saved = saved_position          # position choisie dans l'app (ville ou iPhone) : prioritaire
        self._client = client or httpx.AsyncClient(timeout=15)
        self._clock = clock
        self._cache: tuple[float, dict] | None = None
        self._past: tuple[float, dict] | None = None

    def invalidate(self) -> None:
        self._cache = self._past = None

    async def hourly_temps(self, days: int = PAST_DAYS) -> dict[float, float]:
        """Température extérieure heure par heure, des `days` derniers jours à demain soir (époque UNIX → °C) :
        indicateurs (passé) et prévision de la température de la pièce (futur)."""
        now = self._clock()
        if self._past and now - self._past[0] < PAST_CACHE_SECONDS:
            return self._past[1]
        lat, lon, _ = await self._position()
        try:
            resp = await self._client.get("https://api.open-meteo.com/v1/forecast", params={
                "latitude": f"{lat:.4f}", "longitude": f"{lon:.4f}", "timezone": "UTC", "past_days": min(92, days),
                "forecast_days": 2, "hourly": "temperature_2m"})
        except httpx.HTTPError as err:
            raise WeatherError(f"service météo injoignable : {err}") from err
        if resp.status_code != 200:
            raise WeatherError(f"service météo : HTTP {resp.status_code}")
        hourly = resp.json().get("hourly") or {}
        out = {}
        for t, v in zip(hourly.get("time") or [], hourly.get("temperature_2m") or []):
            ts = datetime.fromisoformat(t).replace(tzinfo=ZoneInfo("UTC")).timestamp()
            if v is not None:
                out[ts] = float(v)
        self._past = (now, out)
        return out

    async def _position(self) -> tuple[float, float, str]:
        saved = self._saved()
        if saved:
            return float(saved["lat"]), float(saved["lon"]), f"choisie dans l'app : {saved.get('name') or 'position'}"
        if self._lat is not None and self._lon is not None:
            return self._lat, self._lon, "configurée (WEATHER_LAT/WEATHER_LON)"
        try:
            lat, lon = await self._lookup()
        except Exception as err:
            raise WeatherError(f"position du poêle introuvable ({err}) : définis WEATHER_LAT et WEATHER_LON") from err
        return lat, lon, "fiche du poêle (cloud MCZ)"

    async def forecast(self) -> dict:
        now = self._clock()
        if self._cache and now - self._cache[0] < CACHE_SECONDS:
            return self._cache[1]
        lat, lon, source = await self._position()
        self.last_position = (lat, lon, source)
        try:
            resp = await self._client.get("https://api.open-meteo.com/v1/forecast", params={
                "latitude": f"{lat:.4f}", "longitude": f"{lon:.4f}", "timezone": self.tz.key, "forecast_days": FORECAST_DAYS + 1,
                "hourly": "temperature_2m,apparent_temperature,weather_code", "daily": "temperature_2m_min,temperature_2m_max"})
        except httpx.HTTPError as err:
            raise WeatherError(f"service météo injoignable : {err}") from err
        if resp.status_code != 200:
            raise WeatherError(f"service météo : HTTP {resp.status_code}")
        result = {"source": source, "days": parse(resp.json(), self.tz, datetime.fromtimestamp(now, self.tz))}
        self._cache = (now, result)
        return result
