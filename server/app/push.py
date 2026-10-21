"""Notifications sur le téléphone (Web Push) : la web app installée s'abonne, le serveur envoie.

La clé VAPID est créée par le serveur au premier lancement et gardée dans la base SQLite (volume du conteneur) :
rien à saisir. Chaque appareil abonné est une ligne de `push_subs` ; un abonnement que le service de push déclare
expiré (404 / 410) est supprimé. Quels événements notifier : réglage global « notify » (voir PREFS).
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import sqlite3
import time
from pathlib import Path
from typing import Callable

log = logging.getLogger("mcz.push")

# Événements notifiables et leur valeur par défaut.
PREFS = {
    "alarm": True,      # le poêle se met en alarme
    "failed": True,     # allumage / extinction automatique non confirmé
    "guard": True,      # la garde de température rallume ou éteint
    "cold": True,       # la pièce passe sous le seuil, poêle éteint (heures de la garde)
    "offline": True,    # le poêle ne répond plus depuis 30 min
    "fumes": True,      # fumées nettement plus chaudes : nettoyage conseillé
    "pellet": True,     # réservoir bas (capteur du poêle)
    "external": False,  # allumage / extinction fait ailleurs (Maestro, bouton, télécommande)
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS push_subs (endpoint TEXT PRIMARY KEY, p256dh TEXT NOT NULL, auth TEXT NOT NULL,
    label TEXT NOT NULL DEFAULT '', created REAL NOT NULL);
"""


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _default_sender(sub: dict, payload: str, private_pem: bytes, subject: str) -> int | tuple[int, str]:
    """Envoi réel (pywebpush, bloquant) : rend le code HTTP du service de push, et sa raison en cas de refus."""
    from py_vapid import Vapid02
    from pywebpush import WebPushException, webpush
    try:
        resp = webpush(sub, payload, vapid_private_key=Vapid02.from_pem(private_pem), vapid_claims={"sub": subject},
                       ttl=12 * 3600, timeout=10, headers={"Urgency": "high"})
        return resp.status_code
    except WebPushException as err:
        if err.response is not None:
            return err.response.status_code, (err.response.text or "")[:200]   # Apple : {"reason":"BadJwtToken"}…
        raise


class Notifier:
    def __init__(self, path: str, subject: str, get_setting: Callable[[str], dict | None], set_setting: Callable[[str, dict], None],
                 sender: Callable[[dict, str, bytes, str], int] = _default_sender) -> None:
        """`get_setting` / `set_setting` : réglages du planificateur (« notify » pour les préférences, « vapid » pour la clé)."""
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.executescript(SCHEMA)
        self.subject, self._sender = subject, sender
        self._get, self._set = get_setting, set_setting
        self._tasks: set[asyncio.Task] = set()
        self._pem: bytes | None = None
        self.public_key = ""

    # ---- Clé du serveur -------------------------------------------------------------

    def ensure_key(self) -> str:
        """Clé publique (applicationServerKey) ; la paire est créée une fois puis gardée en base."""
        if self._pem is None:
            from cryptography.hazmat.primitives import serialization
            from cryptography.hazmat.primitives.asymmetric import ec
            saved = self._get("vapid")
            if saved:
                private = serialization.load_pem_private_key(saved["pem"].encode(), password=None)
            else:
                private = ec.generate_private_key(ec.SECP256R1())
                pem = private.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                            serialization.NoEncryption()).decode()
                self._set("vapid", {"pem": pem})
            self._pem = private.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                              serialization.NoEncryption())
            self.public_key = b64url(private.public_key().public_bytes(serialization.Encoding.X962,
                                                                       serialization.PublicFormat.UncompressedPoint))
        return self.public_key

    # ---- Appareils abonnés ----------------------------------------------------------

    def subscribe(self, endpoint: str, p256dh: str, auth: str, label: str = "") -> None:
        if not endpoint.startswith("https://") or len(endpoint) > 2048 or not p256dh or not auth:
            raise ValueError("abonnement invalide")
        self._db.execute("INSERT OR REPLACE INTO push_subs (endpoint, p256dh, auth, label, created) VALUES (?,?,?,?,?)",
                         (endpoint, p256dh, auth, label.strip()[:60], time.time()))
        self._db.commit()

    def unsubscribe(self, endpoint: str) -> bool:
        cur = self._db.execute("DELETE FROM push_subs WHERE endpoint=?", (endpoint,))
        self._db.commit()
        return cur.rowcount > 0

    def subscriptions(self) -> list[dict]:
        return [dict(r) for r in self._db.execute("SELECT endpoint, p256dh, auth, label, created FROM push_subs ORDER BY created")]

    def prefs(self) -> dict:
        return {**PREFS, **{k: bool(v) for k, v in (self._get("notify") or {}).items() if k in PREFS}}

    def set_prefs(self, changes: dict) -> dict:
        self._set("notify", {**self.prefs(), **{k: bool(v) for k, v in changes.items() if k in PREFS}})
        return self.prefs()

    # ---- Envoi ------------------------------------------------------------------------

    async def send(self, kind: str, title: str, body: str = "", only: str | None = None, force: bool = False) -> dict:
        """Envoie à tous les appareils (ou au seul `only`) si l'événement est coché. Rend {sent, failed}."""
        if not force and not self.prefs().get(kind, False):
            return {"sent": 0, "failed": 0, "skipped": True}
        self.ensure_key()
        payload = json.dumps({"title": title, "body": body, "tag": kind, "url": "/"})
        sent = failed = 0
        errors: list[str] = []                       # pourquoi ça n'est pas parti : affiché par l'essai, écrit dans les logs
        for sub in self.subscriptions():
            if only and sub["endpoint"] != only:
                continue
            info = {"endpoint": sub["endpoint"], "keys": {"p256dh": sub["p256dh"], "auth": sub["auth"]}}
            try:
                status = await asyncio.to_thread(self._sender, info, payload, self._pem, self.subject)
            except Exception as err:                 # réseau : on n'insiste pas, l'événement suivant repartira
                log.warning("notification %s non envoyée : %s", kind, err)
                errors.append(f"{type(err).__name__} : {err}"[:200])
                failed += 1
                continue
            status, reason = status if isinstance(status, tuple) else (status, "")
            if status in (404, 410):                 # appareil désabonné ou abonnement expiré
                self.unsubscribe(sub["endpoint"])
                errors.append(f"HTTP {status} : abonnement expiré, réactive les notifications")
                failed += 1
            elif status >= 400:
                log.warning("notification %s refusée : HTTP %s %s", kind, status, reason)
                errors.append(f"HTTP {status} {reason}".strip())
                failed += 1
            else:
                sent += 1
        return {"sent": sent, "failed": failed, **({"errors": errors} if errors else {})}

    def emit(self, kind: str, title: str, body: str = "") -> None:
        """Depuis du code synchrone (relevé, journal) : l'envoi part en tâche de fond, sans rien bloquer."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        task = loop.create_task(self.send(kind, title, body))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def flush(self) -> None:
        """Attend les envois en cours (tests)."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)
