#!/usr/bin/env python3
"""Mini site web pour afficher l'horaire HyperPlanning (type hplanning2026.umons.ac.be/invite).

Lance le serveur :
    python3 serveur_horaires.py [--port 8000] [--host 127.0.0.1] [--url URL_ICAL]

- Sélecteur de ressources (formations, salles, groupes) reconstruit depuis
  l'export public « toutes salles », comme dans l'interface d'origine.
- Archive locale persistante (HP_ARCHIVE) : les semaines publiées (fenêtre
  glissante ≈ 3 semaines) sont enregistrées à chaque consultation et via une
  capture automatique périodique (CAPTURE_HEURES, défaut 2 h), ce qui permet
  de revoir les semaines passées même quand le serveur ne les publie plus.
- Catalogue de flux annuels (liens_annuels.json) : un lien par formation
  publique de l'année 2026-2027 (récolté via l'interface UMONS). Le serveur
  fusionne ces flux dans l'archive — toutes les semaines de l'année deviennent
  consultables pour chaque formation, sans intervention. Désactivable avec
  HP_ANNUEL_HEURES=0 ; en cas d'année suivante : relancer la récolte (voir README).
- Variables d'environnement : HP_HOST, HP_PORT, HP_URL, HP_ARCHIVE,
  CAPTURE_HEURES, HP_LIENS_ANNUELS, HP_LIENS_GROUPES, HP_ANNUEL_HEURES.

Aucune dépendance externe : bibliothèque standard uniquement.
"""

import argparse
import html
import json
import os
import re
import sys
import threading
import time
import traceback
import unicodedata
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import horaires

FICHIER_ARCHIVE = Path(os.environ.get(
    "HP_ARCHIVE",
    str(Path(__file__).with_name("archive_horaires.json")),
))
INTERVALLE_CAPTURE = float(os.environ.get("CAPTURE_HEURES", "2")) * 3600
INTERVALLE_REPRISE = 120  # nouvelle tentative rapide tant qu'aucune donnée live n'a été obtenue
FICHIER_LIENS = Path(os.environ.get(
    "HP_LIENS_ANNUELS",
    str(Path(__file__).with_name("liens_annuels.json")),
))
FICHIER_GROUPES = Path(os.environ.get(
    "HP_LIENS_GROUPES",
    str(Path(__file__).with_name("liens_groupes.json")),
))
INTERVALLE_ANNUEL = float(os.environ.get("HP_ANNUEL_HEURES", "24")) * 3600
DELAIS_ANNUEL = 0.4      # pause entre deux flux annuels (politesse envers le serveur UMONS)
DELAI_BOUCLE_ANNUEL = 30  # attendue avant la première passe annuelle au démarrage

CACHE = {"url": None, "quand": 0.0, "evenements": [], "ressources": None}
ARCHIVE = {}
DUREE_CACHE = 300

CATALOGUE_ANNUEL = {}          # libellé formation -> lien iCal annuel
ANNUEL_MEMOIRE = {}            # libellé -> {quand, evenements} (cache TTL)
CATALOGUE_GROUPES = {}         # libellé formation -> {libellé TD -> lien iCal annuel}
GROUPES_MEMOIRE = {}           # "formation\x00TD" -> {quand, evenements}
VERROU_ANNUEL = threading.Lock()

# Protège ARCHIVE contre les accès concurrents (fil annuel de fond, capture
# périodique, requêtes web) — sans lui, l'itération lève
# « RuntimeError: dictionary changed size during iteration » (HTTP 500).
VERROU_ARCHIVE = threading.RLock()
ANNUEL_PRET = False            # True après la première passe annuelle complète
ANNUEL_GROUPES_PRET = False    # True après la première passe des groupes complète

# Liste complète des cours (archive + live + flux annuels de formations et de
# groupes) reconstruite en mémoire dès que la moindre source change ; les
# requêtes utilisateur filtrent UNIQUEMENT cette liste : zéro requête réseau
# sur le chemin d'affichage, donc réponse quasi instantanée.
VERSION_MEMOIRE = 0            # incrémenté à chaque changement d'une source
LISTE_MEMOIRE = {"version": -1, "evenements": []}

PREFIXE_COURS = re.compile(r"^Cours-(\d+)-(\d+)-")
CHEVRONS_GROUPE = re.compile(r"<[^<>]*>")


def marquer_memoire():
    """Signale que le pool de cours a changé : la liste complète sera
    reconstruite à la prochaine requête (au lieu d'être refaite à chaque fois)."""
    global VERSION_MEMOIRE
    VERSION_MEMOIRE += 1


def libelle_court_td(td):
    """« BAB2 - Ir Civil - B » -> « B » (la formation est déjà sélectionnée
    juste au-dessus, inutile de la répéter)."""
    morceaux = [m.strip() for m in (td or "").split(" - ")]
    if len(morceaux) >= 2 and 0 < len(morceaux[-1]) <= 8:
        return morceaux[-1]
    return td


def nettoyage_groupe(valeur):
    """Un groupe HyperPlanning est préfacé par sa formation entre chevrons
    (ex. «&lt;.BAB2 - Ir. civil&gt;BAB2 - Ir Civil - B») ; on retire le
    préfixe pour garder un libellé propre et filtrable."""
    return CHEVRONS_GROUPE.sub("", valeur or "").strip()


def forme_lemme(texte):
    """Forme comparale : minuscules, sans accents, sans pluriel (pour ne pas
    réafficher dans le mémo ce que l'étiquette de type montre déjà)."""
    s = unicodedata.normalize("NFKD", (texte or "").lower())
    s = "".join(c for c in s if not unicodedata.combining(c))
    return " ".join(t[:-1] if len(t) > 3 and t.endswith("s") else t for t in s.split())


