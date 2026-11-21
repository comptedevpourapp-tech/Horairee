#!/usr/bin/env python3
"""Mini site web pour afficher l'horaire HyperPlanning (type hplanning2026.umons.ac.be/invite).

Lance le serveur :
    python3 serveur_horaires.py [--port 8000] [--host 127.0.0.1] [--url URL_ICAL]

- Sélecteur de ressources (formations, salles, groupes) reconstruit depuis
  l'export public « toutes salles », comme dans l'interface d'origine.
- Archive locale persistante (HP_ARCHIVE) : les semaines publiées (fenêtre
  glissante ≈ 3 semaines) sont enregistrées à chaque consultation et via une
  capture automatique périodique (CAPTURE_HEURES, défaut 6 h), ce qui permet
  de revoir les semaines passées même quand le serveur ne les publie plus.
- Variables d'environnement : HP_HOST, HP_PORT, HP_URL, HP_ARCHIVE, CAPTURE_HEURES.

Aucune dépendance externe : bibliothèque standard uniquement.
"""

import argparse
import json
import os
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
INTERVALLE_CAPTURE = float(os.environ.get("CAPTURE_HEURES", "6")) * 3600

CACHE = {"url": None, "quand": 0.0, "evenements": [], "ressources": None}
ARCHIVE = {}
DUREE_CACHE = 300


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
    def tour():
        while True:
            time.sleep(INTERVALLE_CAPTURE)
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
                if type_ressource == "formation" and ressource:
                    for e in list(CACHE["evenements"]) + evenements_archive():
                        if ressource in (e.get("formations") or []):
                            for g in e.get("groupes") or []:
                                if g:
                                    groupes.setdefault(g, g)
                    # dédoublonnage approx : la même formation peut avoir les mêmes
                    # groupes exprimés avec virgules répétées
                else:
                    for e in list(CACHE["evenements"]) + evenements_archive():
                        for g in e.get("groupes") or []:
                            if g:
                                groupes.setdefault(g, g)
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

                fusion = {}
                for e in evenements_archive():
                    fusion[e.get("uid")] = e
                for e in evenements_bruts:
                    fusion[e.get("uid")] = e
                evenements = list(fusion.values())

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
                type_ressource = (params.get("type", [""])[0] or "").strip().lower()
                ressource = (params.get("ressource", [""])[0] or "").strip()
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
                    source = "archive" if hors_fenetre else "live"
                    if hors_fenetre and not selection:
                        source = "vide"
                else:
                    fenetre = None
                    source = "archive" if selection else "vide"

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
