# mcz-bridge

Container qui garde une session ouverte avec le cloud MCZ, met l'état du poêle en cache, enregistre l'historique (SQLite) et sert la **web app**
(racine `/`), son API (`/api/…`) et Prometheus (`/metrics`). L'API demande une session ouverte dans la web app (`WEB_USER`/`WEB_PASSWORD`) ou
`Authorization: Bearer <BRIDGE_TOKEN>` (pont Apple Maison, scripts).

```
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
```

Déploiement : voir `docs/installation.md` et `.env.example`.

Commandes : `POST /api/stoves/{id}/commands` avec `{"kind": …, "value": …}` — `power` (true/false), `setpoint` (5–40, pas de 0,5), `power_level` (1–5),
`mode` (manual, auto, overnight, comfort), `sleep_timer` (0–480 min). Liste blanche : aucun réglage installateur n'est exposé.
Chaque commande est envoyée, vérifiée dans l'état du poêle (`/Status`), renvoyée jusqu'à 3 fois, puis la réponse dit `confirmed` ou `not_confirmed`.

Mot de passe MCZ avec un `$` : Docker Compose interprète le `$`. Donne-le alors en base64 dans `MCZ_PASSWORD_B64` (et laisse `MCZ_PASSWORD` vide) :
`read -s p; printf '%s' "$p" | base64; unset p` (saisie masquée, rien dans l'historique du terminal).

Programmation (heures à la minute près, fuseau `TIMEZONE`, défaut Europe/Paris) : plages hebdomadaires `GET/POST /api/plages`, `PUT/DELETE /api/plages/{id}`
(`{name, days:[0=lundi…6], start:"07:42", end:"09:30", setpoint?, mode?, enabled}`), minuteries `POST /api/stoves/{id}/timers` (`{action:"off", time:"22:30"}`,
prochaine occurrence), `DELETE /api/timers/{id}`. Statistiques `GET /api/stoves/{id}/stats?days=7`, pellets `GET /api/stoves/{id}/pellets` et
`POST …/pellets/refill` (`{kg:15}` ou `{full:true}`). Le planificateur vérifie toutes les 15 s, ne rallume jamais un poêle en alarme, et rejoue une commande
non confirmée jusqu'à 10 min (30 min pour une minuterie) après l'heure prévue.

Météo : `GET /api/weather` — températures à 7 h, 12 h et 18 h pour aujourd'hui et demain (Open-Meteo, gratuit, sans compte) et une phrase d'aide à la
décision (chauffage utile sous 15 °C). La position vient de `WEATHER_LAT`/`WEATHER_LON` si définies, sinon de la fiche du poêle dans le cloud MCZ ;
seule cette position est envoyée à api.open-meteo.com. Réponse mise en cache 30 minutes.

Web app (seule interface depuis le 14/10/2026) : `app/web/index.html` (une page, sans dépendance), servie à la racine `/` ; connexion par
identifiant / mot de passe (session de 90 j gardée dans le navigateur), ou saisie du jeton si `WEB_USER` n'est pas défini.
Installable sur l'écran d'accueil (manifeste). Démonstration locale : `dev_web.py`.

Journal « qui a fait quoi » : `GET /api/journal?days=7` — nos commandes (app, planning, vacances) avec leur issue (confirmé, déjà fait,
échec, sauté), les changements faits ailleurs (Maestro, bouton, télécommande : l'état change sans commande de notre part) et les alarmes. 90 jours.

Jours sautés : `PUT/DELETE /api/schedule/skips/{AAAA-MM-JJ}` (les allumages des plages de ce jour sont sautés, les plages ne changent pas).
Absence : `PUT /api/schedule/absence` `{kind:"pause", end_ts}` (aucun allumage de plage jusqu'à `end_ts`) ou
`{kind:"vacances", start_ts, end_ts, preheat_minutes:120, setpoint:21, off_at_start:true}` (extinction au départ, aucun allumage,
rallumage `preheat_minutes` avant le retour) ; `DELETE` l'annule. `GET /api/schedule/overview` renvoie aussi `skips`, `absence` et le modèle
d'allumage anticipé.

Allumage anticipé : une plage `anticipate:true` donne l'heure où il doit faire `setpoint` °C ; l'allumage part plus tôt (15 min à 3 h),
d'après la température de la pièce et ce que l'historique des allumages a appris (`app/anticipation.py`).

Indicateurs (`GET /api/stoves/{id}/insights`, `app/insights.py`, onglet Courbes) : coût (kg estimés × prix du sac,
`PUT /api/settings/pellet-price {bag_eur, bag_kg}`) sur 7 jours, le mois et la saison (depuis le 1er septembre) ; chauffe comparée
à la météo (kg par degré-jour base 18 °C, semaine contre semaine) ; refroidissement de la maison la nuit poêle éteint (loi de Newton,
températures extérieures passées d'Open-Meteo) ; confort (temps pour atteindre la consigne, part du temps où elle est tenue) ; fumées
à puissance égale (encrassement). Les heures de chauffe se fondent sur la phase enregistrée (`fase_op`), y compris pour les anciens relevés.

Prévision 12 h (`GET /api/stoves/{id}/projection?threshold=19`, `app/projection.py`) : température de la pièce simulée par pas de 5 min,
avec le planning et sans rallumage (perte k × (dedans − dehors) apprise la nuit, météo prévue, 1 h de chaleur résiduelle après
extinction, démarrage et montée appris). Donne l'heure de passage sous le seuil, l'heure conseillée pour rallumer (seuil − démarrage)
et si le prochain allumage prévu arrive à temps ; la web app propose d'en faire une minuterie.

Garde de température (`GET`/`PUT /api/settings/guard {enabled, threshold, start, end, after}`, `app/guard.py`) : chaque minute, aux heures
de garde, la prévision ci-dessus est refaite au seuil de la garde ; si la pièce va passer sous le seuil avant la fin de ces heures et
qu'aucun allumage prévu n'arrive à temps, le poêle est rallumé à l'heure conseillée (journal : origine `garde`). Ensuite il s'éteint à la
consigne après 1 h de chauffe au moins (`after:"setpoint"`) ou à la fin des heures de garde (`after:"window"`). Une plage en cours (allumage
anticipé compris) ou un allumage prévu dans l'heure reprend la main ; rien en pause, en vacances, un jour sauté, poêle en alarme ou injoignable.

Notifications (Web Push, `app/push.py`, service worker `/sw.js`) : la clé VAPID est créée au premier lancement et gardée en base (rien à
saisir) ; `PUSH_SUBJECT` (défaut de la stack : `https://${BRIDGE_HOST}`) est l'adresse annoncée aux services de push. `GET /api/push/key`,
`POST /api/push/subscriptions` (abonnement du navigateur), `POST /api/push/unsubscribe`, `POST /api/push/test {endpoint}`,
`GET`/`PUT /api/settings/notify` (événements cochés : alarme, allumage/extinction automatique non confirmé, garde, pièce sous le seuil
poêle éteint, poêle injoignable 30 min, réservoir bas, fumées +15 °C, changement fait ailleurs). Sur iPhone, il faut l'app ajoutée à l'écran
d'accueil (iOS 16.4 ou plus).