def retirer_suffixe_td(texte, groupe):
    """Retire « - <formation>Groupe / type » des intitulés, où qu'il soit.

    Un flux TD suffixe la matière par le groupe, mais les variantes sont
    nombreuses : « - GroupeB - TP », « - GroupeB - C (FR) », combinaisons
    « - GroupeA, GroupeB »… Un simple endswith(groupe) laisse donc des
    résidus. On coupe à partir de la première occurrence du libellé (ou de
    sa partie formation, la plus courte victorieuse) en tolérant les
    variantes d'espacement (ex. « -  01 » à double espace)."""
    texte = CHEVRONS_GROUPE.sub("", (texte or "")).strip()
    if not texte or not groupe:
        return texte
    compresse = re.sub(r"\s{2,}", " ", groupe.strip())
    candidats = {groupe.strip(), compresse}
    parties = compresse.split(" - ")
    if len(parties) > 1:
        prefixe = " - ".join(parties[:-1])
        candidats |= {prefixe, re.sub(r"\s{2,}", " ", prefixe)}
    resultat = texte
    for candidat in candidats:
        motif = r"\s*-\s*".join(
            re.escape(p).replace("\\ ", "\\s+") for p in candidat.split(" - "))
        coupe = re.sub(rf"[\s,\-]*(?:{motif}).*$", "", texte)
        coupe = re.sub(r"\s{2,}", " ", coupe).strip(" -,")
        if coupe and len(coupe) < len(resultat):
            resultat = coupe
    return resultat


def charger_catalogue_annuel():
    global CATALOGUE_ANNUEL
    catalogue = {}
    try:
        brut = json.loads(FICHIER_LIENS.read_text(encoding="utf-8"))
    except Exception as err:
        print(f"Catalogue de flux annuels indisponible ({err}) : l'année ne sera couverte que par la capture glissante.", file=sys.stderr)
        CATALOGUE_ANNUEL = {}
        return catalogue
    base = getattr(horaires, "BASE", "")
    for libelle, v in brut.items():
        lien = (v or {}).get("lien") or ""
        if not lien:
            continue
        if lien.startswith("http"):
            url = lien
        else:
            url = base + "/" + lien.lstrip("/")
        catalogue[libelle] = url
    CATALOGUE_ANNUEL = catalogue


def charger_catalogue_groupes():
    """Chargé une seule fois au boot : liens_groupes.json = les flux iCal
    annuels des groupes de TD (TP) par formation (récoltés via le client
    officiel, voir outils/recolter_groupes.js)."""
    global CATALOGUE_GROUPES
    catalogue = {}
    try:
        brut = json.loads(FICHIER_GROUPES.read_text(encoding="utf-8"))
    except Exception as err:
        print(f"Catalogue de groupes indisponible ({err}) : le choix d'un groupe de TP se limitera aux groupes publiés.", file=sys.stderr)
        CATALOGUE_GROUPES = {}
        return catalogue
    base = getattr(horaires, "BASE", "")
    for formation, tds in brut.items():
        if not isinstance(tds, dict):
            continue
        pour_formation = {}
        for libelle, lien in tds.items():
            if not lien:
                continue
            url = lien if lien.startswith("http") else base + "/" + lien.lstrip("/")
            pour_formation[libelle] = url
        if pour_formation:
            catalogue[formation] = pour_formation
    CATALOGUE_GROUPES = catalogue
    if catalogue:
        print(f"Catalogue de flux annuels chargé : {len(catalogue)} formations.")
    return catalogue


def est_marqueur_journee(eve):
    """Vrai pour un marqueur journée entière (Vacances/congé publié à 00:00
    et terminant à 00:00, f-> souvent plusieurs jours plus tard)."""
    try:
        debut = eve["debut"].astimezone(horaires.TZ_LOCAL)
        fin = eve["fin"].astimezone(horaires.TZ_LOCAL) if eve.get("fin") else None
    except Exception:
        return False
    if fin is None or not eve.get("matiere"):
        return False
    return (debut.hour, debut.minute) == (0, 0) and (fin.hour, fin.minute) == (0, 0) \
        and fin > debut and (fin.date() - debut.date()).days >= 1


def cle_dedup(eve):
    """Un même cours apparaît avec des UID différents selon l'export
    (toutes salles vs flux par formation) mais partage le préfixe
    Cours-<id>-<num>- . On dédoublonne sur (début, préfixe).
    Les marqueurs journée entière (Vacances…) ont des UID complètement
    différents d'un flux à l'autre : on les rapproche par (début, fin, intitulé)."""
    debut = eve["debut"].astimezone(horaires.TZ_LOCAL).isoformat()
    m = PREFIXE_COURS.match(eve.get("uid") or "")
    suffixe = m.group(0) if m else (eve.get("uid") or "")
    if est_marqueur_journee(eve):
        fin = eve["fin"].astimezone(horaires.TZ_LOCAL).isoformat()
        return "marqueur|" + debut + "|" + fin + "|" + eve.get("matiere", "").strip().lower()
    return debut + "|" + suffixe


def _a_ligne_type(description):
    return bool(re.search(r"^type\s*:", description or "", re.IGNORECASE | re.MULTILINE))


def fusionner_event(a, b):
    """Fusionne deux variantes du même cours : garde la description la plus
    riche (celle qui porte la ligne Type :) et complète formations/salles/groupes."""
    if a is None:
        return b
    desc_a = a.get("description") or ""
    desc_b = b.get("description") or ""
    base, autre = (a, b)
    if not _a_ligne_type(desc_a) and _a_ligne_type(desc_b):
        base, autre = (b, a)
    lignes = []
    for ligne in (base.get("description") or "").split("\n"):
        l = ligne.strip()
        if l and l not in lignes:
            lignes.append(l)
    for ligne in (autre.get("description") or "").split("\n"):
        l = ligne.strip()
        if l and l not in lignes:
            lignes.append(l)
    base["description"] = "\n".join(lignes)
    for cle in ("formations", "salles_liste", "groupes"):
        for v in autre.get(cle) or []:
            if v and v not in (base.get(cle) or []):
                base.setdefault(cle, []).append(v)
    if (autre.get("maj") or "") > (base.get("maj") or ""):
        base["maj"] = autre["maj"]
    return base


def fusionner_sources(paires):
    """Fusionne plusieurs listes d'événements en dédoublonnant les paires
    équivalentes (même cours vu par plusieurs exports). paires = liste de
    tuples (étiquette_source, événements), du moins prioritaire au plus prioritaire."""
    par_cle = {}
    for etiquette, evenements in paires:
        for e in evenements:
            try:
                cle = cle_dedup(e)
            except Exception:
                continue
            base = fusionner_event(par_cle.get(cle), e)
            base["_source"] = etiquette
            par_cle[cle] = base
    return list(par_cle.values())


