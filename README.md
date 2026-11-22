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

## Toute l'année (flux iCal annuel)

L'export public « toutes salles » est limité par UMONS à la fenêtre publiée
(~3 semaines glissantes) : aucun paramètre ne l'étend. En revanche, chaque
ressource dispose dans l'interface officielle d'un **lien permanent iCal
annuel** (tout est prévu côté client avec `d=[1..62]`) :

1. Dans `https://hplanning2026.umons.ac.be/invite?fd=1`, ouvre ta ressource
   (formation, salle, …).
2. Clique le bouton **iCal** et copie le **lien permanent** (image/QR).
3. Colle-le dans la page web de l'app, champ **« Flux iCal annuel »** → **Utiliser**.

L'app télécharge alors l'horaire de l'année entière pour cette ressource, en
continrant à fusionner avec l'archive locale. Le choix est mémorisé dans le
navigateur (localStorage, clé `lical`) ; **Retirer** revient au flux public.
Côté serveur, la variable `HP_URL` accepte aussi ce lien (historique).
