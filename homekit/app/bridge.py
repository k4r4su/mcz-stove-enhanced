"""Client du container mcz-bridge (même jeton que l'app iPhone)."""
from __future__ import annotations

import httpx


class BridgeError(RuntimeError):
    pass


class Bridge:
    def __init__(self, base_url: str, token: str, client: httpx.Client | None = None) -> None:
        self._client = client or httpx.Client(base_url=base_url.rstrip("/"), timeout=60,
                                              headers={"Authorization": f"Bearer {token}"})

    def stove(self) -> dict | None:
        resp = self._client.get("/api/stoves", timeout=10)
        if resp.status_code != 200:
            raise BridgeError(f"HTTP {resp.status_code}")
        stoves = resp.json().get("stoves", [])
        return stoves[0] if stoves else None

    def command(self, stove_id: str, kind: str, value) -> dict:
        resp = self._client.post(f"/api/stoves/{stove_id}/commands", json={"kind": kind, "value": value})
        if resp.status_code != 200:
            raise BridgeError(f"HTTP {resp.status_code} {resp.text[:120]}")
        return resp.json()