def charger_archive():
    global ARCHIVE
    with VERROU_ARCHIVE:
        if FICHIER_ARCHIVE.exists():
            try:
                ARCHIVE = json.loads(FICHIER_ARCHIVE.read_text(encoding="utf-8"))
            except Exception as err:
                print(f"Archive illisible ({err}), démarrage à vide.", file=sys.stderr)
                ARCHIVE = {}


def sauver_archive():
    with VERROU_ARCHIVE:
        try:
            temporaire = FICHIER_ARCHIVE.with_suffix(".tmp")
            temporaire.write_text(json.dumps(ARCHIVE, ensure_ascii=False), encoding="utf-8")
            temporaire.replace(FICHIER_ARCHIVE)
        except Exception as err:
            print(f"Impossible d'écrire l'archive {FICHIER_ARCHIVE} : {err}", file=sys.stderr)


def analyser(eve):
    return horaires.analyser_cours(eve.get("matiere", ""), eve.get("description", ""))


def enregistrer_dans_archive(evenements):
    modifie = False
    with VERROU_ARCHIVE:
        for e in evenements:
            uid = e.get("uid") or (
                e["debut"].astimezone(horaires.TZ_LOCAL).isoformat() + "|" + e.get("matiere", "")
            )
            infos = analyser(e)
            rec = {
                "debut": e["debut"].astimezone(horaires.TZ_LOCAL).isoformat(),
                "fin": e["fin"].astimezone(horaires.TZ_LOCAL).isoformat(),
                "matiere": e.get("matiere", ""),
                "salle": e.get("salle", ""),
                "description": e.get("description", ""),
                "uid": uid,
                "maj": e.get("maj", ""),
                "vu": datetime.now(horaires.TZ_LOCAL).isoformat(timespec="seconds"),
                "code": infos["code"],
                "intitule": infos["intitule"],
                "enseignants": infos["enseignants"],
                "type_ac": infos["type"],
            }
            actuel = ARCHIVE.get(uid)
            champs = ("maj", "debut", "fin", "matiere", "salle", "description")
            if actuel is None or any(actuel.get(c) != rec[c] for c in champs):
                rec["vu"] = actuel.get("vu", rec["vu"]) if actuel else rec["vu"]
                ARCHIVE[uid] = rec
                modifie = True
        if modifie or not FICHIER_ARCHIVE.exists():
            sauver_archive()
        marquer_memoire()


def liste_complete():
    """Pool complet en mémoire : archive + fenêtre live + flux annuels de
    formations et de groupes déjà rassemblés. Reconstruite seulement quand une
    source a changé (marquer_memoire) — les requêtes utilisateur filtrent cette
    liste sans AUCUNE requête réseau, d'où la fluidité."""
    if LISTE_MEMOIRE["version"] == VERSION_MEMOIRE:
        return LISTE_MEMOIRE["evenements"]
    with VERROU_ANNUEL:
        annuels = [e["evenements"] for e in ANNUEL_MEMOIRE.values()]
        groupes_m = [e["evenements"] for e in GROUPES_MEMOIRE.values()]
    paires = [
        ("archive", evenements_archive()),
        ("live", list(CACHE["evenements"])),
        ("annuel", [e for lot in annuels for e in lot]),
        ("annuel", [e for lot in groupes_m for e in lot]),
    ]
    fusion = fusionner_sources(paires)
    LISTE_MEMOIRE["version"] = VERSION_MEMOIRE
    LISTE_MEMOIRE["evenements"] = fusion
    return fusion


def amorcer_memoire():
    """Après un redémarrage, recharge la mémoire de cours depuis l'archive
    persistée au lieu de retélécharger les ~1 300 flux : le serveur redémarre
    « chaud ». Les entrées amorcées orientent leur TTL pour être rafraîchies
    au prochain cycle (moitié de l'intervalle)."""
    if not ARCHIVE:
        return
    eve = evenements_archive()
    formes = set(CATALOGUE_ANNUEL)
    par_formation = {}
    par_groupe = {}
    for e in eve:
        for f in e.get("formations") or []:
            if f in formes or ("." + f.lstrip(".")) in formes:
                par_formation.setdefault(f, []).append(e)
                for g in e.get("groupes") or []:
                    if g:
                        par_groupe.setdefault(f + "\x00" + g, []).append(e)
    quart = INTERVALLE_ANNUEL / 2
    with VERROU_ANNUEL:
        for ressource, evs in par_formation.items():
            ANNUEL_MEMOIRE.setdefault(ressource, {"quand": time.time() - quart, "evenements": list(evs)})
        for cle, evs in par_groupe.items():
            GROUPES_MEMOIRE.setdefault(cle, {"quand": time.time() - quart, "evenements": list(evs)})
    marquer_memoire()
    print(f"Mémoire amorcée depuis l'archive : {len(par_formation)} formations, "
          f"{len(par_groupe)} couples formation/groupe.", file=sys.stderr)


