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

  function init(appState) {
    state = appState;
  }

  /* Built lazily on first show: cytoscape needs a visible container to
     measure itself for the layout. */
  function show() {
    if (!built) {
      built = true;
      build();
    } else if (cy) {
      cy.resize();
    }
  }

  function highlight(page) {
    // Map-driven hovers do not need to touch the (hidden) network; only
    // clear our own classes when the bus resets.
    if (!page) clearClasses();
  }

  return { init, show, highlight };
})();
