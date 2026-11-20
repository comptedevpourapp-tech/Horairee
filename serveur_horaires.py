#!/usr/bin/env python3
"""Mini site web pour afficher l'horaire HyperPlanning (type hplanning2026.umons.ac.be/invite).

Lance le serveur :
    python3 serveur_horaires.py [--port 8000] [--url URL_ICAL]

Puis ouvre http://127.0.0.1:8000 dans le navigateur.

- Par défaut l'horaire affiché est l'export public « toutes salles » de l'UMONS.
- Le champ « URL iCal » de la page permet de coller un lien iCal (.ics) de
  n'importe quelle ressource (diplôme, promotion, salle, enseignant).
- Le champ « Filtre » restreint l'affichage (ex. 'BAB2 - Ir. civil').

Aucune dépendance externe : bibliothèque standard uniquement.
Le serveur n'écoute que sur 127.0.0.1 (local).
"""

import argparse
import json
import sys
import time
import urllib.request
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import horaires

CACHE = {"url": None, "quand": 0.0, "evenements": []}
DUREE_CACHE = 300  # secondes


def obtenir_evenements(url_ics: str):
    maintenant = time.time()
    if CACHE["url"] == url_ics and CACHE["evenements"] and maintenant - CACHE["quand"] < DUREE_CACHE:
        return CACHE["evenements"]
    texte = horaires.telecharger_ics(url_ics)
    evenements = horaires.parser_ics(texte)
    CACHE.update(url=url_ics, quand=maintenant, evenements=evenements)
    return evenements


def evenements_en_json(evenements):
    resultat = []
    for e in evenements:
        debut = e["debut"].astimezone(horaires.TZ_LOCAL)
        fin = e["fin"].astimezone(horaires.TZ_LOCAL)
        resultat.append({
            "debut": debut.isoformat(),
            "fin": fin.isoformat(),
            "minuteDebut": debut.hour * 60 + debut.minute,
            "minuteFin": max(fin.hour * 60 + fin.minute, debut.hour * 60 + debut.minute + 15),
            "jour": debut.strftime("%A"),
            "matiere": e.get("matiere", ""),
            "salle": e.get("salle", ""),
            "description": e.get("description", ""),
        })
    return resultat


PAGE = Path(__file__).with_name("page_horaires.html")


class Controleur(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _envoyer(self, code, corps, type_contenu):
        octets = corps if isinstance(corps, bytes) else corps.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", type_contenu + "; charset=utf-8")
        self.send_header("Content-Length", str(len(octets)))
        self.end_headers()
        self.wfile.write(octets)

    def do_GET(self):
        analyse = urlparse(self.path)
        params = parse_qs(analyse.query)
        try:
            if analyse.path in ("/", "/index.html"):
                self._envoyer(200, PAGE.read_text(encoding="utf-8"), "text/html")
            elif analyse.path == "/api/horaires":
                url_ics = params.get("url", [horaires.URL_SALLES])[0] or horaires.URL_SALLES
                evenements = evenements_en_json(obtenir_evenements(url_ics))
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
                selection = [
                    e for e in evenements
                    if lundi <= datetime.fromisoformat(e["debut"]) < dimanche
                    and (not filtre or filtre in (e["matiere"] + " " + e["salle"] + " " + e["description"]).lower())
                ]
                self._envoyer(200, json.dumps({
                    "semaine": lundi.strftime("%Y-%m-%d"),
                    "filtre": filtre,
                    "cours": selection,
                }, ensure_ascii=False), "application/json")
            else:
                self._envoyer(404, "Introuvable", "text/plain")
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as err:
            self._envoyer(500, json.dumps({"erreur": str(err)}), "application/json")


def main(argv=None):
    parseur = argparse.ArgumentParser(description="Serveur web d'affichage de l'horaire HyperPlanning.")
    parseur.add_argument("--port", type=int, default=8000)
    parseur.add_argument("--url", default=horaires.URL_SALLES, help="URL iCal affichée par défaut")
    args = parseur.parse_args(argv)

    evenements = evenements_en_json(obtenir_evenements(args.url))
    if evenements:
        dates = [e["debut"][:10] for e in evenements]
        print(f"Serveur prêt : {len(evenements)} cours en cache ({min(dates)} -> {max(dates)}).", file=sys.stderr)
    serveur = ThreadingHTTPServer(("127.0.0.1", args.port), Controleur)
    print(f"Ouvrez http://127.0.0.1:{args.port} dans votre navigateur.", file=sys.stderr)
    try:
        serveur.serve_forever()
    except KeyboardInterrupt:
        print("\nArrêt du serveur.", file=sys.stderr)


if __name__ == "__main__":
    main()
