# Installation

Ce guide installe **mcz-bridge** : un petit serveur Docker qui se connecte au cloud MCZ avec ton compte Maestro+, garde l'état du poêle en
cache, exécute ta programmation et sert la web app. Compte 20 à 30 minutes.

## 1. Ce qu'il te faut

- Un poêle MCZ **Maestro** relié au Wi-Fi, que tu pilotes déjà depuis l'application **MCZ Maestro+** (le serveur passe par le même cloud).
- Le **compte Maestro+** : l'e-mail et le mot de passe avec lesquels tu te connectes dans l'app. Mot de passe oublié : « Mot de passe oublié »
  dans l'app Maestro+, puis connecte-toi une fois dans l'app pour vérifier.
- Une machine **toujours allumée** avec Docker et Docker Compose v2 : NAS, Raspberry Pi 4 ou 5, mini-PC, serveur maison.
  La programmation tourne sur cette machine : téléphone éteint, les plages s'exécutent quand même.
- Pour les **notifications sur iPhone** et l'accès **hors de la maison** : un nom de domaine et du HTTPS, par exemple avec Traefik
  (section 4). Sans ça, tout le reste marche sur le réseau local.

Compatibilité : développé et vérifié sur un **MCZ Ego Comfort Air 10 Core (R)**. La lecture de l'état devrait marcher sur les autres poêles
Maestro ; les commandes utilisent des identifiants relevés sur ce modèle. Vérifie-les avant tout sur un autre modèle (section 7).

## 2. Les identifiants à préparer

| Variable | Où la trouver |
|---|---|
| `MCZ_USER` | L'e-mail de ton compte Maestro+. |
| `MCZ_PASSWORD` | Le mot de passe de ce compte. S'il contient un `$`, laisse ce champ vide et utilise `MCZ_PASSWORD_B64` (ci-dessous). |
| `BRIDGE_TOKEN` | À créer toi-même : `openssl rand -hex 32` (ou `python3 -c "import secrets; print(secrets.token_hex(32))"`). Il donne le contrôle total du poêle par l'API : garde-le secret. |
| `WEB_USER`, `WEB_PASSWORD` | Au choix : l'identifiant et le mot de passe pour ouvrir la web app. Vides, la web app demande `BRIDGE_TOKEN`. |

Rien d'autre à récupérer :

- L'**identifiant du poêle** est trouvé tout seul (tous les poêles du compte apparaissent).
- L'**identifiant du cloud MCZ** (`tenant_id`) est le même pour tous les utilisateurs de l'app et est déjà dans le code.
- La **clé des notifications** (VAPID) est créée au premier lancement et gardée dans la base.

**Mot de passe avec un `$`** : Docker Compose prend le `$` pour une variable. Encode-le en base64, sans qu'il reste dans l'historique du terminal :

```sh
read -s p; printf '%s' "$p" | base64; unset p
```

Mets le résultat dans `MCZ_PASSWORD_B64` (ou `WEB_PASSWORD_B64` pour la web app) et laisse `MCZ_PASSWORD` vide.

## 3. Installation simple (réseau local)

```sh
git clone <adresse-de-ce-depot> mczpoele
cd mczpoele/server
cp .env.example .env
nano .env                      # remplis au moins MCZ_USER, MCZ_PASSWORD, BRIDGE_TOKEN, WEB_USER, WEB_PASSWORD
docker compose up -d --build
docker logs -f mcz-bridge      # Ctrl+C pour quitter les logs
```

Ouvre `http://<ip-de-la-machine>:8000` (port réglable avec `PORT`) et connecte-toi. En quelques secondes, l'état du poêle s'affiche.
Pour vérifier que les commandes passent, monte la consigne de 0,5 °C puis remets-la : chaque commande affiche « confirmé par le poêle »
une fois que le poêle l'a vraiment appliquée.

Sur iPhone, ouvre la page dans Safari, puis Partager → **Sur l'écran d'accueil** : la web app s'ouvre alors comme une application.

## 4. Avec un nom de domaine et HTTPS (Traefik)

Il faut un Traefik déjà en place, avec le fournisseur Docker, un résolveur de certificats et un réseau Docker partagé.

1. Dans `.env`, renseigne aussi `BRIDGE_HOST` (ex. `poele.example.com`), `CERT_RESOLVER` (le nom du résolveur de ton Traefik),
   `TRAEFIK_ENTRYPOINTS` (défaut `websecure`) et `TRAEFIK_NETWORK` (défaut `proxy`).
2. Fais pointer le domaine vers ta box et redirige le port 443 vers Traefik.
3. Lance la version Traefik :

   ```sh
   docker compose -f docker-compose.traefik.yml up -d --build
   ```

