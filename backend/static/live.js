// Live sync: keeps signed-in pages current with changes made on WhatsApp.
//
// Every POLL_MS the page asks /api/live for a small fingerprint of the cart and
// recent orders. The cart badge always follows it. Pages that mark their <main>
// with data-live-refresh re-render in place when the fingerprint changes, so
// adding to the cart in chat, or paying on the phone, shows up here within a
// couple of seconds without touching the laptop.
//
// Polling rather than WebSockets on purpose: it survives ngrok, server restarts
// and flaky demo Wi-Fi with no reconnect logic at all.
(() => {
  "use strict";

  const POLL_MS = 2000;
  // ngrok's free plan shows a warning page to browsers; this header skips it
  const HEADERS = { "ngrok-skip-browser-warning": "1" };

  const main = document.getElementById("main");
  const badge = document.getElementById("cart-count");
  const toastBox = document.getElementById("live-toasts");
  const refreshesInPlace = Boolean(main && main.hasAttribute("data-live-refresh"));

  let last = null;       // previous state from /api/live
  let busy = false;      // one request at a time
  let stopped = false;   // signed out elsewhere

  function setBadge(count) {
    if (badge) badge.textContent = count > 0 ? `(${count})` : "";
  }

  function toast(text) {
    if (!toastBox) return;
    const note = document.createElement("div");
    note.className =
      "bg-stone-900 text-white text-sm px-4 py-2.5 rounded-md shadow-lg transition-opacity duration-500";
    note.textContent = text;
    toastBox.appendChild(note);
    setTimeout(() => { note.style.opacity = "0"; }, 3500);
    setTimeout(() => note.remove(), 4100);
  }

  // What changed, in words, for the toast
  function describe(previous, current) {
    const notes = [];
    for (const [id, status] of Object.entries(current.orders)) {
      const before = previous.orders[id];
      if (before === undefined) notes.push(`Order #${id} created`);
      else if (before !== status && status === "captured") notes.push(`Order #${id} paid`);
      else if (before !== status && status === "failed") notes.push(`Order #${id} cancelled`);
    }
    if (current.cart_count !== previous.cart_count) {
      const n = current.cart_count;
      notes.push(n === 0 ? "Cart is now empty" : `Cart updated — ${n} item${n === 1 ? "" : "s"}`);
    }
    return notes;
  }

  // Someone typing in a quantity box or choosing a size should not have the
  // page replaced under them; the next poll will catch up instead.
  function userIsEditing() {
    const el = document.activeElement;
    return Boolean(main && el && main.contains(el) && /^(INPUT|SELECT|TEXTAREA)$/.test(el.tagName));
  }

  async function refreshMain() {
    const response = await fetch(location.href, { headers: HEADERS, cache: "no-store" });
    if (!response.ok) return false;
    const page = new DOMParser().parseFromString(await response.text(), "text/html");
    const fresh = page.getElementById("main");
    if (!fresh) return false;
    main.innerHTML = fresh.innerHTML;
    return true;
  }

  async function poll() {
    if (busy || stopped || document.hidden) return;
    busy = true;
    try {
      const response = await fetch("/api/live", { headers: HEADERS, cache: "no-store" });
      if (response.status === 401) { stopped = true; return; }
      if (!response.ok) return;
      const state = await response.json();
      setBadge(state.cart_count);

      if (last && state.fingerprint !== last.fingerprint) {
        if (refreshesInPlace) {
          // Keep the old fingerprint until the page really re-rendered, so a
          // skipped or failed refresh is retried on the next poll
          if (userIsEditing() || !(await refreshMain())) return;
        }
        describe(last, state).forEach(toast);
      }
      last = state;
    } catch (_) {
      // Network blip: the next poll tries again
    } finally {
      busy = false;
    }
  }

  poll();
  setInterval(poll, POLL_MS);
  // Catch up at once when the tab comes back into view
  document.addEventListener("visibilitychange", () => { if (!document.hidden) poll(); });
})();
