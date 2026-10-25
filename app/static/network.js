/* Network mode: force-directed reading-session graph via cytoscape.js.
   Hover enlarges a node, shows the shared popup, highlights its
   neighborhood, and fades everything else so connections can be
   followed hop by hop. */

window.WWnet = (function () {
  let state = null;
  let cy = null;
  let built = false;

  function build() {
    const pages = state.data.pages;
    const maxPagerank = Math.max(...pages.map(p => p.pagerank), 1e-9);
    const size = p => 10 + 46 * (p.pagerank / maxPagerank);

    const elements = [
      ...pages.map(p => ({
        data: { id: p.title, page: p, size: size(p) },
      })),
      ...state.data.edges.map(e => ({
        data: { source: e.source, target: e.target, weight: e.weight },
      })),
    ];

    cy = cytoscape({
      container: document.getElementById("network-view"),
      elements: elements,
      layout: { name: "cose", animate: false, padding: 40 },
      wheelSensitivity: 0.25,
      style: [
        { selector: "node", style: {
            "background-color":
              ele => WW.colorFor(ele.data("page").macro_topic),
            "width": "data(size)",
            "height": "data(size)",
            "label": "data(id)",
            "font-size": 8,
            "color": "#6b6a64",
            "text-valign": "bottom",
            "text-margin-y": 3,
            "border-width": 1,
            "border-color": "#ffffff",
            "transition-property": "opacity width height",
            "transition-duration": "0.15s",
        } },
        { selector: "edge", style: {
            "width": ele => Math.min(1 + ele.data("weight") * 0.7, 5),
            "line-color": "#c9c5bc",
            "target-arrow-color": "#c9c5bc",
            "target-arrow-shape": "triangle",
            "arrow-scale": 0.6,
            "curve-style": "bezier",
            "opacity": 0.7,
            "transition-property": "opacity line-color",
            "transition-duration": "0.15s",
        } },
        { selector: ".faded", style: { "opacity": 0.08, "label": "" } },
        { selector: "node.hl", style: { "border-color": "#5b5a55" } },
        { selector: "edge.hl", style: {
            "line-color": "#8f8c83", "target-arrow-color": "#8f8c83",
            "opacity": 1,
        } },
        /* Filter styles come last so they beat the per-element colour
           function in the base node rule. Hiding a node hides its edges
           too, and display:none leaves the laid-out positions alone, so
           nothing reflows while a sweep runs. */
        { selector: "node.greyed", style: {
            "background-color": WW.GREY, "color": "#c2beb6",
        } },
        { selector: "node.filtered-out", style: { "display": "none" } },
      ],
    });

    cy.on("mouseover", "node", event => {
      const node = event.target;
      const page = node.data("page");
      const hood = node.closedNeighborhood();
      cy.elements().not(hood).addClass("faded");
      hood.addClass("hl");
      node.data("size0", node.data("size"));
      node.data("size", node.data("size") * 1.4);
      const oe = event.originalEvent;
      if (oe) WW.showPopup(page, oe.clientX, oe.clientY);
      if (window.WWpanels) WWpanels.highlight(page);
    });

    cy.on("mouseout", "node", event => {
      const node = event.target;
      if (node.data("size0")) node.data("size", node.data("size0"));
      clearClasses();
      WW.highlight(null);
    });
  }

  function clearClasses() {
    if (cy) cy.elements().removeClass("faded hl");
  }

  /* -- filters ------------------------------------------------------------- */
  /* Toggling a class on every node each frame of a sweep is the one place
     in this app where the work is worth avoiding: there are as many nodes
     as visited pages. So only the nodes whose visibility actually changed
     are touched, the whole pass runs inside one cytoscape batch, and while
     the graph is off screen nothing happens at all - the pending flag
     makes show() catch up in a single pass instead. */
  let hidden = new Set();
  let filtersPending = false;

  function applyFilters(topicChanged) {
    if (!cy) return;
    if (WW.state.mode !== "network") {
      filtersPending = true;
      return;
    }
    filtersPending = false;
    cy.batch(() => {
      cy.nodes().forEach(node => {
        const page = node.data("page");
        const visible = WW.pageVisible(page);
        if (visible === hidden.has(page.title)) {
          node.toggleClass("filtered-out", !visible);
          if (visible) hidden.delete(page.title);
          else hidden.add(page.title);
        }
        if (topicChanged) node.toggleClass("greyed", WW.pageGreyed(page));
      });
    });
  }

  function init(appState) {
    state = appState;
  }

  /* Lay the graph out at startup rather than on the first click.

     The cose layout over a graph this size is seconds of blocking work,
     and it used to be spent the first time the Threads tab was pressed -
     the one moment the user is waiting on it. Spending it during launch
     costs the same time but nobody is waiting on the answer yet, and the
     tab then opens instantly for the rest of the session.

     The rAF-then-timeout is what makes the launch overlay work: the frame
     callback runs before the paint, and the timeout it queues runs after
     that paint has been committed. So "Launching..." is on the glass
     before the layout seizes the main thread, which it could not be if
     build() were called straight through. `done` fires only once the
     graph genuinely exists, and is what takes the overlay back down.

     Building at all here is only possible because the inactive view keeps
     its layout box (see style.css) - cytoscape cannot measure a
     display:none container. */
  function prebuild(done) {
    requestAnimationFrame(() => setTimeout(() => {
      if (!built) {
        built = true;
        build();
        filtersPending = true;
      }
      done();
    }, 0));
  }

  function show() {
    // Falls back to building on demand if the tab is somehow reached
    // before prebuild has run.
    if (!built) {
      built = true;
      build();
      filtersPending = true;
    } else if (cy) {
      cy.resize();
    }
    // setMode() flips the container visible before calling us, so by here
    // WW.state.mode is already "network" and applyFilters will not defer.
    if (filtersPending) applyFilters(true);
  }

  function highlight(page) {
    // Map-driven hovers do not need to touch the (hidden) network; only
    // clear our own classes when the bus resets.
    if (!page) clearClasses();
  }

  /* cytoscape caches its container size, so it needs telling when the
     sidebar or the window changes the width of #network-view. */
  function relayout() {
    if (cy) cy.resize();
  }

  return { init, show, prebuild, highlight, relayout, applyFilters };
})();
