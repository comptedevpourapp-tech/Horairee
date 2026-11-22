# Horaire UMONS (HyperPlanning)

Affiche l'emploi du temps du serveur HyperPlanning de l'Université de Mons
(`hplanning2026.umons.ac.be/invite`) dans une page web claire, via l'export iCal public.

| Fichier | Rôle |
|---|---|
| `horaires.py` | CLI : télécharge et affiche l'horaire (table / JSON / CSV) |
| `serveur_horaires.py` | Serveur web local + API JSON (cache 5 min) |
| `page_horaires.html` | Grille horaire hebdomadaire interactive |
| `Dockerfile`, `docker-compose.yaml` | Conteneur compatible TrueNAS SCALE / Docker |

## Installation sur TrueNAS SCALE (Electric Eel 24.10+)

1. **Apps → Discover Apps → ⋮ (en haut à droite) → Install via YAML**
2. Collez le contenu de [`docker-compose.yaml`](docker-compose.yaml)
3. Installez → l'app est disponible sur `http://<ip-du-nas>:8080`

Personnalisation via variables d'environnement (section `environment` du YAML) :

| Variable | Défaut | Description |
|---|---|---|
| `HP_PORT` | `8000` | Port d'écoute interne du conteneur |
| `HP_HOST` | `127.0.0.1` | Mettre `0.0.0.0` dans un conteneur |
| `HP_URL` | export « toutes salles » UMONS | Lien iCal d'une ressource précise (diplôme, promo, salle, enseignant) |

Pour un autre FAI/hoster de planning HyperPlanning, changez aussi `BASE` dans `horaires.py`.

## En ligne de commande (sans Docker)

```bash
python3 horaires.py --filtre "BAB2 - Ir. civil" --semaine 48
python3 horaires.py --format csv -o cours.csv
python3 serveur_horaires.py          # http://127.0.0.1:8000
```

## Sémaines passées (archive locale)

Le serveur UMONS ne publie qu'≈ 3 semaines glissantes. L'app enregistre donc
automatiquement chaque semaine consultée dans une archive JSON (`HP_ARCHIVE`,
défaut `archive_horaires.json` à côté du script ; dans Docker : volume
`/app/data`) et la recapture toutes les `CAPTURE_HEURES` heures (défaut 2). À partir du moment où l'app tourne, toutes les semaines publiées par UMONS sont archivées et restent consultables indéfiniment (`/api/semaines` liste les semaines disponibles) ; en revanche les semaines antérieures à l'installation n'ont jamais été publiées par UMONS et ne sont pas récupérables.
Une semaine déjà capturée reste consultable après sa disparition de la fenêtre
publiée ; une semaine jamais capturée (trop ancienne ou trop future) n'est pas
disponible — un badge l'indique dans la page.

Notes :
- Pour l'emploi du temps d'une promo précise : dans l'interface `invite?fd=1`, ouvrez la ressource, cliquez le bouton iCal et copiez le lien permanent dans `HP_URL` ou le champ de la page.

## Année entière, automatiquement

L'export public « toutes salles » est limité par UMONS à la fenêtre publiée
(~3 semaines glissantes) : aucun paramètre ne l'étend. Les chevauchements de
cette limite ont été contournés :

1. **Catalogue de flux annuels intégré** (`liens_annuels.json`) : 234
   formations publiques de 2026-2027 (tous les bacheliers confirmés, dont
   `.BAB2 - Ir. civil`) avec leur **lien permanent iCal annuel** — le même que
   celui de l'interface (jeton `icalsecurise` attribué par le serveur
   HyperPlanning, accessible sans authentification une fois récolté). Ces liens
   ont été récoltés en pilotant le client officiel (chaque formation demande son
   `FonctionParametresICAL`) ; les horaires de master non publiés ne sont pas
   accessibles en mode invité et restent hors catalogue.
2. **Fusion automatique côté serveur** : au démarrage (puis toutes les 24 h,
   `HP_ANNUEL_HEURES`), le serveur télécharge chaque flux annuel et le fusionne
   dans l'archive avec dédoublonnage par cours (`Cours-<id>-<n>` + horaire) :
   un cours publié à la fois dans l'export « toutes salles » et dans le flux
   annuel n'apparaît qu'une fois, avec la description la plus riche (code,
   type) et les formations des deux sources.
3. **À la demande** : sélectionner une formation hors fenêtre télécharge
   immédiatement son flux annuel (badge « 📅 année complète » dans la page).

Résultat : toute semaine de l'année 2026-2027 est consultable pour les
formations du catalogue — la navigation est indépendante de la fenêtre
glissante, de septembre 2026 à juillet 2027.

### Après un redémarrage du conteneur

La passe annuelle de fond démarre ~30 s après le lancement et met environ
2 à 4 minutes à intégrer les 234 flux (progression affichée dans les
 journaux du conteneur : « Passe annuelle : 25/234… »). Pendant ce temps,
le bandeau 📂 de la page signale « Passe annuelle en cours » ; les semaines
lointaines se remplissent au fur et à mesure, puis l'indicateur passe à
`annuel_pret: true` (`/api/semaines`). Les horaires d'une formation
sélectionnée restent disponibles immédiatement pendant la passe.

Note technique : les accès à l'archive sont protégés par un verrou
(`VERROU_ARCHIVE`) — le fil annuel, la capture périodique « toutes salles »
et les requêtes web peuvent désormais travailler en même temps sans
corrompre l'archive ni faire tomber une requête en erreur 500.

### Variables d'environnement

- `HP_LIENS_ANNUELS` (défaut `liens_annuels.json` à côté du code) : chemin du
  catalogue. Remplacable via un montage TrueNAS.
- `HP_ANNUEL_HEURES` (défaut `24`) : rythme de rafraîchissement des flux
  annuels en arrière-plan ; `0` désactive (ne reste que la capture glissante).
- Champ « Secours flux iCal (optionnel) » de la page : pour une ressource non
  couverte (ex. horaire de master protégé), coller le lien permanent copié dans
  l'interface `invite?fd=1` (bouton iCal). Mémorisé dans le navigateur
  (localStorage, clé `lical`) ; **Retirer** revient au flux public.

### Régénérer le catalogue (nouvelles années)

Les jetons `icalsecurise` sont attribués par le serveur UMONS et devraient
rester valables toute l'année. En 2027-2028 : rejouer le récolteur
`outils/recolter_liens.js` (Node + jsdom — il n'est pas dans l'image Docker,
c'est un outil de maintenance) : il lance le client `invite?fd=1` dans jsdom,
énumère `FonctionRenvoyerListeDeRessource`, puis interroge
`FonctionParametresICAL` pour chaque formation et écrit `liens_annuels.json`
(réprise possible : les entrées existantes sont sautées).

