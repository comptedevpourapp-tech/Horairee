// Récolte des groupes de TD (TP) par diplôme et de leur flux iCal annuel.
//
// Principe : piloter le client officiel HyperPlanning (invite?fd=1) dans jsdom.
//  1. lister les diplômes (FonctionRenvoyerListeDeRessource, genre 1)
//  2. pour chaque formation du catalogue (liens_annuels.json) : fixer la
//     navigation primaire puis demander ses TD/options
//     (FonctionListeDeTDEtOptionDuDiplome — la réponse appartient au diplôme
//     porté par la navigation de session)
//  3. pour chaque TD (genre 4) : FonctionParametresICAL → lien iCal annuel
//  4. écrire LIENS_GROUPES : { "<formation>": { "<libellé TD>": "<lien>" } }
//
// Usage :
//   npm i jsdom && node outils/recolter_groupes.js
// Variables :
//   BASE (hplanning2026.umons.ac.be), DELAI (300 ms), SORTIE (liens_groupes.json),
//   LIENS_FORMATIONS (liens_annuels.json), MAX (limite d'essais, défaut 0 = tout)
// La sortie est réécrite par palliers : le script peut être relancé, les
// formations déjà récoltées sont sautées (reprise).

const { JSDOM, VirtualConsole } = require("jsdom");
process.env.NODE_TLS_REJECT_UNAUTHORIZED = "0";
const fs = require("fs");
const path = require("path");

const BASE = process.env.BASE || "https://hplanning2026.umons.ac.be/";
const UA = "Mozilla/5.0 (X11; Linux x86_64; rv:132.0) Gecko/20100101 Firefox/132.0";
const DELAI = Number(process.env.DELAI || 300);
const RACINE = path.join(__dirname, "..");
const SORTIE = process.env.SORTIE || path.join(RACINE, "liens_groupes.json");
const LIENS_FORMATIONS = process.env.LIENS_FORMATIONS || path.join(RACINE, "liens_annuels.json");
const MAX = Number(process.env.MAX || 0);
// partitionnement : K sur N (pour lancer plusieurs sessions en parallèle)
const K = Number(process.env.K || 0), N = Number(process.env.N || 1);

const pause = (ms) => new Promise((r) => setTimeout(r, ms));

(async () => {
  const formations = Object.keys(JSON.parse(fs.readFileSync(LIENS_FORMATIONS, "utf8")));
  console.log(`Catalogue : ${formations.length} formations.`);
  let resultats = {};
  try { resultats = JSON.parse(fs.readFileSync(SORTIE, "utf8")); } catch (e) {}

  const pg = await fetch(BASE + "invite?fd=1", { headers: { "User-Agent": UA } });
  let html = await pg.text();
  html = html.replace('<base target="_blank">', "");
  const vc = new VirtualConsole();
  vc.on("jsdomError", () => {});
  vc.on("error", () => {});
  vc.on("warn", () => {});
  const dom = new JSDOM(html, { url: BASE + "invite?fd=1", runScripts: "dangerously",
    resources: { userAgent: UA }, pretendToBeVisual: true, virtualConsole: vc });
  const w = dom.window;
  w.fetch = (u, o) => fetch(new URL(u, w.location.href).href, o);
  w.crypto = w.crypto || require("crypto").webcrypto;
  w.matchMedia = w.matchMedia || (() => ({ matches: false, addListener: () => {}, removeListener: () => {}, addEventListener: () => {} }));

  setTimeout(async () => {
    if (!w.GEtatUtilisateur) { console.log("PAS DE SESSION"); process.exit(2); }
    const ReqListe = w.require("ObjetRequeteRenvoyerListeDeRessource.js");
    const ReqTD = w.require("objetrequetelistedetdetoptiondudiplome.js");
    const ReqICal = w.require("ObjetRequeteParametresICAL.js");
    const ObjetElementRecherche = w.require("ObjetElementRecherche.js").ObjetElementRecherche;
    const GEtat = w.GEtatUtilisateur;

    // recensement des diplômes de la session
    const diplomes = {};
    new ReqListe.ObjetRequeteRenvoyerListeDeRessource(null, (n, liste) => {
      liste.parcourir((el) => { diplomes[el.getLibelle() || ""] = el; });
      console.log("Diplômes de la session :", Object.keys(diplomes).length);
      (async () => {
        const restantes = formations.filter((f) => !resultats[f] && (((N > 1 ? formations.indexOf(f) : 0) + K) % N === 0));
        console.log("Formations à traiter :", restantes.length);
        let ens = 0;
        for (const formation of restantes) {
          const el = diplomes[formation];
          if (!el) { console.log("absente de la session :", formation); resultats[formation] = {}; continue; }
          GEtat.setOngletRechercheRessource(0, new ObjetElementRecherche(el.getLibelle(), el.getNumero(), el.getGenre(), 1));
          await pause(DELAI);
          const tds = await new Promise((res) => {
            const minute = setTimeout(() => res([]), 45000);
            try {
              new ReqTD.ObjetRequeteListeDeTDEtOptionDuDiplome(null, (liste2) => {
                clearTimeout(minute);
                const els = []; liste2.parcourir((x) => els.push(x)); res(els);
              }, () => { clearTimeout(minute); res([]); }).lancerRequete(1, true).catch(() => { clearTimeout(minute); res([]); });
            } catch (e) { clearTimeout(minute); res([]); }
          });
          const liens = {};
          for (const td of tds) {
            if (Number(td.getGenre()) !== 4 || !td.getLibelle()) continue;
            const libelle = String(td.getLibelle()).trim();
            await pause(DELAI);
            const lien = await new Promise((res) => {
              const minute = setTimeout(() => res(null), 45000);
              try {
                new ReqICal.ObjetRequeteParametresICAL(null, (rep) => {
                  clearTimeout(minute);
                  res(rep && rep.ParametreExportICal ? rep.ParametreExportICal : null);
                }, () => { clearTimeout(minute); res(null); }).lancerRequete(td);
              } catch (e) { clearTimeout(minute); res(null); }
            });
            if (lien) { liens[libelle] = lien; ens++; }
          }
          resultats[formation] = liens;
          console.log(`${formation} : ${Object.keys(liens).length} groupes`);
          try { fs.writeFileSync(SORTIE, JSON.stringify(resultats)); } catch (e) {}
        }
        fs.writeFileSync(SORTIE, JSON.stringify(resultats, null, 1));
        console.log(`TERMINE : ${ens} liens de groupes écrits dans ${SORTIE}`);
        process.exit(0);
      })().catch((e) => { console.log("FATAL2", String(e).slice(0, 300)); process.exit(1); });
    }, (e) => { console.log("échec liste:", JSON.stringify(e).slice(0, 200)); process.exit(5); })
      .lancerRequete({ genreRessource: 1, genreRecherche: 1 });
  }, 25000);
})().catch((e) => { console.log("FATAL", String(e).slice(0, 300)); process.exit(1); });
