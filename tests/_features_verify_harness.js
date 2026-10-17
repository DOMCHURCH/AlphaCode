// Runs src/report/static/features.js against a tiny fake DOM and prints what the
// bulk-check panel shows. Usage: node _features_verify_harness.js features.js '<json response>'
const fs = require("fs");
const vm = require("vm");

const source = fs.readFileSync(process.argv[2], "utf8");
const payload = JSON.parse(process.argv[3]);

function node(tag) {
  return {
    tag, children: [], textContent: "", className: "", hidden: false, value: "", listeners: {},
    appendChild(c) { this.children.push(c); return c; },
    addEventListener(type, fn) { this.listeners[type] = fn; },
    setAttribute() {}, getAttribute() { return null; },
  };
}

const els = {};
for (const id of ["verify-form", "verify-q", "verify-note", "verify-out"]) els[id] = node(id);
els["verify-q"].value = "AAA BBB CCC NOPE";

const documentListeners = {};
const document = {
  getElementById: (id) => els[id] || null,
  createElement: node,
  createTextNode: (t) => ({ nodeType: 3, textContent: t }),
  querySelectorAll: () => [],
  addEventListener: (type, fn) => { documentListeners[type] = fn; },
};
const context = {
  document,
  localStorage: { getItem: () => "" },
  fetch: () => Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(payload) }),
  console,
};
context.window = context;
vm.createContext(context);
vm.runInContext(source, context);

documentListeners["DOMContentLoaded"]();
els["verify-form"].listeners["submit"]({ preventDefault() {} });

function rowsOf(n, out) {
  if (n.tag === "tr" && n.children.length && n.children[0].tag === "td") {
    out.push(n.children.map((td) => td.textContent));
  }
  (n.children || []).forEach((c) => rowsOf(c, out));
  return out;
}

setTimeout(() => {
  const rows = [];
  els["verify-out"].children.forEach((c) => rowsOf(c, rows));
  console.log(JSON.stringify({ note: els["verify-note"].textContent, rows }));
}, 100);