La web app a sa propre connexion (sessions de 90 jours, 15 minutes de blocage après trop d'essais ratés). Si tu l'ouvres sur Internet,
choisis un mot de passe solide ; tu peux aussi la mettre derrière un portail d'authentification (Authelia, Authentik…).
Dans ce cas, le pont HomeKit passe par le port local `127.0.0.1:8001` pour ne pas buter sur le portail.

## 5. Avec Portainer

1. Stacks → **Add stack** → **Repository**.
2. URL du dépôt et chemin du fichier : `server/docker-compose.yml`, ou `server/docker-compose.traefik.yml`.
   - Si le dépôt est sur un serveur Git de ton réseau, mets son **adresse IP** plutôt qu'un nom local (`git.maison.local`) : Portainer
     résout les noms avec le DNS de la machine, et un changement de DNS (redémarrage, box) suffit à bloquer tous les déploiements.
   - L'URL ne se modifie plus une fois la stack créée.
3. **Environment variables** : les mêmes que dans `.env.example`.
4. Coche **GitOps updates** (vérification toutes les 5 min) pour redéployer à chaque push, puis **Deploy the stack**.

## 6. Premiers réglages dans la web app

- **Pellets** : indique le prix du sac, puis « Sac de 15 kg ajouté » ou « Réservoir plein » à chaque remplissage. Le niveau est une
  estimation (le poêle n'a pas de capteur) ; ajuste `PELLET_CAPACITY_KG` à ce que contient vraiment ton réservoir plein.
- **Météo** : choisis ta ville (prévisions, chauffe comparée à la météo, prévision de la température de la pièce).
- **Planning** : crée tes plages ; « Avoir déjà chaud à » démarre le poêle plus tôt, d'après ce que le serveur apprend de tes allumages.
- **Programme Maestro** : si le chrono de l'app Maestro+ est actif, il s'ajoute à tes plages. Désactive-le dans Maestro+ pour que seul
  ce serveur programme le poêle.
- **Notifications** : sur iPhone, d'abord la web app sur l'écran d'accueil (iOS 16.4 ou plus) et en HTTPS, puis « Activer sur cet appareil » et « Envoyer un essai ».
  Un mode Concentration peut faire taire les notifications.
- **Garde de température** : rallume le poêle si la pièce va passer sous un seuil, aux heures choisies. Désactivée par défaut.

## 7. Vérifier un autre modèle de poêle

Les commandes utilisent des identifiants de configuration relevés sur le poêle de référence (`server/app/commands.py`) :

| Commande | Configuration | Identifiant (`ConfigurationId`) |
|---|---|---|
| Marche / arrêt | `Spegnimento` | `e373bb67-fe69-4d49-b2f2-4b636fa68e37` |
| Consigne | `Set_amb_temp` | `875fb696-4af1-42da-bb78-ebe57c90fe5c` |
| Puissance | `Set_pot` | `fa2e1c9e-83e3-4435-825b-cdfb50da92a7` |
| Mode | `set_mod` | `d85b93a4-4cae-4745-925d-ef53547c661e` |
| Arrêt programmé | `Spegnimento_prg` | `a088a0dd-4f34-4cc9-a4f1-3b0f1d182777` |

Sur un autre modèle, compare-les avant d'envoyer des commandes :

```sh
python3 tools/mczcloud.py dump     # lecture seule ; demande l'e-mail et le mot de passe en saisie masquée
```

Le script écrit `captures/cloud-dump.json`, avec ta position, ton e-mail et tes jetons masqués. Cherche ces configurations dans
`model_configurations` : si leurs identifiants sont ceux du tableau, les commandes fonctionneront. Sinon, ouvre une issue avec ces
identifiants (jamais le fichier entier).

## 8. Mises à jour, sauvegarde, dépannage

- **Mettre à jour** : `git pull && docker compose up -d --build` (ou laisse Portainer le faire).
- **Sauvegarder** : le volume `mcz-data` contient l'historique, les plages, les réglages et la clé des notifications
  (`docker run --rm -v server_mcz-data:/d -v "$PWD":/b alpine tar czf /b/mcz-data.tgz -C /d .` ; le nom du volume dépend du dossier
  ou de la stack, voir `docker volume ls`).
- **« Variable d'environnement manquante »** au démarrage : une variable obligatoire est vide (voir `docker logs mcz-bridge`).
- **Connexion MCZ refusée** : vérifie l'e-mail et le mot de passe en te connectant dans l'app Maestro+.
- **« Poêle injoignable »** : le module Wi-Fi du poêle ne joint plus le cloud MCZ. Vérifie qu'il apparaît en ligne dans Maestro+.
- **Une commande « non confirmée »** : le cloud l'a reçue mais le poêle ne l'a pas appliquée. Le serveur réessaie trois fois au plus
  et n'envoie jamais un marche/arrêt à l'aveugle (c'est une bascule : un appui de trop rallumerait le poêle).
- **État de santé** : `curl http://<ip>:8000/health`.

## 9. À savoir

Projet non officiel, sans lien avec MCZ. Il utilise le même service cloud que l'application Maestro+, avec ton propre compte, et peut
cesser de fonctionner si MCZ change ce service. Tu restes responsable de ce que le poêle fait : la programmation allume et éteint un
appareil de chauffage à combustible.
