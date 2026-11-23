// Harnais DOM minimal pour exécuter le JS de page_horaires.html contre le serveur réel.
// Usage : node test_page.js [port]   (serveur déjà lancé sur ce port)
const fs = require("fs");

function elementStub(id) {
  const classes = new Set();
  const el = {
    id,
    style: {},
    value: "",
    textContent: "",
    title: "",
    className: "",
    classList: {
      add: (...c) => c.forEach(x => classes.add(x)),
      remove: (...c) => c.forEach(x => classes.delete(x)),
      contains: (c) => classes.has(c),
      toggle: (c, on) => { if (on === undefined) { classes.has(c) ? classes.delete(c) : classes.add(c); } else if (on) classes.add(c); else classes.delete(c); },
    },
    innerHTML: "",
    onclick: null,
    options: [],
    selectedOptions: [],
    addEventListener: () => {},
    appendChild: (c) => { el.options.push(c); return c; },
    querySelectorAll: () => [],
    querySelector: () => elementStub("interne"),
    getElementsByTagName: () => [],
  };
  return el;
}

const elements = {};
const gridCols = [];
function getCol(i) {
  if (!gridCols[i]) gridCols[i] = elementStub("col" + i);
  return gridCols[i];
}

const page = fs.readFileSync(__dirname + "/page_horaires.html", "utf8");
function valeurDefaut(id) {
  const sel = page.match(new RegExp('<select[^>]*id="' + id + '"[^>]*>([\\s\\S]*?)</select>'));
  if (!sel) return "";
  const tete = sel[1].match(/<option value="([^"]*)"[^>]*selected/);
  return tete ? tete[1] : "";
}

global.document = {
  body: elementStub("body"),
  getElementById: (id) => {
    if (id.startsWith("col")) return getCol(Number(id.slice(3)));
    if (!elements[id]) {
      elements[id] = elementStub(id);
      elements[id].value = valeurDefaut(id);
    }
    if (id === "grille") {
      elements[id].querySelectorAll = () => [0,1,2,3,4,5,6].map(getCol);
      elements[id].querySelector = () => elementStub("corps");
    }
    return elements[id];
  },
  createElement: (tag) => {
    const el = elementStub(tag);
    if (tag === "option") { el.text = ""; }
    return el;
  },
  createTextNode: (t) => ({ text: t }),
  addEventListener: () => {},
};

(async () => {
  const port = process.argv[2] || "8725";
  const js = page.match(/<script>([\s\S]*)<\/script>/)[1];
  let it = null;
  try {
    const AsyncFunction = Object.getPrototypeOf(async function(){}).constructor;
    const fn = new AsyncFunction("fetch", "URL", "location", "window", js + "\n;return { charger: typeof charger !== 'undefined' ? charger : null, appliquerPreference: typeof appliquerPreference !== 'undefined' ? appliquerPreference : null }");
    const fetchLocal = (u) => fetch(u);
    it = await fn(fetchLocal, URL, { toString: () => "http://127.0.0.1:" + port + "/" }, {});
    await new Promise(r => setTimeout(r, 4000));
  } catch (e) {
    console.log("ERREUR RUNTIME PAGE:", e && e.stack || e);
    process.exit(1);
  }
  const blocs = () => gridCols.reduce((n, c) => n + c.options.filter(o => o.className === "cours").length, 0);
  console.log("== message:", JSON.stringify(elements["message"] ? elements["message"].textContent : "(jamais touché)"));
  console.log("== ressource options:", elements["ressource"].options.length);
  console.log("== groupe options:", elements["groupe"].options.length);
  console.log("== blocs sans sélection (attendu 0 avec l'écran de choix):", blocs());
  console.log("== items légende:", (elements["legende"] ? elements["legende"].options : []).filter(c => c.className === "legende-item").length);
  if (it && it.charger) {
    it.majListeRessources && it.majListeRessources();
    const bab2 = elements["ressource"].options.find(o => o.value === ".BAB2 - Ir. civil");
    console.log("== .BAB2 - Ir. civil present dans la liste:", Boolean(bab2));
    if (it.appliquerPreference)
      await it.appliquerPreference({ categorie: "formations", ressource: ".BAB2 - Ir. civil", groupe: "BAB2 - Ir Civil - B" });
    console.log("== sélecteurs posés: ress. =", elements["ressource"].value, "| groupe =", elements["groupe"].value);
    it.charger();
    await new Promise(r => setTimeout(r, 2500));
    console.log("== titreSemaine:", elements["titreSemaine"].textContent);
    console.log("== badgeSource:", elements["badgeSource"].textContent, "display:", elements["badgeSource"].style.display);
    console.log("== après sélection : blocs =", blocs(), "| message:", JSON.stringify(elements["message"] ? elements["message"].textContent : "(jamais touché)"));
  }
})();
