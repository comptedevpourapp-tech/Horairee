#!/usr/bin/env python3
"""Récupère et affiche l'horaire d'un planning HyperPlanning de type
https://hplanning2026.umons.ac.be/invite (Université de Mons - INDEX-ÉDUCATION).

Le site HyperPlanning expose l'emploi du temps au format iCalendar (.ics).
Deux façons de l'utiliser :

1. Sans argument : le programme télécharge l'export public « toutes salles »
   du serveur UMONS (vérifie que la connexion marche et liste les cours
   publiés).

2. Avec l'URL iCal d'un calendrier précis : dans l'interface
   https://hplanning2026.umons.ac.be/invite?fd=1, choisir sa ressource
   (diplôme / promotion / salle / enseignant), cliquer sur le bouton iCal
   (ou QR code) et copier le « lien permanent » qui ressemble à
   https://hplanning2026.umons.ac.be/ical/...  Passer cette URL au programme.

Exemples :
    python3 horaires.py
    python3 horaires.py "https://hplanning2026.umons.ac.be/ical/....ics?..."
    python3 horaires.py --du 2026-11-23 --au 2026-11-27
    python3 horaires.py --format json
    python3 horaires.py --format csv -o calendrier.csv

Sans dépendance externe : uniquement la bibliothèque standard (Python >= 3.9).
"""

import argparse
import contextlib
import csv
import io
import json
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

BASE = "https://hplanning2026.umons.ac.be"
VERSION_HP = "2026.5.8"
URL_SALLES = (
    f"{BASE}/ical/exportSalles.ics"
    f"?icalSansRessource=Salles&version={VERSION_HP}&fh=Europe%2FBrussels&f=0"
)
TZ_LOCAL = ZoneInfo("Europe/Brussels")
UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)


def telecharger_ics(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=60) as reponse:
            if reponse.status != 200:
                raise RuntimeError(f"Le serveur a répondu HTTP {reponse.status} pour {url}")
            octets = reponse.read()
    except urllib.error.HTTPError as err:
        corps = err.read(300).decode("utf-8", "replace").strip()
        raise RuntimeError(
            f"Erreur HTTP {err.code} lors du téléchargement de {url}\n{corps}"
        ) from err
    except urllib.error.URLError as err:
        raise RuntimeError(f"Impossible de joindre {url} : {err.reason}") from err
    contenu = octets.decode("utf-8-sig", "replace")
    if "BEGIN:VCALENDAR" not in contenu:
        raise RuntimeError(
            "La réponse ne contient pas de calendrier iCalendar (BEGIN:VCALENDAR). "
            "Vérifiez l'URL fournie (elle doit être le lien iCal/.ics d'HyperPlanning)."
        )
    return contenu


def vider_echappements(valeur: str) -> str:
    valeur = valeur.replace("\\n", "\n").replace("\\N", "\n")
    return valeur.replace("\\,", ",").replace("\\;", ";").replace("\\\\", "\\")


def parser_date(valeur: str, parametres: dict) -> datetime:
    valeur = valeur.strip()
    if parametres.get("VALUE") == "DATE" or re.fullmatch(r"\d{8}", valeur):
        return datetime.strptime(valeur, "%Y%m%d").replace(tzinfo=TZ_LOCAL)
    retour_utc = valeur.endswith("Z")
    naive = datetime.strptime(valeur.rstrip("Zz"), "%Y%m%dT%H%M%S")
    return naive.replace(tzinfo=timezone.utc if retour_utc else TZ_LOCAL)


def parser_vevent(lignes):
    evenement = {}
    for ligne in lignes:
        nom, _, reste = ligne.partition(":")
        nom = nom.upper()
        parametres = {}
        if ";" in nom:
            nom, brut_params = nom.split(";", 1)
            for morceau in brut_params.split(";"):
                cle, _, val = morceau.partition("=")
                if val:
                    parametres[cle.upper()] = vider_echappements(val)
        donnee = vider_echappements(reste)
        if nom == "DTSTART":
            evenement["debut"] = parser_date(reste, parametres)
        elif nom == "DTEND" and reste:
            evenement["fin"] = parser_date(reste, parametres)
        elif nom == "SUMMARY":
            evenement["matiere"] = donnee.strip()
        elif nom == "LOCATION":
            evenement["salle"] = donnee.strip()
        elif nom == "DESCRIPTION":
            evenement["description"] = donnee.strip()
        elif nom == "LAST-MODIFIED" and reste:
            evenement["maj"] = reste
        elif nom == "UID":
            evenement["uid"] = donnee
    return evenement if "debut" in evenement else None


