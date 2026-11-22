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
  CAPTURE_HEURES, HP_LIENS_ANNUELS, HP_ANNUEL_HEURES.

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
INTERVALLE_ANNUEL = float(os.environ.get("HP_ANNUEL_HEURES", "24")) * 3600
DELAIS_ANNUEL = 0.4      # pause entre deux flux annuels (politesse envers le serveur UMONS)
DELAI_BOUCLE_ANNUEL = 30  # attendue avant la première passe annuelle au démarrage

CACHE = {"url": None, "quand": 0.0, "evenements": [], "ressources": None}
ARCHIVE = {}
DUREE_CACHE = 300

CATALOGUE_ANNUEL = {}          # libellé formation -> lien iCal annuel
ANNUEL_MEMOIRE = {}            # libellé -> {quand, evenements} (cache TTL)
VERROU_ANNUEL = threading.Lock()

PREFIXE_COURS = re.compile(r"^Cours-(\d+)-(\d+)-")


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
    if catalogue:
        print(f"Catalogue de flux annuels chargé : {len(catalogue)} formations.")
    return catalogue


def cle_dedup(eve):
    """Un même cours apparaît avec des UID différents selon l'export
    (toutes salles vs flux par formation) mais partage le préfixe
    Cours-<id>-<num>- . On dédoublonne sur (début, préfixe)."""
    debut = eve["debut"].astimezone(horaires.TZ_LOCAL).isoformat()
    m = PREFIXE_COURS.match(eve.get("uid") or "")
    suffixe = m.group(0) if m else (eve.get("uid") or "")
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
    if FICHIER_ARCHIVE.exists():
        try:
            ARCHIVE = json.loads(FICHIER_ARCHIVE.read_text(encoding="utf-8"))
        except Exception as err:
            print(f"Archive illisible ({err}), démarrage à vide.", file=sys.stderr)
            ARCHIVE = {}


def sauver_archive():
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
        if actuel is None or actuel.get("maj") != rec["maj"]:
            rec["vu"] = actuel.get("vu", rec["vu"]) if actuel else rec["vu"]
            ARCHIVE[uid] = rec
            modifie = True
    if modifie or not FICHIER_ARCHIVE.exists():
        sauver_archive()


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
        try:
            evenements = horaires.parser_ics(horaires.telecharger_ics(lien))
        except Exception as err:
            print(f"Flux annuel indisponible pour {ressource} ({err}).", file=sys.stderr)
            if entree and maint - entree["quand"] < 7 * 86400:
                return entree["evenements"]
            return []
        for e in evenements:
            desc = e.get("description") or ""
            if "formation" not in desc.lower():
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
                    e["groupes"] += valeurs
        enregistrer_dans_archive(evenements)
        ANNUEL_MEMOIRE[ressource] = {"quand": maint, "evenements": evenements}
        return evenements


def boucle_annuel():
    """En arrière-plan : télécharge les flux annuels de toutes les formations
    du catalogue et les fusionne dans l'archive (cycle HP_ANNUEL_HEURES)."""
    def tour():
        time.sleep(DELAI_BOUCLE_ANNUEL)
        while True:
            for ressource in list(CATALOGUE_ANNUEL):
                try:
                    obtenir_annuel(ressource)
                except Exception:
                    traceback.print_exc(file=sys.stderr)
                time.sleep(DELAIS_ANNUEL)
            time.sleep(max(INTERVALLE_ANNUEL, 3600))
    fil = threading.Thread(target=tour, daemon=True, name="annuel")
    fil.start()


def evenements_archive():
    evenements = []
    for rec in ARCHIVE.values():
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
                    evenements[-1]["groupes"] += valeurs
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
                e["groupes"] += valeurs
    CACHE.update(url=url_ics, quand=maintenant, evenements=evenements,
                 ressources=classer_champs(evenements))
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


