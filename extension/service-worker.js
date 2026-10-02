// Hunchfall service worker (Manifest V3).
// The content script scans the marketplace page; this worker forwards the
// snapshot to the Hunchfall backend, which runs the Jev decision layer and
// returns a paper prediction. The worker never places trades and never
// touches the marketplace's own buttons or session.

const DEFAULT_BACKEND = "http://localhost:8000";

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (!msg || msg.type !== "HUNCHFALL_SCAN") return false;

  (async () => {
    const { backendUrl } = await chrome.storage.sync.get({
      backendUrl: DEFAULT_BACKEND,
    });
    const base = String(backendUrl || DEFAULT_BACKEND).replace(/\/+$/, "");

    const res = await fetch(`${base}/extension/scan`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        marketplace: msg.marketplace, // e.g. "polymarket"
        kind: msg.kind,               // "event" | "market"
        slug: msg.slug,
        url: msg.url,
        page_state: msg.pageState,    // best-effort scrape; backend re-validates via official APIs
        scanned_at: new Date().toISOString(),
      }),
    });

    if (!res.ok) throw new Error(`backend returned ${res.status}`);
    const data = await res.json();
    sendResponse({ ok: true, ...data });
  })().catch((err) => {
    sendResponse({ ok: false, error: String((err && err.message) || err) });
  });

  return true; // async sendResponse
});
