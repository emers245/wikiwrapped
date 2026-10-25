/* Wikipedia Wrapped frontend bootstrap: fetches /dashboard once, owns the
   shared topic palette, the hover popup, and the two cross-component
   buses - transient highlight (hover) and persistent filter (selection).
   Muted palette on an off-white theme. */

window.WW = (function () {
  const PALETTE = [
    "#7295b8", "#e0a458", "#7fb586", "#c66e6e", "#9d85b8",
    "#a98b78", "#d495b6", "#8f9779", "#b8b16d", "#6fb3b8",
    "#c99a7a", "#94a7c4", "#b0c98f", "#c98fa6", "#87a8a4",
  ];
  const UNCLASSIFIED_COLOR = "#b3afa7";
  // Fill for a page whose topic is not the selected one. Lighter and less
  // saturated than any palette entry, so a selection reads at a glance.
  const GREY = "#d3cfc7";

  const state = { data: null, topicColors: {}, mode: "umap" };

  function esc(text) {
    const div = document.createElement("div");
    div.textContent = text == null ? "" : String(text);
    return div.innerHTML;
  }

  function colorFor(topic) {
    return state.topicColors[topic || "Unclassified"] || UNCLASSIFIED_COLOR;
  }

  /* Display name for a macro topic. The stored labels keep the taxonomy
     shape ("Culture.Food and drink"); this is the one place that decides
     how they read on screen, so the panels and the popup always agree. */
  const TOPIC_RENAMES = { "Food and drink": "Food & drink" };

  function topicLabel(topic) {
    const name = (topic || "Unclassified").replace(/^Culture\./, "");
    return TOPIC_RENAMES[name] || name;
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
        <tr><td>Topic</td><td>${esc(topicLabel(page.macro_topic))}</td></tr>
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

  /* -- filter bus ---------------------------------------------------------- */
  /* Selections made in the sidebar, applied to whichever visualization is
     showing. The two kinds are deliberately different: a topic selection
     greys the pages that do not match, so the shape of the whole data set
     stays visible behind the one you picked, while the time selections
     remove pages outright, which is what makes a sweep read as motion.

     The panels own the widgets and call setFilter(); the views only read
     the two predicates below. Nothing here recomputes the panels
     themselves - they always describe the full history, so the track a
     slider runs along cannot move while you are dragging it. */
  const filters = { topic: null, dayRange: null, hour: null };

  function topicGreyed(topic) {
    return filters.topic !== null && (topic || "Unclassified") !== filters.topic;
  }

  function pageGreyed(page) {
    return topicGreyed(page.macro_topic);
  }

  /* A page survives if at least one visit falls in the day range and at
     least one falls in the selected hour - not necessarily the same visit.
     `_times` is sorted, so most pages reject on the first comparison. */
  function pageVisible(page) {
    const range = filters.dayRange;
    const hour = filters.hour;
    if (range === null && hour === null) return true;
    const times = page._times;
    if (!times.length) return false;
    if (hour !== null && !page._hours.has(hour)) return false;
    if (range === null) return true;
    if (times[0] >= range[1] || times[times.length - 1] < range[0]) return false;
    return times.some(t => t >= range[0] && t < range[1]);
  }

  let filterFrame = null;

  function setFilter(patch) {
    const topicChanged = "topic" in patch && patch.topic !== filters.topic;
    Object.assign(filters, patch);
    // A sweep calls this every frame, so coalesce to one repaint per frame.
    if (filterFrame !== null) {
      filterFrame.topicChanged = filterFrame.topicChanged || topicChanged;
      return;
    }
    filterFrame = { topicChanged: topicChanged };
    requestAnimationFrame(() => {
      const pending = filterFrame;
      filterFrame = null;
      if (window.WWmap) WWmap.applyFilters(pending.topicChanged);
      if (window.WWnet) WWnet.applyFilters(pending.topicChanged);
      if (window.WWpanels) WWpanels.syncFilters();
    });
  }

  // Escape is the one global way out of every selection at once.
  window.addEventListener("keydown", event => {
    if (event.key !== "Escape") return;
    if (window.WWpanels) WWpanels.clearAll();
  });

  /* -- mode toggle --------------------------------------------------------- */
  const umapView = document.getElementById("umap-view");
  const netView = document.getElementById("network-view");
  const btnUmap = document.getElementById("btn-umap");
  const btnNet = document.getElementById("btn-network");

  function setMode(mode) {
    state.mode = mode;
    highlight(null);
    umapView.classList.toggle("inactive", mode !== "umap");
    netView.classList.toggle("inactive", mode !== "network");
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

  /* -- panels resize handle ------------------------------------------------ */
  /* Drag the sidebar edge to scale the panels. Width is the only input:
     each chart keeps a fixed aspect ratio, so widening makes the figures
     and their type bigger, and the sidebar scrolls once they stop fitting.
     Everything downstream reads the --panel-w custom property. */
  const WIDTH_KEY = "ww.panelWidth";
  const KEY_STEP_PX = 20;
  const resizer = document.getElementById("panels-resizer");
  const rootStyle = document.documentElement.style;
  let defaultWidth = null;      // the stylesheet value, captured before we
                                // ever write an inline override
  let pendingFrame = null;

  function widthBounds() {
    const css = getComputedStyle(document.documentElement);
    return {
      min: parseFloat(css.getPropertyValue("--panel-w-min")) || 260,
      max: parseFloat(css.getPropertyValue("--panel-w-max")) || 720,
    };
  }

  function currentWidth() {
    return parseFloat(
      getComputedStyle(document.documentElement).getPropertyValue("--panel-w")
    );
  }

  function setPanelWidth(px, persist) {
    const { min, max } = widthBounds();
    const width = Math.round(Math.min(Math.max(px, min), max));
    rootStyle.setProperty("--panel-w", width + "px");
    if (persist) {
      try {
        localStorage.setItem(WIDTH_KEY, String(width));
      } catch (err) {
        /* see the stow-state note above */
      }
    }
    // Coalesce to one relayout per frame: the map redraws 10k backdrop
    // points and a drag fires far more often than the display refreshes.
    if (pendingFrame === null) {
      pendingFrame = requestAnimationFrame(() => {
        pendingFrame = null;
        relayoutAll();
      });
    }
  }

  resizer.addEventListener("pointerdown", event => {
    event.preventDefault();
    const startX = event.clientX;
    const startWidth = currentWidth();
    resizer.setPointerCapture(event.pointerId);
    document.body.classList.add("resizing");

    const onMove = moveEvent =>
      setPanelWidth(startWidth + (moveEvent.clientX - startX), false);
    const onUp = () => {
      resizer.removeEventListener("pointermove", onMove);
      resizer.removeEventListener("pointerup", onUp);
      resizer.removeEventListener("pointercancel", onUp);
      document.body.classList.remove("resizing");
      setPanelWidth(currentWidth(), true);   // persist the final width only
    };
    resizer.addEventListener("pointermove", onMove);
    resizer.addEventListener("pointerup", onUp);
    resizer.addEventListener("pointercancel", onUp);
  });

  // Double-click restores the width the stylesheet shipped with.
  resizer.addEventListener("dblclick", () => setPanelWidth(defaultWidth, true));

  resizer.addEventListener("keydown", event => {
    const step = event.key === "ArrowLeft" ? -KEY_STEP_PX
      : event.key === "ArrowRight" ? KEY_STEP_PX : 0;
    if (!step) return;
    event.preventDefault();
    setPanelWidth(currentWidth() + step, true);
  });

  /* Restore the saved state before boot() measures anything, so the first
     render already uses the correct panel width. The no-anim class keeps a
     stowed sidebar from visibly sliding shut on page load. */
  (function restorePanelsState() {
    defaultWidth = currentWidth();

    let savedStowed = null;
    let savedWidth = null;
    try {
      savedStowed = localStorage.getItem(STOWED_KEY);
      savedWidth = localStorage.getItem(WIDTH_KEY);
    } catch (err) {
      /* see above */
    }
    document.body.classList.add("no-anim");
    if (savedWidth !== null && !Number.isNaN(parseFloat(savedWidth))) {
      setPanelWidth(parseFloat(savedWidth), false);
    }
    setPanelsStowed(savedStowed === "1");
    requestAnimationFrame(() =>
      requestAnimationFrame(() => document.body.classList.remove("no-anim")));
  })();

  /* -- background jobs ----------------------------------------------------- */
  /* The launcher syncs and enriches AFTER the window opens (app/jobs.py),
     so the dashboard first shows whatever data already exists and this
     strip reports the job. When it finishes, /health is compared with what
     was loaded: an empty dashboard reloads by itself, a populated one
     offers a Reload button rather than yanking away the user's view. */
  const POLL_RUNNING_MS = 1000;
  const POLL_IDLE_MS = 5000;
  const strip = document.getElementById("job-strip");
  const jobMessage = document.getElementById("job-message");
  const jobBar = document.getElementById("job-bar");
  const jobFill = document.getElementById("job-bar-fill");
  const jobCount = document.getElementById("job-count");
  const jobReload = document.getElementById("job-reload");
  const jobSettings = document.getElementById("job-settings");
  const jobDismiss = document.getElementById("job-dismiss");
  let pollTimer = null;
  const REPORTED_KEY = "ww.reportedJob";
  let lastJobId = null;      // the job last seen running
  /* The job whose result was already shown. Kept in sessionStorage so the
     Reload button does not bring the same report straight back; a new
     window is a new session, so a fresh launch still reports its job. */
  let reported = null;
  try {
    reported = JSON.parse(sessionStorage.getItem(REPORTED_KEY));
  } catch (err) {
    /* storage unavailable: at worst a report shows twice */
  }

  function markReported(id) {
    reported = id;
    try {
      sessionStorage.setItem(REPORTED_KEY, JSON.stringify(id));
    } catch (err) {
      /* see above */
    }
  }

  jobReload.addEventListener("click", () => location.reload());
  jobDismiss.addEventListener("click", () => strip.classList.add("hidden"));
  jobSettings.addEventListener("click", () => {
    strip.classList.add("hidden");
    if (window.WWsetup) WWsetup.open("settings");
  });

  function showRunning(job) {
    strip.classList.remove("hidden", "problem");
    jobReload.classList.add("hidden");
    jobSettings.classList.add("hidden");
    jobDismiss.classList.add("hidden");
    const step = job.steps > 1 ? ` (step ${job.step_index} of ${job.steps})` : "";
    jobMessage.textContent = (job.message || job.step || "Working") + step;
    const counted = job.total !== null && job.total > 0;
    jobBar.classList.remove("hidden");
    jobBar.classList.toggle("indeterminate", !counted);
    jobFill.style.width = counted
      ? Math.round(100 * job.done / job.total) + "%" : "";
    jobCount.textContent = counted ? `${job.done} / ${job.total}` : "";
  }

  /* What changed since the dashboard was loaded. Visits are what a sync
     adds; topics and coordinates are what enrichment adds. */
  function dataChanged(health) {
    const loaded = state.loaded;
    if (!loaded) return false;
    const gaps = health.enrichment || {};
    return health.visits !== loaded.visits ||
      (gaps.topics_missing ?? loaded.unlabeled) < loaded.unlabeled ||
      (gaps.coords_missing ?? loaded.noCoords) < loaded.noCoords;
  }

  /* Problems in the user's terms. The sync's own messages are written for
     a terminal ("Run: python scripts/setup_takeout.py --authorize ..."),
     so an account that needs signing in again is described here instead,
     and the raw duplicate is dropped. */
  function describeProblems(job, health) {
    const sync = (health && health.sync) || { errors: [], reauthorize: [] };
    const out = sync.reauthorize.map(email =>
      `Google account ${email} needs you to sign in again (Settings)`);
    for (const message of [...job.errors, ...sync.errors]) {
      if (!message.startsWith("Re-authorization needed")) out.push(message);
    }
    return out;
  }

  async function showFinished(job) {
    await booted;            // the comparison needs the loaded dashboard
    let health = null;
    try {
      health = await (await fetch("/health")).json();
    } catch (err) {
      /* the strip still reports the job; there is just nothing to compare */
    }
    const problems = describeProblems(job, health);
    const changed = health !== null && dataChanged(health);

    if (changed && state.loaded && state.loaded.empty) {
      location.reload();
      return;
    }
    jobBar.classList.add("hidden");
    jobCount.textContent = "";
    strip.classList.toggle("problem", problems.length > 0);
    if (problems.length) {
      jobMessage.textContent = `Sync finished with ${problems.length} ` +
        `problem${problems.length === 1 ? "" : "s"}` +
        (changed ? "; new data is ready" : "");
      jobMessage.title = problems.join("\n");
    } else {
      jobMessage.textContent = changed ? "New data is ready" : "Up to date";
      jobMessage.title = "";
    }
    jobReload.classList.toggle("hidden", !changed);
    jobSettings.classList.toggle("hidden", !problems.length);
    jobDismiss.classList.remove("hidden");
    strip.classList.remove("hidden");
    if (!changed && !problems.length) {
      setTimeout(() => strip.classList.add("hidden"), 3000);
    }
  }

  async function pollJobs() {
    let job = null;
    try {
      job = await (await fetch("/jobs")).json();
    } catch (err) {
      schedulePoll(POLL_IDLE_MS);   // server busy or restarting
      return;
    }
    if (job.status === "running") {
      lastJobId = job.id;
      showRunning(job);
      schedulePoll(POLL_RUNNING_MS);
      return;
    }
    /* Report a job once, and only one this page saw running, or one that
       finished between the page loading and the first poll (the launch
       job when nothing needed doing). */
    const finished = job.status === "done" || job.status === "failed";
    // Job ids restart with each server, so the start time is part of the key.
    const key = `${job.id}@${job.started_at}`;
    if (finished && key !== reported &&
        (job.id === lastJobId || lastJobId === null)) {
      markReported(key);
      showFinished(job);
    }
    lastJobId = job.id;
    schedulePoll(POLL_IDLE_MS);
  }

  /* One timer, so pollJobsNow() (called when setup starts a sync) cannot
     leave two polling loops running side by side. */
  function schedulePoll(delay) {
    clearTimeout(pollTimer);
    pollTimer = setTimeout(pollJobs, delay);
  }

  function pollJobsNow() {
    clearTimeout(pollTimer);
    pollJobs();
  }

  /* -- boot ---------------------------------------------------------------- */
  /* Every exit from boot() has to reach this, including the failures:
     an overlay that is never dismissed is a worse outcome than the frozen
     dashboard it replaced. */
  function finishLaunch() {
    document.getElementById("loading").classList.add("done");
  }

  async function boot() {
    const response = await fetch("/dashboard");
    state.data = await response.json();
    const data = state.data;

    /* Parse each page's visit stamps once. The filter predicates run on
       every frame of a sweep and highlight() draws a marker per visit, so
       neither can afford to re-parse ISO strings on the way through.
       Local time throughout, matching the server-side stats, which convert
       to the device zone by default (config.get_timezone). */
    data.pages.forEach(page => {
      page._stamps = page.visits.map(ts => new Date(ts))
        .sort((a, b) => a - b);
      page._times = page._stamps.map(d => d.getTime());
      page._hours = new Set(page._stamps.map(d => d.getHours()));
    });

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

    /* Snapshot of what this page shows, for the job strip's "did anything
       change?" check once a background sync or enrichment finishes. */
    state.loaded = {
      visits: data.summary.total_visits,
      unlabeled: data.stats.topics.pages_unlabeled || 0,
      noCoords: data.pages.filter(p => p.x === null || p.x === undefined).length,
      empty: !data.pages.length,
    };

    const s = data.summary;
    document.getElementById("summary-line").textContent = s.total_visits
      ? `${s.unique_pages} pages | ${s.total_visits} visits | ` +
        `${(s.first_visit || "").slice(0, 10)} to ${(s.last_visit || "").slice(0, 10)}`
      : "No history yet";

    /* Enrichment is a separate pass from ingestion, so a fresh import can
       leave most of the history with no topic and no coordinates - which
       shows up as a dominant "Unclassified" bar and a near-empty map,
       neither of which looks like a missing step. Say so plainly instead.
       "Unlabeled" is not "Unclassified": the former has never been sent to
       the topic model, the latter came back with no prediction. */
    const unlabeled = data.stats.topics.pages_unlabeled || 0;
    const noCoords = data.pages.filter(p => p.x === null || p.x === undefined).length;
    if (unlabeled || noCoords) {
      const parts = [];
      if (unlabeled) parts.push(`${unlabeled} pages have no topic yet`);
      if (noCoords) parts.push(`${noCoords} are missing map coordinates`);
      const warning = document.getElementById("data-warning");
      // The launcher enriches in the background after every sync, so this
      // is a "still catching up" note rather than an instruction.
      warning.textContent = parts.join(", ") +
        " - they are added in the background after a sync";
      warning.classList.remove("hidden");
    }

    if (!data.pages.length) {
      document.getElementById("viz-message").textContent =
        "No reading history yet. It is read from Chrome (and any Takeout " +
        "folders) when the app syncs; the dashboard fills in when a sync " +
        "finishes. Sources and syncing are in Settings.";
      document.getElementById("viz-message").classList.remove("hidden");
      btnUmap.disabled = btnNet.disabled = true;
      finishLaunch();
      return;
    }

    WWpanels.init(state);
    WWnet.init(state);

    const hasMap = data.reference !== null &&
      data.pages.some(p => p.x !== null && p.x !== undefined);
    if (hasMap) {
      WWmap.init(state);
      setMode("umap");
      /* Build the thread graph now, behind the launch overlay, so the
         cost lands before the dashboard is handed over rather than on the
         first click of the Threads tab. The button stays disabled until
         the graph exists as a backstop - the overlay is what the user
         actually sees, but nothing should be clickable before it is real.
         setMode("network") already builds it in the other branch, so this
         is only for the case where Threads is the tab not opened. */
      btnNet.disabled = true;
      btnNet.title = "Building the thread graph...";
      WWnet.prebuild(() => {
        btnNet.disabled = false;
        btnNet.removeAttribute("title");
        finishLaunch();
      });
    } else {
      btnUmap.disabled = true;
      btnUmap.title = data.reference === null
        ? "The map's reference space is not installed"
        : "Map positions are still being added; they appear after the " +
          "background enrichment finishes";
      setMode("network");   // builds the graph synchronously
      finishLaunch();
    }
  }

  const booted = boot().catch(error => {
    // Without this the overlay would sit on "Launching..." forever, hiding
    // the one thing worth reporting.
    const message = document.getElementById("viz-message");
    message.textContent = "Could not load the dashboard: " + error.message;
    message.classList.remove("hidden");
    finishLaunch();
  });
  pollJobs();

  return { state, colorFor, hexToRgba, pageUrl, esc, topicLabel, GREY,
           showPopup, scheduleHidePopup, highlight,
           filters, setFilter, pageGreyed, pageVisible, topicGreyed,
           pollJobsNow };
})();
