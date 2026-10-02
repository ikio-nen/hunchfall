// Hunchfall content script — page scanner for Polymarket.
// 1. Detects which market page the user is viewing (slug from URL).
// 2. Best-effort scrape of visible state (prices, book, trades).
// 3. Sends the snapshot to the service worker -> backend /extension/scan.
// 4. Renders the returned paper prediction as an on-page panel.
//
// The backend re-validates everything through Polymarket's official
// Gamma/CLOB/Data APIs; the scrape is only a hint so the backend knows
// which market to look at. Nothing here clicks, fills, or submits anything.

(() => {
  const PANEL_ID = "hunchfall-panel";
  const MARKETPLACE = "polymarket";

  function getMarketRef() {
    const m = location.pathname.match(/^\/(event|market)\/([^/?#]+)/);
    if (!m) return null;
    return { kind: m[1], slug: decodeURIComponent(m[2]) };
  }

  // --- best-effort visible-state scrape ------------------------------------
  // Polymarket is a SPA and its DOM changes; keep this defensive and simple.
  function scrapePageState() {
    const state = { yesPrice: null, noPrice: null, title: document.title || null };

    try {
      // Prices usually render like "62¢". Collect candidates, take the most
      // plausible YES price (first sane cent value near typical buy buttons).
      const cents = [];
      const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
      let node;
      while ((node = walker.nextNode())) {
        const t = node.nodeValue.trim();
        const mm = t.match(/^(\d{1,3})¢$/);
        if (mm) {
          const v = parseInt(mm[1], 10);
          if (v > 0 && v < 100) cents.push(v / 100);
        }
      }
      if (cents.length) {
        // most frequent cent value is usually the displayed YES price
        const freq = {};
        cents.forEach((c) => { freq[c] = (freq[c] || 0) + 1; });
        const top = Object.entries(freq).sort((a, b) => b[1] - a[1])[0];
        state.yesPrice = parseFloat(top[0]);
        state.noPrice = +(1 - state.yesPrice).toFixed(2);
      }
    } catch (_e) { /* scrape is best-effort; backend re-validates */ }

    try {
      const h1 = document.querySelector("h1");
      if (h1 && h1.innerText.trim()) state.title = h1.innerText.trim().slice(0, 200);
    } catch (_e) {}

    return state;
  }

  // --- panel ----------------------------------------------------------------
  function removePanel() {
    const el = document.getElementById(PANEL_ID);
    if (el) el.remove();
  }

  function el(tag, cls, text) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }

  function renderPanel(payload) {
    removePanel();
    const panel = el("div");
    panel.id = PANEL_ID;

    const head = el("div", "hf-head");
    head.appendChild(el("span", "hf-brand", "HUNCHFALL"));
    head.appendChild(el("span", "hf-tag", "paper prediction · no trade placed"));
    const close = el("button", "hf-close", "×");
    close.onclick = removePanel;
    head.appendChild(close);
    panel.appendChild(head);

    const body = el("div", "hf-body");

    if (payload.error) {
      body.appendChild(el("div", "hf-error", "Couldn't scan this market."));
      body.appendChild(el("div", "hf-hint", payload.error));
    } else if (payload.loading) {
      body.appendChild(el("div", "hf-loading", "Scanning market…"));
    } else {
      const h = payload.hunch || {};
      if (payload.market && payload.market.title) {
        body.appendChild(el("div", "hf-title", payload.market.title));
      }
      const row = el("div", "hf-row");
      const side = el("span", "hf-side " + String(h.side || "").toLowerCase(), h.side || "—");
      row.appendChild(side);
      row.appendChild(el("span", "hf-prob", h.p_true != null ? `P(true) ${(h.p_true * 100).toFixed(0)}%` : ""));
      body.appendChild(row);

      const grid = el("div", "hf-grid");
      grid.appendChild(el("div", "hf-k", "Live price"));
      grid.appendChild(el("div", "hf-v mono", h.price != null ? `${(h.price * 100).toFixed(1)}¢` : "—"));
      grid.appendChild(el("div", "hf-k", "Edge"));
      grid.appendChild(el("div", "hf-v mono", h.edge != null ? `${(h.edge * 100 >= 0 ? "+" : "") + (h.edge * 100).toFixed(1)}%` : "—"));
      if (h.signal_id) {
        grid.appendChild(el("div", "hf-k", "Signal"));
        grid.appendChild(el("div", "hf-v mono hf-dim", String(h.signal_id).slice(0, 8)));
      }
      body.appendChild(grid);

      if (h.reason) body.appendChild(el("div", "hf-reason", h.reason));

      const actions = el("div", "hf-actions");
      const dash = el("a", "hf-btn", "Open dashboard");
      dash.href = "http://localhost:5173";
      dash.target = "_blank";
      actions.appendChild(dash);
      body.appendChild(actions);
    }

    panel.appendChild(body);
    document.documentElement.appendChild(panel);
  }

  // --- scan ------------------------------------------------------------------
  let scanning = false;
  async function scan() {
    const ref = getMarketRef();
    if (!ref || scanning) return;
    scanning = true;
    renderPanel({ loading: true });
    try {
      const res = await chrome.runtime.sendMessage({
        type: "HUNCHFALL_SCAN",
        marketplace: MARKETPLACE,
        kind: ref.kind,
        slug: ref.slug,
        url: location.href,
        pageState: scrapePageState(),
      });
      renderPanel(res && res.ok ? res : { error: (res && res.error) || "scan failed" });
    } catch (e) {
      renderPanel({ error: String((e && e.message) || e) });
    } finally {
      scanning = false;
    }
  }

  // Polymarket is an SPA: re-scan when the URL changes without reload.
  let lastUrl = location.href;
  new MutationObserver(() => {
    if (location.href !== lastUrl) {
      lastUrl = location.href;
      removePanel();
      scan();
    }
  }).observe(document.documentElement, { subtree: true, childList: true });

  scan();
})();
