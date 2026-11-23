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

## Types de cours détectés

Le type d'activité est lu **en priorité dans le label officiel UMONS**
(lignes « Type : … » des descriptions iCal) puis, à défaut, déduit du libellé,
et affiché comme pastille colorée : **Cours**, **Exercices**,
**Exercices cotés** (exercices notés — type UMONS « Exercice coté », parfois
orthographié « côté/côte »), **Cours/Exercices**, **TP** (aussi « labo »,
« pratique », « atelier »), **Projet** (« project »), **Examen** (« test »,
« interrogation », « rattrapage », « soutenance », « défense », « jury »,
« épreuve pratique »), **Stage**, **TFE** (« mémoire », « travail de fin
d'études »), **Tutorat**, **Séminaire** (« workshop »), **Conférence**
(« colloque », « débat »), **Réunion** (« consultation », « Q-R »),
**Remédiation**, **Visite**, **Voyage**, **À distance**. Les libellés UMONS
tronqués à 10 caractères (« Remédiatio », « Interrogatio », « Cours/Exercic »…)
sont normalisés. Le type est recalculé à l'affichage : les entrées archivées
avant l'extension des types sont réparées automatiquement, sans migration.
Les types **Examen** (écrit ou oral) et **Exercices cotés** portent une
étiquette rouge en bas de case (centrée) ; le type étant affiché au bas de la
case, les metadonnées utiles restent lisibles au-dessus.

## Préférences mémorisées (cookies)

À la première visite, un écran d'accueil demande la **formation** et le
**groupe de TP** (optionnel) ; le choix est enregistré dans des cookies
(`hp_categorie`, `hp_ressource`, `hp_groupe`, validité 1 an) et réappliqué
automatiquement à chaque ouverture de la page. Si la formation choisie
disparaît du catalogue, l'écran d'accueil se rouvre. Le bouton **Passer**
permet de ne rien mémoriser (tout l'horaire s'affiche alors). Les boutons de
la page restent utilisables à tout moment ; choisir une autre formation ne
réécrit le cookie qu'en passant par l'accueil.

👉 Astuce : pour retrouver l'écran d'accueil, effacez les cookies `hp_*` du
site (ou utilisez une fenêtre privée).

## Mémo des cours

Certaines descriptions UMONS contiennent une ligne « Mémo : … » (~3 800 cours
par an : consignes de rentrée, « Test diagnostique MLFU », « SEMAINE
FLEXIBLE - Activités à préciser »…). Le mémo est extrait automatiquement et
affiché dans la case du cours (badge 📝 italique sur fond jaune pâle, une
ligne avec ellipsis) ainsi que dans la fiche détail. Les mémos qui ne font
que répéter le type ou l'intitulé déjà affichés sont masqués (comparaison
sans accents ni pluriel).

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
3. **À la demande** : tant que la passe de fond n'a pas encore traité une
   ressource, la sélectionner déclenche son flux annuel immédiatement (badge
   « 📅 année complète » dans la page) — une fois la passe terminée, tout
   est déjà en mémoire, cf. ci-dessous.

### Architecture : tout en mémoire, l'utilisateur ne reçoit que sa part

Le serveur fonctionne en **mode mémoire** :

1. Au démarrage, la mémoire est **amorcée depuis l'archive persistée**
   (`amorcer_memoire()`) — le serveur redémarre « chaud », sans attendre le
   réseau.
2. En arrière-plan, la passe annuelle télécharge **tout** : les 234 flux annuels
   de formations **puis** les ~1 159 flux annuels de groupes de TD, et fusionne
   chaque flux dans l'archive (dédoublonnage par cours). C'est le seul endroit
   où le serveur communique avec UMONS, avec politesse (0,4 s entre flux).
3. Chaque requête utilisateur (`/api/horaires`, `/api/ressources`,
   `/api/semaines`) **ne télécharge rien** : elle filtre simplement le pool
   complet en mémoire (`liste_complete()`, reconstruit seulement quand une
   source a changé) et renvoie **uniquement les cours de la sélection**
   (formation + groupe + semaine + filtre). Mesuré : ~30 ms par réponse ;
   l'utilisateur qui choisit « BAB civil + groupe B » reçoit exactement ses
   cours, rien d'autre.
4. La fenêtre live « toutes salles » (~3 semaines) reste capturée toutes les
   2 h (`CAPTURE_HEURES`) pour garder les semaines courantes fraîches ; le
   champ « Secours flux iCal » conserve son comportement direct (téléchargé à
   la demande, dédoublonné contre la mémoire).

### Groupes de TP (TD) — ex. « BAB civil - B »

Umons ne publie presque aucun sous-groupe dans les flux publics de formation :
les TD/TP sont des **ressources distinctes** d'HyperPlanning (`genreRessource 4`).
Le dépôt embarque donc un second catalogue, `liens_groupes.json`, récolté de la
même façon (client officiel piloté dans jsdom) :

1. `outils/recolter_groupes.js` énumère, pour chaque formation du catalogue,
   ses TD/options (`FonctionListeDeTDEtOptionDuDiplome`) puis obtient le lien
   iCal annuel de chaque groupe (`FonctionParametresICAL`) ;
2. au chargement d'une formation, la liste « Groupe (TP) » de la page se
   complète avec ces groupes (libellés courts : « B », « 01 »…) ;
3. sélectionner un groupe fusionne alors le flux iCal annuel **du groupe** :
   ses TP, séminaires et cours spécifiques apparaissent dans toute l'année
   (les cours communs à la formation restent visibles, comme sur HyperPlanning).

Variables : `HP_LIENS_GROUPES` (défaut `liens_groupes.json` à côté du code ;
monter un fichier TrueNAS pour le remplacer). Récolte pareil :
`DELAI`, `SORTIE`, `LIENS_FORMATIONS`, `K`/`N` (partition, pour lancer
plusieurs sessions en parallèle).

Résultat : toute semaine de l'année 2026-2027 est consultable pour les
formations du catalogue — la navigation est indépendante de la fenêtre
glissante, de septembre 2026 à juillet 2027.

### Après un redémarrage du conteneur

Au démarrage la mémoire est amorcée depuis l'archive persistée (les semaines
déjà connues sont servies immédiatement). La passe annuelle de fond démarre
~30 s après le lancement : ~1 minute pour les 234 formations, puis 15 à
20 minutes pour les ~1 159 groupes de TD (progression dans les journaux :
« Passe annuelle : 25/234… », « Passe groupes : 50/1159… »). Empreinte
typique : ~0,5 à 0,8 Go de RAM une fois l'année en mémoire (prévoir la
limite de mémoire du conteneur TrueNAS en conséquence). Pendant ce temps,
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

