"""Client du cloud MCZ (API `hlapi`, relevée dans l'APK de Maestro+)."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from .config import Settings


class CloudError(RuntimeError):
    """Réponse inattendue ou refus du cloud MCZ."""


class MczCloud:
    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self._settings = settings
        self._client = client or httpx.AsyncClient(base_url=settings.base_url, timeout=20)
        self._token: str | None = None
        self._expires: datetime = datetime.min.replace(tzinfo=timezone.utc)
        self._login_lock = asyncio.Lock()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _login(self) -> None:
        async with self._login_lock:
            if self._token and datetime.now(timezone.utc) < self._expires - timedelta(minutes=5):
                return
            resp = await self._client.post(
                "hlapi/v1.0/Authorization/Login",
                headers={"tenantid": self._settings.tenant_id},
                json={"username": self._settings.mcz_user.strip(), "password": self._settings.mcz_password.strip()},
            )
            body = resp.json() if resp.content else {}
            if resp.status_code != 200 or "Token" not in body:
                # On ne recopie que le motif du refus, jamais le corps entier (il peut citer l'identifiant).
                reason = ""
                try:
                    reason = " : " + str(body["Errors"][0]["ErrorMessage"])
                except (KeyError, IndexError, TypeError):
                    pass
                raise CloudError(f"Connexion refusée par le cloud MCZ (HTTP {resp.status_code}{reason})")
            self._token = body["Token"]
            self._expires = _parse_expiry(body.get("ExpirationDateTime"))

    async def request(self, method: str, path: str, json: Any = None) -> Any:
        for attempt in (1, 2):
            await self._login()
            resp = await self._client.request(
                method, path, json=json,
                headers={"tenantid": self._settings.tenant_id, "Authorization": f"Bearer {self._token}"},
            )
            if resp.status_code == 401 and attempt == 1:
                self._token = None  # jeton refusé : une seule nouvelle connexion
                continue
            if resp.status_code >= 400:
                reason = ""
                try:   # motif donné par le cloud (jamais le corps entier)
                    reason = " : " + str(resp.json()["Errors"][0]["ErrorMessage"])
                except Exception:
                    pass
                raise CloudError(f"{method} {path.split('?')[0].rsplit('/', 1)[0]} → HTTP {resp.status_code}{reason}")
            return resp.json() if resp.content else None
        raise CloudError("unreachable")

    async def list_stoves(self) -> list[dict]:
        body = await self.request("POST", "hlapi/v1.0/Nav/FirstVisibleObjectsPaginated", json={"Top": 50})
        items = body if isinstance(body, list) else (body or {}).get("Items") or (body or {}).get("items") or []
        return [item.get("Node", item) for item in items if isinstance(item, dict)]

    async def status(self, stove_id: str) -> dict:
        return await self.request("GET", f"mcz/v1.0/Appliance/{stove_id}/Status")

    async def location(self, stove_id: str) -> tuple[float, float]:
        """Position du poêle (fiche SensorSet du cloud MCZ). Jamais journalisée."""
        sheet = await self.request("GET", f"hlapi/v1.0/SensorSet/{stove_id}")
        try:
            return float(sheet["Latitude"]), float(sheet["Longitude"])
        except (KeyError, TypeError, ValueError):
            raise CloudError("la fiche du poêle n'a pas de position")

    async def chrono(self, stove_id: str):
        """Programme interne du poêle (app Maestro+), lecture seule."""
        return await self.request("GET", f"mcz/v1.0/Crono/{stove_id}")

    async def ping(self, stove_id: str) -> None:
        """« Ping » de l'app Maestro+ : réveille le module Wi-Fi du poêle (l'app attend 2 s avant de relire l'état)."""
        await self.request("POST", f"mcz/v1.0/Program/Ping/{stove_id}")

    async def detail(self, stove_id: str) -> dict:
        return await self.request("GET", f"mcz/v1.0/Appliance/{stove_id}/State")


    async def activate(self, stove: dict, config_id: str, sensor_id: str, value) -> None:
        """Envoie un réglage (`ActivateProgram`). « Command successfully activated » = accepté par le cloud, pas encore exécuté par le poêle."""
        body = {
            "ModelId": stove["ModelId"],
            "ConfigurationId": config_id,
            "SensorSetTypeId": stove["SensorSetTypeId"],
            "Commands": [{"SensorId": sensor_id, "Value": value}],
        }
        await self.request("POST", f"mcz/v1.0/Program/ActivateProgram/{stove['Id']}", json=body)


def _parse_expiry(value: str | None) -> datetime:
    if not value:
        return datetime.now(timezone.utc) + timedelta(minutes=30)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(timezone.utc) + timedelta(minutes=30)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)