def parser_ics(texte: str):
    lignes = []
    depliees = []
    for brut in texte.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if brut[:1] in (" ", "\t") and depliees:
            depliees[-1] += brut[1:]
        elif brut:
            depliees.append(brut)
    evenements = []
    dans_vevent = False
    bloc = []
    for ligne in depliees:
        haut = ligne.upper()
        if haut == "BEGIN:VEVENT":
            dans_vevent = True
            bloc = []
        elif haut == "END:VEVENT":
            dans_vevent = False
            event = parser_vevent(bloc)
            if event:
                if "fin" not in event:
                    event["fin"] = event["debut"] + timedelta(hours=1)
                evenements.append(event)
        elif dans_vevent:
            bloc.append(ligne)
    evenements.sort(key=lambda e: e["debut"])
    return evenements


def filtre_ricochet(evenement, du: datetime = None, au: datetime = None) -> bool:
    debut = evenement["debut"].astimezone(TZ_LOCAL)
    if du and debut < du:
        return False
    if au and debut > au.replace(hour=23, minute=59, second=59):
        return False
    return True


JOURS = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]

MOTS_TYPE = [
    ("Examen", ("examen", "rattrapage")),
    ("TP", ("tp", "groupes tp", "labo", "pratique")),
    ("Exercices", ("exercice",)),
    ("Séminaire", ("séminaire", "seminaire")),
    ("Conférence", ("conférence", "conference", "débat", "debat")),
    ("Réunion", ("réunion", "reunion")),
]
RE_CODE = re.compile(r"\b[A-Z]{1,4}-[A-Z0-9]{2,5}-\d{3}\b")


def analyser_cours(matiere: str = "", description: str = "") -> dict:
    """Extrait de l'intitulé et de la description iCalendar un dictionnaire structuré :
    code (ex. I-PHYS-022), intitulé, enseignants (si présents), type d'activité."""
    infos = {"code": "", "intitule": "", "enseignants": [], "type": "Cours"}
    for ligne in (description or "").split("\n"):
        ligne = ligne.strip()
        cle, sep, valeur = ligne.partition(":")
        if not sep:
            continue
        cle = cle.strip().lower()
        valeurs = [v.strip() for v in valeur.split(",") if v.strip()]
        if cle in ("aa", "intitulé", "intitule"):
            infos["intitule"] = valeur.strip()
        elif cle in ("code", "code cours"):
            infos["code"] = valeur.strip()
        elif cle in ("enseignant", "enseignants", "prof", "profs", "titulaire"):
            infos["enseignants"] += valeurs
    recherche = (infos["intitule"] + " " + (matiere or "")).strip()
    if recherche and not infos["code"]:
        trouve = RE_CODE.search(recherche)
        if trouve:
            infos["code"] = trouve.group(0)
    if recherche:
        tout = recherche.lower()
        for titre, mots in MOTS_TYPE:
            if any(mot in tout for mot in mots):
                infos["type"] = titre
                break
    return infos


def afficher_table(evenements):
    if not evenements:
        print("Aucun cours trouvé sur la période demandée.")
        return
    print()
    jour_courant = None
    largeur = 78
    for ev in evenements:
        debut = ev["debut"].astimezone(TZ_LOCAL)
        fin = ev["fin"].astimezone(TZ_LOCAL)
        if debut.date() != jour_courant:
            jour_courant = debut.date()
            print(f"── {JOURS[debut.weekday()]} {jour_courant.strftime('%d/%m/%Y')} ".ljust(largeur, "─"))
        print(
            f"  {debut:%H:%M} - {fin:%H:%M}  "
            f"{ev.get('matiere', '?')}"
            f"{('  @ ' + ev['salle']) if ev.get('salle') else ''}"
        )
        for ligne in (ev.get("description") or "").splitlines():
            ligne = ligne.strip()
            if ligne:
                print(f"       · {ligne}")
        print()