def obtenir_annuel(ressource):
    """Télécharge (avec cache TTL) le flux iCal annuel d'une formation et
    l'enregistre dans l'archive. La formation est mentionnée dans les
    descriptions (les flux par formation n'en portent pas)."""
    if not ressource:
        return []
    with VERROU_ANNUEL:
        maint = time.time()
        entree = ANNUEL_MEMOIRE.get(ressource)
        if entree and maint - entree["quand"] < INTERVALLE_ANNUEL:
            return entree["evenements"]
        lien = CATALOGUE_ANNUEL.get(ressource)
        if lien is None and not ressource.startswith("."):
            lien = CATALOGUE_ANNUEL.get("." + ressource.lstrip("."))
        if not lien:
            return entree["evenements"] if entree else []
        evenements = None
        derniere_erreur = None
        for essai in range(2):              # reprise : 1 timeout ne saute pas la formation
            try:
                evenements = horaires.parser_ics(horaires.telecharger_ics(lien))
                break
            except Exception as err:
                derniere_erreur = err
                if essai == 0:
                    time.sleep(3)
        if evenements is None:
            print(f"Flux annuel indisponible pour {ressource} ({derniere_erreur}).", file=sys.stderr)
            if entree and maint - entree["quand"] < 7 * 86400:
                return entree["evenements"]
            return []
        for e in evenements:
            desc = e.get("description") or ""
            if not re.search(r"^\s*formations?\s*:", desc, re.IGNORECASE | re.MULTILINE):
                e["description"] = (desc + f"\nFormations : {ressource}").strip()
            e["description"] = html.unescape(e["description"])
            e["formations"] = []
            e["salles_liste"] = []
            e["groupes"] = []
            infos = analyser(e)
            e.update({"code": infos["code"], "intitule": infos["intitule"],
                      "enseignants": infos["enseignants"], "type": infos["type"]})
            for ligne in (e.get("description") or "").split("\n"):
                ligne = ligne.strip()
                if ":" not in ligne:
                    continue
                cle, _, valeur = ligne.partition(":")
                cle = cle.strip().lower()
                valeurs = [html.unescape(v.strip()) for v in valeur.split(",") if v.strip()]
                if cle in ("formation", "formations"):
                    e["formations"] += valeurs
                elif cle in ("salle", "salles"):
                    e["salles_liste"] += valeurs
                elif cle in ("groupe", "groupes"):
                    e["groupes"] += [g for g in (nettoyage_groupe(v) for v in valeurs) if g]
        enregistrer_dans_archive(evenements)
        ANNUEL_MEMOIRE[ressource] = {"quand": maint, "evenements": evenements}
        marquer_memoire()
        return evenements


def obtenir_annuel_groupe(ressource, groupe):
    """Télécharge (cache TTL) le flux iCal annuel d'un groupe de TD (ressource
    « TD » d'HyperPlanning) et l'archive. Les descriptions reçoivent
    « Formations : » et « Groupes : » pour que les filtres existants
    s'appliquent tels quels."""
    if not ressource or not groupe:
        return []
    tds = CATALOGUE_GROUPES.get(ressource)
    if tds is None and not ressource.startswith("."):
        tds = CATALOGUE_GROUPES.get("." + ressource.lstrip("."))
    lien = (tds or {}).get(groupe)
    if not lien:
        return []
    cle = ressource + "\x00" + groupe
    with VERROU_ANNUEL:
        maint = time.time()
        entree = GROUPES_MEMOIRE.get(cle)
        if entree and maint - entree["quand"] < INTERVALLE_ANNUEL:
            return entree["evenements"]
        evenements = None
        derniere_erreur = None
        for essai in range(2):
            try:
                evenements = horaires.parser_ics(horaires.telecharger_ics(lien))
                break
            except Exception as err:
                derniere_erreur = err
                if essai == 0:
                    time.sleep(3)
        if evenements is None:
            print(f"Flux annuel de groupe indisponible pour {groupe} ({ressource}) ({derniere_erreur}).", file=sys.stderr)
            if entree and maint - entree["quand"] < 7 * 86400:
                return entree["evenements"]
            return []
        for e in evenements:
            desc = html.unescape(e.get("description") or "")
            # les flux de TD suffixent la matière par « <formation>Groupe »
            # (parfois suivi d'un type, combiné à d'autres groupes…) : on
            # retire le suffixe pour garder un intitulé lisible ; le faire
            # ici garantit une archive propre pour toutes les vues
            e["matiere"] = retirer_suffixe_td(nettoyage_groupe(e.get("matiere") or ""), groupe)
            ajout = []
            if "formation" not in desc.lower():
                ajout.append(f"Formations : {ressource}")
            if "groupe" not in desc.lower():
                ajout.append(f"Groupes : {groupe}")
            if ajout:
                desc = (desc + "\n" + "\n".join(ajout)).strip()
            e["description"] = desc
            e["formations"] = []
            e["salles_liste"] = []
            e["groupes"] = []
            infos = analyser(e)
            e.update({"code": infos["code"], "intitule": infos["intitule"],
                      "enseignants": infos["enseignants"], "type": infos["type"]})
            for ligne in desc.split("\n"):
                ligne = ligne.strip()
                if ":" not in ligne:
                    continue
                cle_ligne, _, valeur = ligne.partition(":")
                cle_ligne = cle_ligne.strip().lower()
                valeurs = [html.unescape(v.strip()) for v in valeur.split(",") if v.strip()]
                if cle_ligne in ("formation", "formations"):
                    e["formations"] += valeurs
                elif cle_ligne in ("salle", "salles"):
                    e["salles_liste"] += valeurs
                elif cle_ligne in ("groupe", "groupes"):
                    e["groupes"] += [g for g in (nettoyage_groupe(v) for v in valeurs) if g]
        enregistrer_dans_archive(evenements)
        GROUPES_MEMOIRE[cle] = {"quand": maint, "evenements": evenements}
        marquer_memoire()
        return evenements


