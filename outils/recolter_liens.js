#!/usr/bin/env node
/**
 * Récolte des liens iCal annuels (liens_annuels.json) pour Horairee.
 *
 * Lance le client officiel UMONS (hplanning2026.umons.ac.be/invite?fd=1) dans
 * jsdom, sans navigateur : le client établit lui-même la session chiffrée
 * « donneesSecurisee ». On récupère alors, par le système de modules du client :
 *   - la liste des formations publiques  (FonctionRenvoyerListeDeRessource) ;
 *   - le lien iCal permanent annuel de chacune (FonctionParametresICAL,
 *     réponse ParametreExportICal = Telechargements/ical/Edt_*.ics?…&icalsecurise=…).
 *
 * Utilisation (Node >= 18, récolteur de maintenance — pas inclus dans l'image) :
 *     npm i jsdom @napi-rs/canvas
 *     node outils/recolter_liens.js
 * Options env : SORTIE (défaut outils/liens_annuels.json), DELAI ms entre
 * requêtes (défaut 350), MAX (limite pour un test), BASE (autre HyperPlanning).
 *
 * Réprise : les entrées déjà présentes dans SORTIE sont sautées.
 */

const path = require("path");
process.env.NODE_TLS_REJECT_UNAUTHORIZED = "0";

const { JSDOM, VirtualConsole } = require("jsdom");
const fs = require("fs");

const BASE = process.env.BASE || "https://hplanning2026.umons.ac.be/";
const UA = "Mozilla/5.0 (X11; Linux x86_64; rv:132.0) Gecko/20100101 Firefox/132.0";
const DELAI = Number(process.env.DELAI || 350);
const MAX = Number(process.env.MAX || 0);
const SORTIE = path.resolve(process.env.SORTIE || path.join(__dirname, "..", "liens_annuels.json"));

const vc = new VirtualConsole();
vc.on("jsdomError", () => {});
vc.on("error", () => {});
vc.on("warn", () => {});

(async () => {
  // Le HTML contient un identifiant de session expirable : toujours frais.
  const page = await fetch(BASE + "invite?fd=1", { headers: { "User-Agent": UA } });
  const html = (await page.text()).replace('<base target="_blank">', "");

  const dom = new JSDOM(html, {
    url: BASE + "invite?fd=1",
    runScripts: "dangerously",
    resources: { userAgent: UA },
    pretendToBeVisual: true,
    virtualConsole: vc,
  });
  const w = dom.window;
  w.fetch = (u, o) => fetch(new URL(u, w.location.href).href, o);
  w.crypto = w.crypto || require("crypto").webcrypto;
  w.matchMedia = w.matchMedia || (() => ({ matches: false, addListener: () => {}, removeListener: () => {}, addEventListener: () => {} }));

  // Le client rend via canvas : @napi-rs/canvas si installé, sinon stub vide
  // (l'export iCal n'en dépend pas).
  if (!w.HTMLCanvasElement.prototype.getContext || w.HTMLCanvasElement.prototype.getContext.toString().includes("[native code]")) {
    try { require("@napi-rs/canvas"); } catch (e) { w.HTMLCanvasElement.prototype.getContext = () => null; }
  }

  setTimeout(() => {
    if (!w.GEtatUtilisateur) { console.error("PAS DE SESSION — le client n'a pas démarré."); process.exit(2); }
    const ReqListe = w.require("ObjetRequeteRenvoyerListeDeRessource.js");
    const ReqICal = w.require("ObjetRequeteParametresICAL.js");

    let resultats = {};
    try { resultats = JSON.parse(fs.readFileSync(SORTIE, "utf8")); } catch (e) {}

    new ReqListe.ObjetRequeteRenvoyerListeDeRessource(null, (nom, liste) => {
      const els = [];
      liste.parcourir((el) => els.push(el));
      console.log("Formations publiques :", els.length);

      const cibles = MAX ? els.slice(0, MAX) : els;
      let k = 0, ok = 0, ko = 0;
      const suivant = () => {
        if (k >= cibles.length) {
          console.log(`TERMINE. ok=${ok} ko=${ko} -> ${SORTIE}`);
          fs.writeFileSync(SORTIE, JSON.stringify(resultats, null, 1));
          process.exit(0);
        }
        const el = cibles[k++];
        const libelle = (el.getLibelle() || "").trim();
        if (!libelle || Object.values(resultats).some((v) => v.libelle === libelle)) { suivant(); return; }
        new ReqICal.ObjetRequeteParametresICAL(null, (rep) => {
          if (rep && rep.ParametreExportICal) {
            ok++;
            resultats[libelle] = { lien: rep.ParametreExportICal, type: "formations", genere: new Date().toISOString().slice(0, 10) };
          } else { ko++; console.log("sans lien:", libelle, rep && (rep.messageExportICalImpossible || "")); }
          if ((ok + ko) % 25 === 0) { try { fs.writeFileSync(SORTIE, JSON.stringify(resultats)); } catch (e) {} }
          if ((ok + ko) % 50 === 0) console.log("progres", ok + ko, "/", cibles.length);
          setTimeout(suivant, DELAI);
        }, () => {
          ko++;
          setTimeout(suivant, DELAI);
        }).lancerRequete(el);
      };
      suivant();
    }, (e) => { console.error("ECHEC LISTE", JSON.stringify(e)); process.exit(3); })
      .lancerRequete({ genreRessource: 1, genreRecherche: 1 }) // TypeHttpRessourceInternet.RessourceInternetDiplomes, EGenreRecherche.Nom
      .catch((err) => { console.error("REJET", err); process.exit(4); });
  }, 25000);
})().catch((e) => { console.error("FATAL", (e && e.stack) || e); process.exit(1); });
