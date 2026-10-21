# mcz-homekit

Publie le poêle dans Apple Maison comme **thermostat** (Éteint / Chauffage, température ambiante, consigne 5–40 °C par 0,5).
Il ne parle jamais au cloud MCZ : il passe par `mcz-bridge` (mêmes commandes confirmées et renvoyées). L'Apple TV sert de centrale pour le pilotage
hors de la maison.

- `network_mode: host` obligatoire (HomeKit trouve l'accessoire par mDNS sur le réseau local). Port TCP 51827 libre sur l'hôte.
- Variables : `BRIDGE_TOKEN` (le même que l'app), `BRIDGE_URL` (défaut `http://127.0.0.1:8001`, port local de mcz-bridge : évite de repasser par Internet et par un éventuel portail d'authentification), `HOMEKIT_NAME`, `HOMEKIT_PIN` (facultatif).
- Le code de jumelage est généré au premier lancement et écrit dans les logs : `docker logs mcz-homekit | grep jumelage`.
- Jumelage : app Maison → **+** → Ajouter un accessoire → **Plus d'options** → « Poêle » → saisir le code.

```
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt pytest && .venv/bin/python -m pytest -q
```
