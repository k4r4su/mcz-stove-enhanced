#!/usr/bin/env python3
"""Sondes du cloud MCZ (hlapi) — à lancer soi-même, avec son propre compte.

Pourquoi : l'app Maestro+ parle à https://s.maestro.mcz.it (routes relevées dans l'APK).
Ce script ouvre une session avec TON compte, liste ton poêle et enregistre les réponses pour qu'on comprenne
les identifiants (modèle, capteurs, configurations) et le format de l'état.
 - `dump` : LECTURE SEULE (login + GET + la liste des poêles).
 - `command-test` : une seule écriture SANS EFFET (la consigne est renvoyée à sa valeur actuelle).

Confidentialité : le mot de passe est demandé en saisie masquée, jamais écrit ni passé en argument ;
le jeton n'est pas enregistré ; position GPS, e-mail et adresses sont masqués dans le fichier de sortie.

    python3 tools/mczcloud.py dump            # écrit captures/cloud-dump.json
    python3 tools/mczcloud.py dump --user moi@exemple.fr
    python3 tools/mczcloud.py command-test    # une seule commande sans effet, pour valider le format d'écriture
"""
from __future__ import annotations

import argparse
import getpass
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

BASE = "https://s.maestro.mcz.it/"
HLAPI = BASE + "hlapi/v1.0"
# Constante embarquée dans l'application (en-tête `tenantid` de toutes les requêtes) : identifie MCZ, pas toi.
TENANT_ID = "7c201fd8-42bd-4333-914d-0f5822070757"
MASKED_KEYS = {"latitude", "longitude", "location", "email", "username", "phonenumber", "ssid_wifi", "token", "firstname", "lastname"}
# Constantes du modèle « ST.EGO C.AIR MATIC 10 CORE R » (voir server/app/commands.py) — pas des données personnelles.
SET_AMB_TEMP_CONFIG = "875fb696-4af1-42da-bb78-ebe57c90fe5c"   # configuration « Set_amb_temp »
SET_AMB1_SENSOR = "b866d2a9-d0ab-4558-8367-910c52261be1"       # capteur `set_amb1` (consigne, 5–40 °C)
OUT = Path(__file__).resolve().parent.parent / "captures" / "cloud-dump.json"