def vers_dictionnaire(ev) -> dict:
    infos = analyser_cours(ev.get("matiere", ""), ev.get("description", ""))
    return {
        "debut": ev["debut"].astimezone(TZ_LOCAL).isoformat(),
        "fin": ev["fin"].astimezone(TZ_LOCAL).isoformat(),
        "matiere": ev.get("matiere", ""),
        "salle": ev.get("salle", ""),
        "description": ev.get("description", ""),
        "uid": ev.get("uid", ""),
        "code": infos["code"],
        "intitule": infos["intitule"],
        "enseignants": infos["enseignants"],
        "type": infos["type"],
    }


def parse_args(argv):
    p = argparse.ArgumentParser(
        description="Récupère l'horaire depuis un planning HyperPlanning INVITE (type hplanning2026.umons.ac.be).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("Exemples :", 1)[-1],
    )
    p.add_argument(
        "url",
        nargs="?",
        default=URL_SALLES,
        help=f"URL iCal (.ics) du calendrier HyperPlanning. Défaut : export public de toutes les salles ({BASE}). "
             "Pour un emploi du temps précis, copier le lien iCal depuis l'interface invite. "
             "On peut aussi passer directement toute l'URL du calendrier.",
    )
    p.add_argument("--du", help="Date de début (AAAA-MM-JJ)", default=None)
    p.add_argument("--au", help="Date de fin (AAAA-MM-JJ)", default=None)
    p.add_argument("--semaine", type=int, help="Numéro de semaine ISO (ex. 48) à afficher", default=None)
    p.add_argument("--futur", action="store_true", help="Uniquement les cours à venir")
    p.add_argument("--filtre", "-f", help="Ne garder que les cours dont le texte contient ce motif (ex. 'BAB2 - Ir. civil')", default=None)
    p.add_argument("--format", choices=["table", "json", "csv"], default="table", help="Format de sortie (défaut : table)")
    p.add_argument("-o", "--sortie", help="Écrire la sortie dans un fichier")
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    texte = telecharger_ics(args.url)
    evenements = parser_ics(texte)

    du = datetime.strptime(args.du, "%Y-%m-%d").replace(tzinfo=TZ_LOCAL) if args.du else None
    au = datetime.strptime(args.au, "%Y-%m-%d").replace(tzinfo=TZ_LOCAL) if args.au else None
    if args.semaine:
        aujourdhui = datetime.now(TZ_LOCAL)
        lundi = aujourdhui - timedelta(days=aujourdhui.weekday())
        lundi += timedelta(weeks=args.semaine - aujourdhui.isocalendar().week)
        du = lundi
        au = lundi + timedelta(days=6)
    if args.futur and not du:
        du = datetime.now(TZ_LOCAL)

    evenements = [e for e in evenements if filtre_ricochet(e, du, au)]

    if args.filtre:
        motif = args.filtre.lower()
        evenements = [
            e for e in evenements
            if motif in (e.get("matiere", "") + " " + e.get("salle", "") + " " + e.get("description", "")).lower()
        ]

    if args.format == "table":
        tampon_out = io.StringIO()
        with contextlib.redirect_stdout(tampon_out):
            afficher_table(evenements)
        sortie = tampon_out.getvalue()
    elif args.format == "json":
        sortie = json.dumps([vers_dictionnaire(e) for e in evenements], indent=2, ensure_ascii=False)
    else:
        tampon_out = io.StringIO()
        w = csv.writer(tampon_out, delimiter=";")
        w.writerow(["debut", "fin", "matiere", "salle", "description", "uid"])
        for e in evenements:
            d = vers_dictionnaire(e)
            w.writerow([d["debut"], d["fin"], d["matiere"], d["salle"], d["description"].replace("\n", " | "), d["uid"]])
        sortie = tampon_out.getvalue()

    if args.sortie:
        Path(args.sortie).write_text(sortie, encoding="utf-8")
        print(f"Écrit dans {args.sortie} ({len(evenements)} cours).", file=sys.stderr)
    else:
        print(sortie)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BrokenPipeError:
        sys.exit(0)
