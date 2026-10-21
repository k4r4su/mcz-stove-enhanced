# MCZ Stove Enhanced

Pilotage d'un poêle à granulés **MCZ Maestro**, auto-hébergé : rapide, fiable, avec une vraie programmation.

> *English: self-hosted controller for MCZ Maestro pellet stoves. A Docker service keeps a session with the MCZ cloud (the one the
> Maestro+ app uses), confirms every command against the stove's reported state, runs minute-precise schedules and serves a mobile
> web app. The interface and docs are in French.*

L'application officielle met longtemps à se connecter et perd parfois des commandes. Ici, un petit serveur (Docker) garde la session
avec le cloud MCZ, affiche l'état tout de suite et **vérifie chaque commande dans l'état renvoyé par le poêle** avant de dire « fait ».

## Ce que ça fait

- **Pilotage confirmé** : marche/arrêt, consigne, puissance, mode. Chaque commande est vérifiée et renvoyée au besoin, et un
  marche/arrêt (une bascule) n'est jamais envoyé à l'aveugle.
- **Programmation** à la minute, exécutée par le serveur même téléphone éteint :
  - plages hebdomadaires, avec un **allumage anticipé** (« chaud à 7 h ») qui apprend le temps de chauffe de ta maison ;
  - minuteries (« éteindre à 22:30 »), jours sautés, pause et vacances avec préchauffage avant le retour.
- **Garde de température** : rallume le poêle avant que la pièce ne passe sous un seuil, aux heures choisies.
- **Courbes et indicateurs** :
  - température de la pièce, avec la température extérieure ;
  - prévision de la pièce sur 12 h ;
  - heures de chauffe comparées à la météo ;
  - refroidissement de la maison, confort, encrassement (température des fumées) ;
  - pellets restants (estimation) et coût.
- **Journal** de qui a fait quoi, y compris les changements faits depuis l'app Maestro+ ou sur le poêle.
- **Notifications** (Web Push, iPhone compris) : alarme, allumage raté, pièce froide, poêle injoignable, réservoir bas…
- **Apple Maison** (facultatif) : le poêle comme thermostat, via `homekit/`.
- **Prometheus** : métriques sur `/metrics`.

## Installation

Voir **[docs/installation.md](docs/installation.md)** : identifiants à préparer, installation simple sur le réseau local, version HTTPS
avec Traefik, Portainer, premiers réglages.

En bref :

```sh
cd server && cp .env.example .env    # MCZ_USER / MCZ_PASSWORD (compte Maestro+), BRIDGE_TOKEN, WEB_USER / WEB_PASSWORD
docker compose up -d --build         # puis http://<ip-de-la-machine>:8000
```

## Organisation du dépôt

| Dossier | Contenu |
|---|---|
| `server/` | mcz-bridge : FastAPI + SQLite, cloud MCZ, planificateur, garde, notifications. La web app est `server/app/web/index.html` (une page, sans dépendance). |
| `homekit/` | Pont Apple Maison (HAP-python), qui parle à mcz-bridge et jamais au cloud. |
| `tools/` | `mczcloud.py` : relevé en lecture seule de ton compte et de ton modèle de poêle (données personnelles masquées). `mczprobe.py` : sonde des anciens modules Wi-Fi locaux. |
| `docs/` | Guide d'installation. |

## Développer

```sh
cd server
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
.venv/bin/python dev_web.py          # web app avec un faux poêle : http://localhost:8765/?token=dev
```

## Avertissement

Projet personnel non officiel, sans lien avec MCZ. Il utilise le service cloud de l'application Maestro+ avec ton propre compte, et
peut cesser de fonctionner si MCZ le modifie. Vérifié sur un MCZ Ego Comfort Air 10 Core (R) : sur un autre modèle, vérifie d'abord les
identifiants de commande (docs/installation.md §7). Tu restes responsable de ce que fait ton poêle.

## Licence

[MIT](LICENSE) : libre d'utiliser, modifier et redistribuer, sans garantie.