def boucle_annuel():
    """En arrière-plan : télécharge TOUT (flux annuels de toutes les formations
    PUIS de tous les groupes de TD) et fusionne dans la mémoire + l'archive,
    cycle HP_ANNUEL_HEURES. Les requêtes utilisateur ne téléchargent jamais :
    elles filtrent le pool déjà en mémoire (liste_complete)."""
    def tour():
        global ANNUEL_PRET, ANNUEL_GROUPES_PRET
        time.sleep(DELAI_BOUCLE_ANNUEL)
        while True:
            debut_passe = time.time()
            total = len(CATALOGUE_ANNUEL)
            reussis = 0
            for indice, ressource in enumerate(list(CATALOGUE_ANNUEL), 1):
                entree = ANNUEL_MEMOIRE.get(ressource)
                if entree and time.time() - entree["quand"] < INTERVALLE_ANNUEL:
                    reussis += 1
                    continue                 # encore frais : rien à télécharger
                try:
                    obtenir_annuel(ressource)
                    if ressource in ANNUEL_MEMOIRE:
                        reussis += 1
                except Exception:
                    traceback.print_exc(file=sys.stderr)
                if indice % 25 == 0:
                    print(f"Passe annuelle : {indice}/{total} formations traitées "
                          f"(en cours {time.time() - debut_passe:.0f} s)…", file=sys.stderr)
                time.sleep(DELAIS_ANNUEL)
            if total == 0 or len(ANNUEL_MEMOIRE) >= total / 2:
                ANNUEL_PRET = True
            print(f"Passe annuelle (formations) terminée : {reussis}/{total} "
                  f"intégrés en {time.time() - debut_passe:.0f} s. Archive : {len(ARCHIVE)} cours.",
                  file=sys.stderr)
            # passe des groupes : les flux iCal annuels de chaque TD (TP…),
            # pour que la sélection d'un groupe soit purement en mémoire
            total_g = sum(len(tds) for tds in CATALOGUE_GROUPES.values())
            fait_g = 0
            debut_groupes = time.time()
            for ressource, tds in list(CATALOGUE_GROUPES.items()):
                for groupe in list(tds):
                    cle = ressource + "\x00" + groupe
                    entree = GROUPES_MEMOIRE.get(cle)
                    if entree and time.time() - entree["quand"] < INTERVALLE_ANNUEL:
                        fait_g += 1
                        continue
                    try:
                        obtenir_annuel_groupe(ressource, groupe)
                    except Exception:
                        traceback.print_exc(file=sys.stderr)
                    fait_g += 1
                    if fait_g % 50 == 0:
                        print(f"Passe groupes : {fait_g}/{total_g} TD traités "
                              f"(en cours {time.time() - debut_groupes:.0f} s)…", file=sys.stderr)
                    time.sleep(DELAIS_ANNUEL)
            ANNUEL_GROUPES_PRET = True
            print(f"Passe annuelle complète (groupes inclus) : {fait_g}/{total_g} groupes, "
                  f"{time.time() - debut_passe:.0f} s au total.", file=sys.stderr)
            time.sleep(max(INTERVALLE_ANNUEL, 3600))
    fil = threading.Thread(target=tour, daemon=True, name="annuel")
    fil.start()


def evenements_archive():
    evenements = []
    with VERROU_ARCHIVE:
        # copie instantanée : l'itération directe sur ARCHIVE.values() peut
        # lever « dictionary changed size during iteration » si le fil annuel
        # écrit en même temps (source du HTTP 500 observé)
        instantane = list(ARCHIVE.values())
    for rec in instantane:
        try:
            evenements.append({
                "debut": datetime.fromisoformat(rec["debut"]),
                "fin": datetime.fromisoformat(rec["fin"]),
                "matiere": rec.get("matiere", ""),
                "salle": rec.get("salle", ""),
                "description": rec.get("description", ""),
                "uid": rec.get("uid", ""),
                "code": rec.get("code", ""),
                "intitule": rec.get("intitule", ""),
                "enseignants": rec.get("enseignants", []),
                "type": rec.get("type_ac", "Cours"),
                "formations": [],
                "salles_liste": [],
                "groupes": [],
                "maj": rec.get("maj", ""),
            })
            for ligne in rec.get("description", "").split("\n"):
                ligne = ligne.strip()
                if ":" not in ligne:
                    continue
                cle, _, valeur = ligne.partition(":")
                cle = cle.strip().lower()
                valeurs = [v.strip() for v in valeur.split(",") if v.strip()]
                if cle in ("formation", "formations"):
                    evenements[-1]["formations"] += valeurs
                elif cle in ("salle", "salles"):
                    evenements[-1]["salles_liste"] += valeurs
                elif cle in ("groupe", "groupes"):
                    evenements[-1]["groupes"] += [g for g in (nettoyage_groupe(v) for v in valeurs) if g]
        except Exception:
            continue
    return evenements


def classer_champs(evenements):
    formations = {}
    salles = {}
    for e in evenements:
        for f in e.get("formations", []) or []:
            formations.setdefault(f, f.lstrip("."))
        for s in e.get("salles_liste", []) or []:
            salles.setdefault(s, s)
    return {
        "formations": dict(sorted(formations.items(), key=lambda kv: kv[0].lower())),
        "salles": dict(sorted(salles.items(), key=lambda kv: kv[0].lower())),
    }


def obtenir_donnees(url_ics: str, autoriser_cache=True):
    maintenant = time.time()
    if (autoriser_cache and CACHE["url"] == url_ics and CACHE["evenements"]
            and maintenant - CACHE["quand"] < DUREE_CACHE):
        return CACHE["evenements"], CACHE["ressources"]
    texte = horaires.telecharger_ics(url_ics)
    evenements = horaires.parser_ics(texte)
    enregistrer_dans_archive(evenements)
    for e in evenements:
        e["formations"] = []
        e["salles_liste"] = []
        e["groupes"] = []
        infos = analyser(e)
        e.update({"code": infos["code"], "intitule": infos["intitule"],
                  "enseignants": infos["enseignants"], "type": infos["type"]})
        for ligne in (e.get("description") or "").split("\n"):
            ligne = ligne.strip()
            if ":" not in ligne:
                continue
            cle, _, valeur = ligne.partition(":")
            cle = cle.strip().lower()
            valeurs = [v.strip() for v in valeur.split(",") if v.strip()]
            if cle in ("formation", "formations"):
                e["formations"] += valeurs
            elif cle in ("salle", "salles"):
                e["salles_liste"] += valeurs
            elif cle in ("groupe", "groupes"):
                e["groupes"] += [g for g in (nettoyage_groupe(v) for v in valeurs) if g]
    CACHE.update(url=url_ics, quand=maintenant, evenements=evenements,
                 ressources=classer_champs(evenements))
    marquer_memoire()
    return evenements, CACHE["ressources"]


def boucle_capture(url_ics):
    """Ne démarre plus la boucle annuelle ici : voir main()."""
    def tour():
        while True:
            time.sleep(INTERVALLE_REPRISE if not CACHE["evenements"] else INTERVALLE_CAPTURE)
            try:
                obtenir_donnees(url_ics, autoriser_cache=False)
            except Exception:
                traceback.print_exc(file=sys.stderr)
    fil = threading.Thread(target=tour, daemon=True)
    fil.start()