def evenements_en_json(evenements):
    resultat = []
    for e in evenements:
        debut = e["debut"].astimezone(horaires.TZ_LOCAL)
        fin = e["fin"].astimezone(horaires.TZ_LOCAL)
        duree = max((fin - debut).total_seconds() // 60, 0)
        infos = analyser(e)
        resultat.append({
            "debut": debut.isoformat(),
            "fin": fin.isoformat(),
            "duree_min": int(duree),
            "matiere": e.get("matiere", ""),
            "salle": e.get("salle", ""),
            "description": e.get("description", ""),
            "formations": e.get("formations") or [],
            "salles_liste": e.get("salles_liste") or [],
            "groupes": e.get("groupes") or [],
            "code": e.get("code") or infos["code"],
            "intitule": e.get("intitule") or infos["intitule"],
            "enseignants": e.get("enseignants") or infos["enseignants"],
            "type": e.get("type") or infos["type"],
            "maj": e.get("maj", ""),
        })
    return resultat


PAGE = Path(__file__).with_name("page_horaires.html")


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
                compte = {}
                for e in list(CACHE["evenements"]) + evenements_archive():
                    d = e["debut"].astimezone(horaires.TZ_LOCAL).date()
                    lundi = (d - timedelta(days=d.weekday())).isoformat()
                    compte[lundi] = compte.get(lundi, 0) + 1
                lunes = sorted(compte)
                self._envoyer(200, json.dumps({
                    "semaines": [{"debut": l, "cours": compte[l]} for l in lunes],
                    "nombre": len(lunes),
                    "premiere": lunes[0] if lunes else "",
                    "derniere": lunes[-1] if lunes else "",
                }, ensure_ascii=False), "application/json")
            elif analyse.path == "/api/ressources":
                url_ics = params.get("url", [os.environ.get("HP_URL", horaires.URL_SALLES)])[0] or horaires.URL_SALLES
                try:
                    _, ressources = obtenir_donnees(url_ics)
                except Exception as err:
                    # si HyperPlanning est injoignable, servir quand même le catalogue de l'archive
                    print(f"Ressources live indisponibles ({err}) — repli archive.", file=sys.stderr)
                    ressources = classer_champs(evenements_archive())
                type_ressource = (params.get("type", [""])[0] or "").strip().lower()
                ressource = (params.get("ressource", [""])[0] or "").strip()
                groupes = {}
                sources_cat = list(CACHE["evenements"]) + evenements_archive()
                if type_ressource == "formation" and ressource:
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
                                groupes.setdefault(g, g)
                    else:
                        for g in e.get("groupes") or []:
                            if g:
                                groupes.setdefault(g, g)
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
                    "groupes": sorted(groupes, key=str.lower),
                }, ensure_ascii=False), "application/json")
            elif analyse.path == "/api/horaires":
                url_ics = params.get("url", [os.environ.get("HP_URL", horaires.URL_SALLES)])[0] or horaires.URL_SALLES
                try:
                    evenements_bruts, _ = obtenir_donnees(url_ics)
                    source = "live"
                except Exception as err:
                    print(f"Téléchargement impossible ({err}); repli sur l'archive.", file=sys.stderr)
                    evenements_bruts = []
                    source = "archive"

                type_ressource = (params.get("type", [""])[0] or "").strip().lower()
                ressource = (params.get("ressource", [""])[0] or "").strip()

                evenements_annuels = []
                if CATALOGUE_ANNUEL and type_ressource == "formation" and ressource:
                    try:
                        evenements_annuels = obtenir_annuel(ressource)
                    except Exception:
                        traceback.print_exc(file=sys.stderr)

                evenements = fusionner_sources([
                    ("archive", evenements_archive()), ("live", evenements_bruts),
                    ("annuel", evenements_annuels),
                ])

                lundi = None
                brut = params.get("semaine", [""])[0]
                if brut:
                    try:
                        lundi = datetime.strptime(brut, "%Y-%m-%d").replace(tzinfo=horaires.TZ_LOCAL)
                    except ValueError:
                        lundi = None
                if lundi is None:
                    aujourdhui = datetime.now(horaires.TZ_LOCAL)
                    lundi = aujourdhui - timedelta(days=aujourdhui.weekday())
                dimanche = lundi + timedelta(days=7)

                filtre = (params.get("filtre", [""])[0] or "").strip().lower()
                groupe = (params.get("groupe", [""])[0] or "").strip()

                selection = []
                for e in evenements:
                    if not (lundi <= e["debut"].astimezone(horaires.TZ_LOCAL) < dimanche):
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
                    if groupe:
                        groupes_e = [g for g in (e.get("groupes") or []) if g]
                        if groupe not in groupes_e:
                            cours_avec_ressource = (type_ressource == "formation" and ressource) or type_ressource == "salle"
                            if groupes_e or not cours_avec_ressource:
                                continue
                    selection.append(e)
                selection.sort(key=lambda e: e["debut"])

                if evenements_bruts:
                    fenetre = {
                        "debut": min(e["debut"] for e in evenements_bruts).date().isoformat(),
                        "fin": max(e["fin"] for e in evenements_bruts).date().isoformat(),
                    }
                    debut_semaine = lundi.date()
                    fin_semaine = (dimanche - timedelta(days=1)).date()
                    hors_fenetre = (fin_semaine < datetime.fromisoformat(fenetre["debut"]).date()
                                    or debut_semaine > datetime.fromisoformat(fenetre["fin"]).date())
                else:
                    fenetre = None
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
                    "cours": evenements_en_json(selection),
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
    if INTERVALLE_ANNUEL > 0 and CATALOGUE_ANNUEL:
        boucle_annuel()
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
