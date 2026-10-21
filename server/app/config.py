"""Réglages lus dans l'environnement (fichier .env fourni par le propriétaire, jamais dans le dépôt)."""
from __future__ import annotations

import base64
import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    mcz_user: str
    mcz_password: str
    bridge_token: str
    base_url: str = "https://s.maestro.mcz.it/"
    tenant_id: str = "7c201fd8-42bd-4333-914d-0f5822070757"
    poll_seconds: float = 5.0
    db_path: str = "data/history.sqlite"
    timezone: str = "Europe/Paris"
    pellet_capacity_kg: float = 16.0
    weather_lat: float | None = None
    weather_lon: float | None = None
    web_user: str = ""          # identifiant de la web app (vide = connexion par jeton seulement)
    web_password: str = ""
    push_subject: str = "https://localhost"   # adresse du serveur, annoncée aux services de push (VAPID « sub »)

    @staticmethod
    def from_env() -> "Settings":
        def need(name: str) -> str:
            value = os.environ.get(name, "").strip()
            if not value:
                raise RuntimeError(f"Variable d'environnement manquante : {name}")
            return value

        # MCZ_PASSWORD_B64 : mot de passe encodé en base64, pour les mots de passe qui contiennent `$` (interprété par Docker Compose).
        encoded = os.environ.get("MCZ_PASSWORD_B64", "").strip()
        password = base64.b64decode(encoded).decode("utf-8") if encoded else need("MCZ_PASSWORD")
        return Settings(
            mcz_user=need("MCZ_USER"),
            mcz_password=password,
            bridge_token=need("BRIDGE_TOKEN"),
            base_url=os.environ.get("MCZ_BASE_URL", Settings.base_url),
            poll_seconds=float(os.environ.get("POLL_SECONDS", Settings.poll_seconds)),
            db_path=os.environ.get("DB_PATH", Settings.db_path),
            timezone=os.environ.get("TIMEZONE", Settings.timezone),
            pellet_capacity_kg=float(os.environ.get("PELLET_CAPACITY_KG", Settings.pellet_capacity_kg)),
            weather_lat=float(os.environ["WEATHER_LAT"]) if os.environ.get("WEATHER_LAT") else None,
            weather_lon=float(os.environ["WEATHER_LON"]) if os.environ.get("WEATHER_LON") else None,
            web_user=os.environ.get("WEB_USER", "").strip(),
            web_password=(base64.b64decode(os.environ["WEB_PASSWORD_B64"]).decode("utf-8") if os.environ.get("WEB_PASSWORD_B64", "").strip()
                          else os.environ.get("WEB_PASSWORD", "")),
            push_subject=os.environ.get("PUSH_SUBJECT", "").strip() or Settings.push_subject,
        )