def evenements_en_json(evenements, groupe=""):
    resultat = []
    for e in evenements:
        debut = e["debut"].astimezone(horaires.TZ_LOCAL)
        fin = e["fin"].astimezone(horaires.TZ_LOCAL)
        duree = max((fin - debut).total_seconds() // 60, 0)
        # les flux de TD suffixent la matière par « - <formation>Groupe / type » :
        # nettoyage à l'affichage (guérit aussi les entrées déjà archivées)
        matiere_propre = (e.get("matiere") or "").split(" - <")[0].strip()
        if groupe:
            matiere_propre = retirer_suffixe_td(matiere_propre, groupe)
        infos = analyser({"matiere": matiere_propre, "description": e.get("description", "")})
        intitule_propre = (e.get("intitule") or infos["intitule"]).split(" - <")[0].strip()
        if groupe:
            intitule_propre = retirer_suffixe_td(intitule_propre, groupe)
        # mémo UMONS (« Mémo : Juste avant I-TRMI-034 »…) : nettoyé, sans
        # doublon du type déjà affiché comme étiquette (comparaison sans
        # accents ni pluriel : « Exercice coté » == « Exercices cotés »)
        memo = nettoyage_groupe(e.get("memo") or infos.get("memo") or "")
        if memo and (forme_lemme(memo) == forme_lemme(infos["type"] or "")
                     or forme_lemme(memo) == forme_lemme(intitule_propre)):
            # le mémo ne fait que répéter le type ou l'intitulé : inutile
            memo = ""
        resultat.append({
            "debut": debut.isoformat(),
            "fin": fin.isoformat(),
            "duree_min": int(duree),
            "matiere": matiere_propre,
            "salle": e.get("salle", ""),
            "description": e.get("description", ""),
            "formations": e.get("formations") or [],
            "salles_liste": e.get("salles_liste") or [],
            "groupes": e.get("groupes") or [],
            "code": e.get("code") or infos["code"],
            "intitule": intitule_propre or e.get("intitule", ""),
            "memo": memo,
            "enseignants": e.get("enseignants") or infos["enseignants"],
            # type toujours recalculé à l'affichage : guérit les entrées
            # archivées avant l'extension des types (Projet, TFE, Tutorat…)
            "type": infos["type"],
            "maj": e.get("maj", ""),
        })
    return resultat


PAGE = Path(__file__).with_name("page_horaires.html")


def url_autorisee(url: str) -> bool:
    """Le paramètre ?url= fait télécharger une adresse par le serveur : on la
    limite aux hôtes HyperPlanning configurés (sinon n'importe quel client du
    réseau pourrait faire sonder le LAN / les services internes du NAS)."""
    try:
        cible = urlparse(url)
    except ValueError:
        return False
    if cible.scheme not in ("http", "https") or not cible.hostname:
        return False
    autorises = {urlparse(horaires.BASE).hostname,
                 urlparse(horaires.URL_SALLES).hostname,
                 urlparse(os.environ.get("HP_URL", "")).hostname}
    return cible.hostname.lower() in {h.lower() for h in autorises if h}


VERSION_CODE = "inconnue"


def determiner_version():
    global VERSION_CODE
    import os
    import subprocess
    VERSION_CODE = os.environ.get("HP_VERSION", "inconnue")
    if VERSION_CODE not in ("inconnue", ""):
        return
    dossier = Path(__file__).parent
    try:
        VERSION_CODE = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], cwd=dossier,
            capture_output=True, text=True, timeout=5,
        ).stdout.strip() or "inconnue"
    except Exception:
        pass


def filtrer_evenements(evenements, type_ressource, ressource, valeur_groupe,
                       filtre, lundi=None, dimanche=None):
    """Filtre commun des requêtes web : ressource (formation/salle), groupe,
   mot-clé, et — si lundi/dimanche fournis — la fenêtre d'une semaine."""
    selection = []
    for e in evenements:
        if lundi is not None and not (lundi <= e["debut"].astimezone(horaires.TZ_LOCAL) < dimanche):
            continue
        if type_ressource == "formation" and ressource and ressource not in (e.get("formations") or []):
            continue
        if type_ressource == "salle" and ressource and ressource not in (e.get("salles_liste") or []):
            continue
        dessous = " ".join(
            [e.get("matiere", ""), e.get("salle", ""), e.get("description", ""),
             " ".join(e.get("formations") or []), " ".join(e.get("salles_liste") or [])]
        ).lower()
        if filtre and filtre not in dessous:
            continue
        dessous_g = dessous + " " + " ".join(e.get("groupes") or []).lower()
        if filtre and filtre not in dessous_g:
            continue
        if valeur_groupe:
            groupes_e = [g for g in (e.get("groupes") or []) if g]
            if valeur_groupe not in groupes_e:
                cours_avec_ressource = (type_ressource == "formation" and ressource) or type_ressource == "salle"
                if groupes_e or not cours_avec_ressource:
                    continue
        selection.append(e)
    return selection


