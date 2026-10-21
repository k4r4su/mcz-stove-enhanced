#!/usr/bin/env python3
"""Sonde MCZ en LECTURE SEULE (aucune dépendance, Python 3 standard).

  python3 tools/mczprobe.py scan [192.168.1]        cherche, sur TON réseau local, les machines qui écoutent sur le port 81
  python3 tools/mczprobe.py info <ip> [--raw]       envoie `C|RecuperoInfo` et décode la trame d'état

N'envoie JAMAIS de commande d'écriture (WriteParametri, Diagnostica, SalvaDataOra…).
"""
import base64
import concurrent.futures
import os
import socket
import struct
import sys

PORT = 81

STOVE_STATES = {
    0: "Éteint", 1: "Contrôle chaud/froid", 2: "Nettoyage à froid", 3: "Chargement pellets à froid", 4: "Allumage 1 à froid",
    5: "Allumage 2 à froid", 6: "Nettoyage à chaud", 7: "Chargement pellets à chaud", 8: "Allumage 1 à chaud", 9: "Allumage 2 à chaud",
    10: "Stabilisation", 11: "Puissance 1", 12: "Puissance 2", 13: "Puissance 3", 14: "Puissance 4", 15: "Puissance 5",
    30: "Diagnostic", 31: "Allumé", 40: "Extinction", 41: "Refroidissement", 42: "Nettoyage bas", 43: "Nettoyage haut",
    44: "Déblocage vis", 45: "Auto Eco", 46: "Standby", 48: "Diagnostic", 49: "Chargement vis",
}
# index dans la trame -> (nom, type) ; type : int | temp (valeur / 2) | timespan (secondes)
INFO_FIELDS = {
    1: ("etat", "int"), 2: ("ventilateur", "int"), 3: ("ventilateur_canalise_1", "int"), 4: ("ventilateur_canalise_2", "int"),
    5: ("temperature_fumees", "int"), 6: ("temperature_ambiante", "temp"), 7: ("temperature_tampon", "temp"),
    8: ("temperature_chaudiere", "temp"), 11: ("active_set", "int"), 12: ("tr_min_extracteur", "int"),
    17: ("brasero", "int"), 18: ("profil", "int"), 20: ("active_mode", "int"), 23: ("eco", "int"), 24: ("silence", "int"),
    25: ("chronostat", "int"), 26: ("consigne_temperature", "temp"), 27: ("consigne_chaudiere", "temp"),
    28: ("temperature_carte_mere", "temp"), 29: ("niveau_puissance", "int"), 30: ("firmware", "int"),
    37: ("heures_totales_s", "int"), 43: ("heures_avant_entretien", "int"), 44: ("minutes_avant_arret", "int"),
    45: ("nombre_allumages", "int"), 47: ("capteur_pellets", "int"),
}


def decode_info(frame: str) -> dict:
    """Décode une trame `01|…` (champs en hexadécimal) ; ignore les champs inconnus ou illisibles."""
    parts = frame.split("|")
    if parts[0] != "01":
        raise ValueError(f"pas une trame d'état (type {parts[0]!r})")
    out = {}
    for index, raw in enumerate(parts):
        if index not in INFO_FIELDS:
            continue
        try:
            value = int(raw, 16)
        except ValueError:
            continue
        name, kind = INFO_FIELDS[index]
        out[name] = value / 2 if kind == "temp" else value
    if "etat" in out:
        out["etat_libelle"] = STOVE_STATES.get(out["etat"], f"inconnu ({out['etat']})" if out["etat"] < 50 else f"alarme/erreur ({out['etat']})")
    return out


# --- client WebSocket minimal (RFC 6455, texte, sans extension) ---------------------------------------------------
def _recv_exact(sock: socket.socket, count: int) -> bytes:
    data = b""
    while len(data) < count:
        chunk = sock.recv(count - len(data))
        if not chunk:
            raise ConnectionError("connexion fermée")
        data += chunk
    return data


def ws_connect(host: str, port: int = PORT, timeout: float = 5.0) -> socket.socket:
    sock = socket.create_connection((host, port), timeout=timeout)
    key = base64.b64encode(os.urandom(16)).decode()
    sock.sendall((f"GET / HTTP/1.1\r\nHost: {host}:{port}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                  f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
    response = b""
    while b"\r\n\r\n" not in response:
        chunk = sock.recv(1024)
        if not chunk:
            raise ConnectionError("pas de réponse à la poignée de main")
        response += chunk
    if b" 101 " not in response.split(b"\r\n")[0]:
        raise ConnectionError("poignée de main refusée : " + response.split(b"\r\n")[0].decode(errors="replace"))
    return sock


def ws_send_text(sock: socket.socket, text: str) -> None:
    payload = text.encode()
    header = bytes([0x81])
    if len(payload) < 126:
        header += bytes([0x80 | len(payload)])
    else:
        header += bytes([0x80 | 126]) + struct.pack(">H", len(payload))
    mask = os.urandom(4)
    sock.sendall(header + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))


def ws_recv_text(sock: socket.socket) -> str:
    first, second = _recv_exact(sock, 2)
    length = second & 0x7F
    if length == 126:
        length = struct.unpack(">H", _recv_exact(sock, 2))[0]
    elif length == 127:
        length = struct.unpack(">Q", _recv_exact(sock, 8))[0]
    mask = _recv_exact(sock, 4) if second & 0x80 else None
    payload = _recv_exact(sock, length)
    if mask:
        payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    opcode = first & 0x0F
    if opcode == 0x8:
        raise ConnectionError("le poêle a fermé la connexion")
    return payload.decode(errors="replace")


# --- commandes -------------------------------------------------------------------------------------------------
def scan(prefix: str) -> None:
    def check(host: str):
        try:
            with socket.create_connection((host, PORT), timeout=0.6):
                return host
        except OSError:
            return None

    hosts = [f"{prefix}.{n}" for n in range(1, 255)]
    with concurrent.futures.ThreadPoolExecutor(max_workers=64) as pool:
        found = [h for h in pool.map(check, hosts) if h]
    print("Port 81 ouvert sur :", ", ".join(found) if found else "aucune machine (poêle sur un autre réseau ? éteint ?)")


def info(host: str, raw: bool) -> None:
    sock = ws_connect(host)
    try:
        ws_send_text(sock, "C|RecuperoInfo")  # lecture seule
        for _ in range(6):  # le poêle peut envoyer d'autres trames avant celle d'état
            frame = ws_recv_text(sock)
            if raw:
                print("trame brute :", frame)
            if frame.startswith("01|"):
                for name, value in decode_info(frame).items():
                    print(f"{name:26} {value}")
                return
        print("aucune trame d'état reçue")
    finally:
        sock.close()


if __name__ == "__main__":
    args = sys.argv[1:]
    if args[:1] == ["scan"] and len(args) in (1, 2):
        prefix = args[1] if len(args) == 2 else ".".join(socket.gethostbyname(socket.gethostname()).split(".")[:3])
        scan(prefix)
    elif args[:1] == ["info"] and len(args) >= 2:
        info(args[1], "--raw" in args)
    else:
        print(__doc__)
        sys.exit(2)