def mask(value):
    """Remplace par « *** » toute valeur dont la clé est personnelle (récursif)."""
    if isinstance(value, dict):
        return {k: ("***" if k.lower() in MASKED_KEYS and v not in (None, "") else mask(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [mask(v) for v in value]
    return value


def call(method: str, url: str, token: str | None = None, body: dict | None = None):
    headers = {"tenantid": TENANT_ID, "Accept": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read().decode("utf-8", "replace")
            status = resp.status
    except urllib.error.HTTPError as err:
        raw = err.read().decode("utf-8", "replace")
        status = err.code
    except (urllib.error.URLError, TimeoutError) as err:
        return {"status": None, "error": str(err)}
    try:
        payload = json.loads(raw) if raw else None
    except json.JSONDecodeError:
        payload = raw[:500]
    return {"status": status, "body": payload}


def dump(user: str | None) -> int:
    user = (user or input("Identifiant MCZ (e-mail) : ")).strip()
    password = getpass.getpass("Mot de passe MCZ (invisible) : ")
    login = call("POST", HLAPI + "/Authorization/Login", body={"username": user, "password": password.strip()})
    del password
    result: dict = {"login": {"status": login.get("status")}}
    body = login.get("body")
    if login.get("status") != 200 or not isinstance(body, dict) or not body.get("Token"):
        result["login"]["body"] = mask(body)
        print("Connexion refusée ou réponse inattendue :", result["login"])
        _save(result)
        return 1
    token = body["Token"]
    result["login"]["fields"] = sorted(body.keys())
    print("Connexion OK.")

    nav = call("POST", HLAPI + "/Nav/FirstVisibleObjectsPaginated", token, {"Top": 50})
    result["stoves"] = mask(nav)
    stoves = []
    if isinstance(nav.get("body"), (dict, list)):
        items = nav["body"] if isinstance(nav["body"], list) else nav["body"].get("Items") or nav["body"].get("items") or []
        for item in items:
            node = item.get("Node", item) if isinstance(item, dict) else {}
            if node.get("Id"):
                stoves.append(node)
    print(f"{len(stoves)} poêle(s) trouvé(s).")

    for node in stoves:
        # Les identifiants contiennent des « § » : à encoder dans l'adresse.
        sid, model = (urllib.parse.quote(str(v), safe="") for v in (node["Id"], node.get("ModelId") or ""))
        entry = {}
        for name, path in {
            "appliance_status": f"mcz/v1.0/Appliance/{sid}/Status",
            "appliance_state": f"mcz/v1.0/Appliance/{sid}/State",
            "appliance_counters": f"mcz/v1.0/Appliance/{sid}/Counters",
            "appliance_alarms": f"mcz/v1.0/Appliance/{sid}/Alarms",
            "sensorset": f"hlapi/v1.0/SensorSet/{sid}",
            "properties": f"hlapi/v1.0/SensorSet/{sid}/DTPropertiesData",
            "model": f"hlapi/v1.0/Model/{model}",
            "model_configurations": f"hlapi/v1.0/Model/{model}/Configurations",
        }.items():
            entry[name] = mask(call("GET", BASE + path, token))
            print(f"  {name}: {entry[name].get('status')}")
        result.setdefault("details", {})[node["Id"]] = entry
    _save(result)
    return 0


def command_test(user: str | None) -> int:
    """Envoie UNE commande sans effet : la consigne de température est remise à sa valeur actuelle.

    Sert à confirmer le format exact du corps de `ActivateProgram`. Essaie deux variantes de `ConfigurationId`
    (identifiant seul, puis préfixé « locataire§…§ ») et s'arrête à la première acceptée.
    """
    user = (user or input("Identifiant MCZ (e-mail) : ")).strip()
    password = getpass.getpass("Mot de passe MCZ (invisible) : ")
    login = call("POST", HLAPI + "/Authorization/Login", body={"username": user, "password": password.strip()})
    del password
    token = (login.get("body") or {}).get("Token") if isinstance(login.get("body"), dict) else None
    if login.get("status") != 200 or not token:
        print("Connexion refusée :", login.get("status"))
        return 1
    nav = call("POST", HLAPI + "/Nav/FirstVisibleObjectsPaginated", token, {"Top": 50})
    items = nav["body"] if isinstance(nav.get("body"), list) else (nav.get("body") or {}).get("Items") or []
    node = next((i.get("Node", i) for i in items if isinstance(i, dict)), None)
    if not node:
        print("Aucun poêle trouvé.")
        return 1
    sid = urllib.parse.quote(node["Id"], safe="")
    status = call("GET", f"{BASE}mcz/v1.0/Appliance/{sid}/Status", token)["body"]
    current = status["set_amb1"]
    print(f"Consigne actuelle : {current} — elle sera renvoyée à l'identique.")
    tenant_prefix = f"{TENANT_ID}§00000000-0000-0000-0000-000000000000§"
    attempts = []
    for label, config_id in (("sans préfixe", SET_AMB_TEMP_CONFIG), ("avec préfixe", tenant_prefix + SET_AMB_TEMP_CONFIG)):
        body = {"ModelId": node["ModelId"], "ConfigurationId": config_id, "SensorSetTypeId": node["SensorSetTypeId"],
                "Commands": [{"SensorId": SET_AMB1_SENSOR, "Value": float(current)}]}
        reply = call("POST", f"{BASE}mcz/v1.0/Program/ActivateProgram/{sid}", token, body)
        attempts.append({"variante": label, "reponse": mask(reply)})
        print(f"  ConfigurationId {label} → HTTP {reply.get('status')}")
        if reply.get("status") and reply["status"] < 300:
            break
    after = call("GET", f"{BASE}mcz/v1.0/Appliance/{sid}/Status", token)["body"]
    attempts.append({"consigne_apres": after.get("set_amb1"), "StatusTimestamp": after.get("StatusTimestamp")})
    _save({"command_test": attempts}, OUT.with_name("command-test.json"))
    return 0


def _save(result: dict, out: Path = OUT) -> None:
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(result, indent=2, ensure_ascii=False))
    print("Écrit :", out)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("dump", help="login + lectures seules, résultat masqué dans captures/")
    d.add_argument("--user", help="identifiant (sinon demandé)")
    c = sub.add_parser("command-test", help="UNE commande sans effet (consigne = consigne actuelle) pour valider le format")
    c.add_argument("--user", help="identifiant (sinon demandé)")
    args = parser.parse_args(argv)
    return command_test(args.user) if args.cmd == "command-test" else dump(args.user)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