class Controleur(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _envoyer(self, code, corps, type_contenu):
        octets = corps if isinstance(corps, bytes) else corps.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", type_contenu + "; charset=utf-8")
        self.send_header("Content-Length", str(len(octets)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(octets)

    def do_GET(self):
        analyse = urlparse(self.path)
        params = parse_qs(analyse.query)
        try:
            if analyse.path in ("/", "/index.html"):
                self._envoyer(200, PAGE.read_text(encoding="utf-8"), "text/html")
            elif analyse.path == "/api/version":
                self._envoyer(200, json.dumps({"version": VERSION_CODE}), "application/json")
            elif analyse.path == "/api/semaines":
                # comptage PAR SEMAINE pour la sélection courante (vue année)
                type_ressource = (params.get("type", [""])[0] or "").strip().lower()
                ressource = (params.get("ressource", [""])[0] or "").strip()
                valeur_groupe = (params.get("groupe", [""])[0] or "").strip()
                filtre = (params.get("filtre", [""])[0] or "").strip().lower()
                selection = filtrer_evenements(liste_complete(), type_ressource,
                                               ressource, valeur_groupe, filtre)
                # compter seulement les vrais cours : les marqueurs journée
                # entière (Vacances, Férié) n'affichent rien dans la grille
                selection = [e for e in selection if not est_marqueur_journee(e)]
                compte = {}
                for e in selection:
                    d = e["debut"].astimezone(horaires.TZ_LOCAL).date()
                    lundi = (d - timedelta(days=d.weekday())).isoformat()
                    compte[lundi] = compte.get(lundi, 0) + 1
                lunes = sorted(compte)
                self._envoyer(200, json.dumps({
                    "semaines": [{"debut": l, "cours": compte[l]} for l in lunes],
                    "nombre": len(lunes),
                    "premiere": lunes[0] if lunes else "",
                    "derniere": lunes[-1] if lunes else "",
                    "annuel_pret": ANNUEL_PRET,
                }, ensure_ascii=False), "application/json")
            elif analyse.path == "/api/ressources":
                url_explicite = (params.get("url", [""])[0] or "").strip()
                if url_explicite:
                    if not url_autorisee(url_explicite):
                        self._envoyer(400, json.dumps({"erreur": "URL iCal non autorisée (hôte HyperPlanning attendu)."}), "application/json")
                        return
                    try:
                        _, ressources = obtenir_donnees(url_explicite)
                    except Exception as err:
                        # si HyperPlanning est injoignable, servir quand même le catalogue de l'archive
                        print(f"Ressources live indisponibles ({err}) — repli archive.", file=sys.stderr)
                        ressources = classer_champs(evenements_archive())
                type_ressource = (params.get("type", [""])[0] or "").strip().lower()
                ressource = (params.get("ressource", [""])[0] or "").strip()
                groupes = {}
                # MODE MÉMOIRE : catalogue servi depuis le pool complet en
                # mémoire — aucun téléchargement au chargement de la page
                eve_mem = liste_complete()
                if not url_explicite:
                    if eve_mem:
                        ressources = classer_champs(eve_mem)
                        sources_cat = eve_mem
                    else:
                        # Premier démarrage sans archive : replis live/archive
                        try:
                            _, ressources = obtenir_donnees(os.environ.get("HP_URL", horaires.URL_SALLES))
                            sources_cat = list(CACHE["evenements"]) + evenements_archive()
                        except Exception as err:
                            print(f"Ressources live indisponibles ({err}) — repli archive.", file=sys.stderr)
                            ressources = classer_champs(evenements_archive())
                            sources_cat = eve_mem
                else:
                    sources_cat = list(CACHE["evenements"]) + evenements_archive()
                if type_ressource == "formation" and ressource and not url_explicite:
                    # groupes exprimés dans les descriptions des flux annuels :
                    # d'abord le pool mémoire, puis repli à la demande tant que
                    # la passe de fond n'a pas encore traité cette formation
                    if not ANNUEL_PRET and ressource in CATALOGUE_ANNUEL and ressource not in ANNUEL_MEMOIRE:
                        try:
                            sources_cat = sources_cat + obtenir_annuel(ressource)
                        except Exception:
                            traceback.print_exc(file=sys.stderr)
                elif type_ressource == "formation" and ressource:
                    try:
                        sources_cat = sources_cat + obtenir_annuel(ressource)
                    except Exception:
                        traceback.print_exc(file=sys.stderr)
                for e in sources_cat:
                    if type_ressource == "formation" and ressource:
                        if ressource not in (e.get("formations") or []):
                            continue
                        for g in e.get("groupes") or []:
                            if g:
                                groupes.setdefault(g, libelle_court_td(g))
                    else:
                        for g in e.get("groupes") or []:
                            if g:
                                groupes.setdefault(g, libelle_court_td(g))
                if type_ressource == "formation" and ressource and CATALOGUE_GROUPES:
                    # groupes de TD récoltés (flux iCal annuels) pour cette formation
                    tds = (CATALOGUE_GROUPES.get(ressource)
                           or CATALOGUE_GROUPES.get("." + ressource.lstrip(".")) or {})
                    for td in tds:
                        if td:
                            groupes.setdefault(td, libelle_court_td(td))
                # dédoublonnage approx : la même formation peut avoir les mêmes
                # groupes exprimés avec virgules répétées
                if CATALOGUE_ANNUEL:
                    formations_aff = dict(ressources["formations"])
                    for libelle in CATALOGUE_ANNUEL:
                        formations_aff.setdefault(libelle, libelle.lstrip("."))
                    ressources = {"formations": dict(sorted(formations_aff.items(), key=lambda kv: kv[0].lower())),
                                  "salles": ressources["salles"]}
                self._envoyer(200, json.dumps({
                    "formations": [{"valeur": v, "affichage": a} for v, a in ressources["formations"].items()],
                    "salles": [{"valeur": s, "affichage": a} for s, a in ressources["salles"].items()],
                    "groupes": [{"valeur": v, "affichage": a}
                                for v, a in sorted(groupes.items(), key=lambda kv: kv[0].lower())],
                }, ensure_ascii=False), "application/json")
            elif analyse.path == "/api/horaires":
                type_ressource = (params.get("type", [""])[0] or "").strip().lower()
                ressource = (params.get("ressource", [""])[0] or "").strip()
                valeur_groupe = (params.get("groupe", [""])[0] or "").strip()
                url_explicite = (params.get("url", [""])[0] or "").strip()
                if url_explicite:
                    # flux de secours fourni par la page : téléchargement direct
                    if not url_autorisee(url_explicite):
                        self._envoyer(400, json.dumps({"erreur": "URL iCal non autorisée (hôte HyperPlanning attendu)."}), "application/json")
                        return
                    try:
                        evenements_bruts, _ = obtenir_donnees(url_explicite)
                    except Exception as err:
                        print(f"Téléchargement impossible ({err}); repli sur l'archive.", file=sys.stderr)
                        evenements_bruts = []
                    with VERROU_ANNUEL:
                        annuels_mem = [e for e2 in ANNUEL_MEMOIRE.values() for e in e2["evenements"]]
                    evenements = fusionner_sources(
                        [("annuel", annuels_mem), ("live", evenements_bruts)]
                        + ([("archive", evenements_archive())] if not evenements_bruts else []))
                else:
                    # MODE MÉMOIRE : tout a été récupéré par les passes de fond.
                    # Aucune requête réseau ici — réponse quasi instantanée.
                    complements = []
                    if type_ressource == "formation" and ressource:
                        with VERROU_ANNUEL:
                            a_annuel = ressource in ANNUEL_MEMOIRE
                            a_groupe = (ressource + "\x00" + valeur_groupe) in GROUPES_MEMOIRE if valeur_groupe else True
                        # replis uniquement tant que les passes de fond ne sont
                        # pas terminées (ou pour une ressource non couverte)
                        if CATALOGUE_ANNUEL and not a_annuel and ressource in CATALOGUE_ANNUEL and not ANNUEL_PRET:
                            try:
                                complements += obtenir_annuel(ressource)
                            except Exception:
                                traceback.print_exc(file=sys.stderr)
                        if valeur_groupe and CATALOGUE_GROUPES and not a_groupe and not ANNUEL_GROUPES_PRET:
                            try:
                                complements += obtenir_annuel_groupe(ressource, valeur_groupe)
                            except Exception:
                                traceback.print_exc(file=sys.stderr)
                    evenements = liste_complete()
                    if complements:
                        evenements = fusionner_sources([("annuel", complements), ("memoire", evenements)])

                lundi = None
                brut = params.get("semaine", [""])[0]
                if brut:
                    try:
                        lundi = datetime.strptime(brut, "%Y-%m-%d").replace(tzinfo=horaires.TZ_LOCAL)
                    except ValueError:
                        lundi = None
                if lundi is None:
                    lundi = datetime.now(horaires.TZ_LOCAL)
                lundi = (lundi - timedelta(days=lundi.weekday())).replace(
                    hour=0, minute=0, second=0, microsecond=0)
                dimanche = lundi + timedelta(days=7)

                filtre = (params.get("filtre", [""])[0] or "").strip().lower()
                groupe = (params.get("groupe", [""])[0] or "").strip()

                selection = filtrer_evenements(evenements, type_ressource, ressource,
                                               valeur_groupe, filtre, lundi, dimanche)
                selection.sort(key=lambda e: e["debut"])

                if url_explicite:
                    # flux de secours fourni par la page : fenêtre de publication live
                    if evenements_bruts:
                        fenetre = {
                            "debut": min(e["debut"] for e in evenements_bruts).date().isoformat(),
                            "fin": max(e["fin"] for e in evenements_bruts).date().isoformat(),
                        }
                    else:
                        fenetre = None
                else:
                    # mode mémoire : la fenêtre de couverture est celle du pool
                    # complet (l'année entière une fois la passe de fond finie)
                    if evenements:
                        fenetre = {
                            "debut": min(e["debut"] for e in evenements).date().isoformat(),
                            "fin": max(e["fin"] for e in evenements).date().isoformat(),
                        }
                    else:
                        fenetre = None
                if fenetre:
                    debut_semaine = lundi.date()
                    fin_semaine = (dimanche - timedelta(days=1)).date()
                    hors_fenetre = (fin_semaine < datetime.fromisoformat(fenetre["debut"]).date()
                                    or debut_semaine > datetime.fromisoformat(fenetre["fin"]).date())
                else:
                    hors_fenetre = True
                if not selection:
                    source = "vide"
                else:
                    sources_selection = {e.get("_source") for e in selection}
                    if "live" in sources_selection and not hors_fenetre:
                        source = "live"
                    elif "annuel" in sources_selection:
                        source = "annuel"
                    elif "live" in sources_selection:
                        source = "live"
                    else:
                        source = "archive"

                self._envoyer(200, json.dumps({
                    "semaine": lundi.strftime("%Y-%m-%d"),
                    "filtre": filtre,
                    "type": type_ressource,
                    "ressource": ressource,
                    "groupe": groupe,
                    "fenetre": fenetre,
                    "source": source,
                    "cours": evenements_en_json(selection, groupe=valeur_groupe),
                }, ensure_ascii=False), "application/json")
            else:
                self._envoyer(404, "Introuvable", "text/plain")
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as err:
            traceback.print_exc(file=sys.stderr)
            self._envoyer(500, json.dumps({"erreur": str(err)}), "application/json")


def main(argv=None):
    parseur = argparse.ArgumentParser(description="Serveur web d'affichage de l'horaire HyperPlanning.")
    parseur.add_argument("--port", type=int, default=int(os.environ.get("HP_PORT", "8000")))
    parseur.add_argument("--host", default=os.environ.get("HP_HOST", "127.0.0.1"))
    parseur.add_argument("--url", default=os.environ.get("HP_URL", horaires.URL_SALLES),
                         help="URL iCal affichée par défaut (ou variable d'environnement HP_URL)")
    args = parseur.parse_args(argv)

    determiner_version()
    charger_archive()
    charger_catalogue_annuel()
    charger_catalogue_groupes()
    # redémarrage " chaud " : la mémoire part de l'archive persistée, aucune
    # attente de réseau pour servir les premières requêtes
    amorcer_memoire()
    global ANNUEL_PRET
    if INTERVALLE_ANNUEL > 0 and CATALOGUE_ANNUEL:
        boucle_annuel()
    else:
        ANNUEL_PRET = True   # pas de passe annuelle : ne pas afficher « en cours » indéfiniment
    try:
        evenements, ressources = obtenir_donnees(args.url, autoriser_cache=False)
        dates = [e["debut"].astimezone(horaires.TZ_LOCAL).date() for e in evenements] if evenements else []
        print(f"Serveur prêt : {len(evenements)} cours en cache "
              f"({min(dates)} -> {max(dates)})." if dates else "Serveur prêt : aucune donnée live.", file=sys.stderr)
        print(f"Ressources : {len(ressources['formations'])} formations, {len(ressources['salles'])} salles, "
              f"archive : {len(ARCHIVE)} cours enregistrés.", file=sys.stderr)
    except Exception as err:
        # vital en conteneur (TrueNAS) : ne pas crasher si HyperPlanning est injoignable,
        # on démarre quand même et on sert l'archive disponible
        print(f"Téléchargement initial impossible ({err}) — démarrage sur archive seule.", file=sys.stderr)
        evenements, ressources = evenements_archive(), classer_champs(evenements_archive())
    boucle_capture(args.url)
    serveur = ThreadingHTTPServer((args.host, args.port), Controleur)
    print(f"Ouvrez http://{args.host}:{args.port} dans votre navigateur.", file=sys.stderr)
    try:
        serveur.serve_forever()
    except KeyboardInterrupt:
        print("\nArrêt du serveur.", file=sys.stderr)
        sauver_archive()


if __name__ == "__main__":
    main()
