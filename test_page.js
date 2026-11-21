// Harnais DOM minimal pour exécuter le JS de page_horaires.html contre le serveur réel.
// Usage : node test_page.js [port]   (serveur déjà lancé sur ce port)
const fs = require("fs");

function elementStub(id) {
  const el = {
    id,
    style: {},
    value: "",
    textContent: "",
    title: "",
    className: "",
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
  try {
    // exécution asynchrone : on attend le cycle de promesses
    const AsyncFunction = Object.getPrototypeOf(async function(){}).constructor;
    const fn = new AsyncFunction("fetch", "URL", "location", "window", js);
    const fetchLocal = (u) => fetch(u);
    await fn(fetchLocal, URL, { toString: () => "http://127.0.0.1:" + port + "/" }, {});
    await new Promise(r => setTimeout(r, 4000));
  } catch (e) {
    console.log("ERREUR RUNTIME PAGE:", e && e.stack || e);
    process.exit(1);
  }
  console.log("== titreSemaine:", elements["titreSemaine"].textContent);
  console.log("== message:", JSON.stringify(elements["message"].textContent));
  console.log("== badgeSource:", elements["badgeSource"].textContent, "display:", elements["badgeSource"].style.display);
  console.log("== ressource options:", elements["ressource"].options.length);
  console.log("== groupe options:", elements["groupe"].options.length);
  console.log("== blocs cours ajoutés:", gridCols.reduce((n, c) => n + c.options.filter(o => o.className === "cours").length, 0));
  console.log("== items légende:", elements["legende"].options.filter(c => c.className === "legende-item").length);
})();
