// Runs the real backend/static/live.js against a fake page and a scripted server,
// to check its logic without a browser: badge, in-place refresh, toasts, not
// clobbering a field being edited, and stopping when signed out.
//
// Run from the repo root:  node scripts/test_live_js.mjs
import { readFileSync } from "node:fs";
import vm from "node:vm";

const code = readFileSync(new URL("../backend/static/live.js", import.meta.url), "utf8");
let passed = 0;
let failed = 0;

function check(label, condition, detail = "") {
  if (condition) { passed += 1; console.log(`  PASS  ${label}`); }
  else { failed += 1; console.log(`  FAIL  ${label} ${detail}`); }
}

const flush = () => new Promise((resolve) => setTimeout(resolve, 0));

function page({ refresh = true } = {}) {
  const toasts = [];
  const badge = { textContent: "" };
  const main = {
    innerHTML: "OLD",
    editing: false,
    hasAttribute: (name) => refresh && name === "data-live-refresh",
    contains: () => main.editing,
  };
  const toastBox = { appendChild: (note) => toasts.push(note.textContent) };
  const server = { states: [], liveCalls: 0, pageCalls: 0 };
  let tick = null;

  const document = {
    hidden: false,
    get activeElement() { return main.editing ? { tagName: "INPUT" } : null; },
    getElementById: (id) => ({ main, "cart-count": badge, "live-toasts": toastBox })[id] ?? null,
    createElement: () => ({ style: {}, className: "", textContent: "", remove() {} }),
    addEventListener: () => {},
  };

  async function fetch(url) {
    if (url === "/api/live") {
      const state = server.states[Math.min(server.liveCalls, server.states.length - 1)];
      server.liveCalls += 1;
      if (state === 401) return { ok: false, status: 401, json: async () => ({}) };
      return { ok: true, status: 200, json: async () => state };
    }
    server.pageCalls += 1;
    return { ok: true, status: 200, text: async () => `<main id="main">NEW${server.pageCalls}</main>` };
  }

  class DOMParser {
    parseFromString(html) {
      const inner = html.replace(/^<main[^>]*>|<\/main>$/g, "");
      return { getElementById: () => ({ innerHTML: inner }) };
    }
  }

  const context = {
    document, fetch, DOMParser, console,
    location: { href: "/cart" },
    setInterval: (fn) => { tick = fn; },
    setTimeout: () => {},  // toast fade-outs: irrelevant here
  };
  return {
    toasts, badge, main, server,
    start(states) { server.states = states; vm.createContext(context); vm.runInContext(code, context); return flush(); },
    async poll() { await tick(); await flush(); },
  };
}

const A = { cart_count: 1, fingerprint: "aaa", orders: { 7: "created" } };
const B = { cart_count: 2, fingerprint: "bbb", orders: { 7: "created" } };
const C = { cart_count: 2, fingerprint: "ccc", orders: { 7: "captured" } };
const D = { cart_count: 2, fingerprint: "ddd", orders: { 7: "captured", 8: "created" } };

console.log("\n1. First load");
let p = page();
await p.start([A, A, B, C, D]);
check("badge shows the cart count", p.badge.textContent === "(1)", p.badge.textContent);
check("no toast on the first poll", p.toasts.length === 0, JSON.stringify(p.toasts));
check("no re-render on the first poll", p.main.innerHTML === "OLD");

console.log("\n2. Nothing changed");
await p.poll();
check("same fingerprint: no re-render", p.server.pageCalls === 0);

console.log("\n3. Item added on WhatsApp");
await p.poll();
check("page re-rendered in place", p.main.innerHTML === "NEW1", p.main.innerHTML);
check("badge follows", p.badge.textContent === "(2)");
check("toast says the cart changed", p.toasts.includes("Cart updated — 2 items"), JSON.stringify(p.toasts));

console.log("\n4. Paid on the phone");
await p.poll();
check("toast says the order was paid", p.toasts.includes("Order #7 paid"), JSON.stringify(p.toasts));

console.log("\n5. New order appears");
await p.poll();
check("toast announces the new order", p.toasts.includes("Order #8 created"), JSON.stringify(p.toasts));

console.log("\n6. Customer is typing in a quantity box");
p = page();
await p.start([A, B, B]);
p.main.editing = true;
await p.poll();
check("no re-render while editing", p.main.innerHTML === "OLD" && p.toasts.length === 0);
p.main.editing = false;
await p.poll();
check("catches up once they stop", p.main.innerHTML === "NEW1" && p.toasts.length === 1,
      `${p.main.innerHTML} ${JSON.stringify(p.toasts)}`);

console.log("\n7. Pages without data-live-refresh (home, product)");
p = page({ refresh: false });
await p.start([A, B]);
await p.poll();
check("badge still updates", p.badge.textContent === "(2)");
check("toast still shown", p.toasts.length === 1);
check("page itself not re-fetched", p.server.pageCalls === 0 && p.main.innerHTML === "OLD");

console.log("\n8. Signed out in another tab");
p = page();
await p.start([A, 401, A]);
await p.poll();
const callsAfterStop = p.server.liveCalls;
await p.poll();
check("stops polling after a 401", p.server.liveCalls === callsAfterStop, `${p.server.liveCalls}`);

console.log(`\n${passed} passed, ${failed} failed`);
process.exit(failed ? 1 : 0);
