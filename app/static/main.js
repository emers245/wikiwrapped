/* Wikipedia Wrapped frontend bootstrap: fetches /dashboard once, owns the
   shared topic palette, the hover popup, and the cross-component
   highlight bus. Muted palette on an off-white theme. */

window.WW = (function () {
  const PALETTE = [
    "#7295b8", "#e0a458", "#7fb586", "#c66e6e", "#9d85b8",
    "#a98b78", "#d495b6", "#8f9779", "#b8b16d", "#6fb3b8",
    "#c99a7a", "#94a7c4", "#b0c98f", "#c98fa6", "#87a8a4",
  ];
  const UNCLASSIFIED_COLOR = "#b3afa7";

  const state = { data: null, topicColors: {}, mode: "umap" };

  function esc(text) {
    const div = document.createElement("div");
    div.textContent = text == null ? "" : String(text);
    return div.innerHTML;
  }

  function colorFor(topic) {
    return state.topicColors[topic || "Unclassified"] || UNCLASSIFIED_COLOR;
  }

  function hexToRgba(hex, alpha) {
    const n = parseInt(hex.slice(1), 16);
    return `rgba(${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255}, ${alpha})`;
  }

  function pageUrl(title) {
    return "https://en.wikipedia.org/wiki/" +
      encodeURIComponent(title.replace(/ /g, "_"));
  }

  /* -- popup -------------------------------------------------------------- */
  const popup = document.getElementById("popup");
  let hideTimer = null;

  function showPopup(page, clientX, clientY) {
    clearTimeout(hideTimer);
    const views = page.pageviews == null
      ? "n/a" : Number(page.pageviews).toLocaleString();
    popup.innerHTML = `
      <h3><a href="${pageUrl(page.title)}" target="_blank"
             rel="noopener">${esc(page.title)}</a></h3>
      <table>
        <tr><td>Topic</td><td>${esc(page.macro_topic || "Unclassified")}</td></tr>
        <tr><td>Your visits</td><td>${page.visit_count}</td></tr>
        <tr><td>First seen</td><td>${esc((page.first_seen || "").slice(0, 10))}</td></tr>
        <tr><td>Views by others</td><td>${views}</td></tr>
        <tr><td>In / out degree</td><td>${page.in_degree} / ${page.out_degree}</td></tr>
        <tr><td>PageRank</td><td>${page.pagerank}</td></tr>
      </table>`;
    popup.classList.remove("hidden");
    const rect = popup.getBoundingClientRect();
    let x = clientX + 14;
    let y = clientY + 10;
    if (x + rect.width > window.innerWidth - 8) x = clientX - rect.width - 14;
    if (y + rect.height > window.innerHeight - 8) y = clientY - rect.height - 10;
    popup.style.left = x + "px";
    popup.style.top = y + "px";
  }

  function scheduleHidePopup() {
    clearTimeout(hideTimer);
    hideTimer = setTimeout(() => popup.classList.add("hidden"), 250);
  }
  popup.addEventListener("mouseenter", () => clearTimeout(hideTimer));
  popup.addEventListener("mouseleave", () => {
    popup.classList.add("hidden");
    highlight(null);
  });

  /* -- highlight bus ------------------------------------------------------ */
  function highlight(page) {
    if (window.WWmap) WWmap.highlight(page);
    if (window.WWnet) WWnet.highlight(page);
    if (window.WWpanels) WWpanels.highlight(page);
    if (!page) scheduleHidePopup();
  }

  /* -- mode toggle --------------------------------------------------------- */
  const umapView = document.getElementById("umap-view");
  const netView = document.getElementById("network-view");
  const btnUmap = document.getElementById("btn-umap");
  const btnNet = document.getElementById("btn-network");

  function setMode(mode) {
    state.mode = mode;
    highlight(null);
    umapView.classList.toggle("hidden", mode !== "umap");
    netView.classList.toggle("hidden", mode !== "network");
    btnUmap.classList.toggle("active", mode === "umap");
    btnNet.classList.toggle("active", mode === "network");
    if (mode === "network") WWnet.show();
  }
  btnUmap.addEventListener("click", () => setMode("umap"));
  btnNet.addEventListener("click", () => setMode("network"));

  /* -- layout bus ---------------------------------------------------------- */
  /* Every view sizes itself from its live container box, and none of them
     hear about a CSS-driven width change on their own. One debounced
     handler here re-fits whichever ones have been initialized. */
  const STOWED_KEY = "ww.panelsStowed";
  const SLIDE_MS = 250;           // must match the #panels width transition
  const btnPanels = document.getElementById("btn-panels");
  let relayoutTimer = null;

  function relayoutAll() {
    if (window.WWpanels) WWpanels.relayout();
    if (window.WWmap) WWmap.relayout();
    if (window.WWnet) WWnet.relayout();
  }

  function relayoutSoon(delay) {
    clearTimeout(relayoutTimer);
    relayoutTimer = setTimeout(relayoutAll, delay);
  }

  window.addEventListener("resize", () => relayoutSoon(150));

  /* -- panels toggle ------------------------------------------------------- */
  function setPanelsStowed(stowed) {
    document.body.classList.toggle("panels-stowed", stowed);
    btnPanels.classList.toggle("active", !stowed);
    btnPanels.setAttribute("aria-expanded", String(!stowed));
    btnPanels.title = stowed ? "Show summary panels" : "Hide summary panels";
    try {
      localStorage.setItem(STOWED_KEY, stowed ? "1" : "0");
    } catch (err) {
      /* private browsing or a file:// origin: the preference just does not
         persist, which is not worth interrupting the page for. */
    }
    // Redraw once the slide has settled, on the final width.
    relayoutSoon(SLIDE_MS + 30);
  }

  btnPanels.addEventListener("click", () =>
    setPanelsStowed(!document.body.classList.contains("panels-stowed")));

  /* Restore the saved state before boot() measures anything, so the first
     render already uses the correct panel width. The no-anim class keeps a
     stowed sidebar from visibly sliding shut on page load. */
  (function restorePanelsState() {
    let saved = null;
    try {
      saved = localStorage.getItem(STOWED_KEY);
    } catch (err) {
      /* see above */
    }
    document.body.classList.add("no-anim");
    setPanelsStowed(saved === "1");
    requestAnimationFrame(() =>
      requestAnimationFrame(() => document.body.classList.remove("no-anim")));
  })();

  /* -- boot ---------------------------------------------------------------- */
  async function boot() {
    const response = await fetch("/dashboard");
    state.data = await response.json();
    const data = state.data;

    const topicSet = new Set();
    data.pages.forEach(p => topicSet.add(p.macro_topic || "Unclassified"));
    if (data.reference) {
      data.reference.head_fractions.forEach(f => topicSet.add(f.macro_topic));
    }
    const topics = [...topicSet].filter(t => t !== "Unclassified").sort();
    topics.forEach((topic, i) => {
      state.topicColors[topic] = PALETTE[i % PALETTE.length];
    });
    state.topicColors["Unclassified"] = UNCLASSIFIED_COLOR;

    const s = data.summary;
    document.getElementById("summary-line").textContent =
      `${s.unique_pages} pages | ${s.total_visits} visits | ` +
      `${(s.first_visit || "").slice(0, 10)} to ${(s.last_visit || "").slice(0, 10)}`;

    if (!data.pages.length) {
      document.getElementById("viz-message").textContent =
        "No history ingested yet - run the extractor, then POST /ingest " +
        "and /enrich.";
      document.getElementById("viz-message").classList.remove("hidden");
      btnUmap.disabled = btnNet.disabled = true;
      return;
    }

    WWpanels.init(state);
    WWnet.init(state);

    const hasMap = data.reference !== null &&
      data.pages.some(p => p.x !== null && p.x !== undefined);
    if (hasMap) {
      WWmap.init(state);
      setMode("umap");
    } else {
      btnUmap.disabled = true;
      btnUmap.title = "Run scripts/build_reference_space.py and POST " +
        "/enrich to enable the UMAP view";
      setMode("network");
    }
  }
  boot();

  return { state, colorFor, hexToRgba, pageUrl, esc,
           showPopup, scheduleHidePopup, highlight };
})();
