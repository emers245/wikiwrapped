/* UMAP mode: canvas backdrop of reference articles (non-interactive) +
   SVG layer of the user's pages, synced d3.zoom, hover highlight. */

window.WWmap = (function () {
  let state = null;
  let canvas, ctx, svg, group;
  let zoomTransform = d3.zoomIdentity;
  let toX, toY;                 // data -> base pixel scales (equal aspect)
  let backdropPoints = [];
  let userPages = [];
  let width = 0, height = 0;

  function radiusOf(page) {
    return 4 + Math.sqrt(page.visit_count) * 1.4;
  }

  function computeScales() {
    const rect = document.getElementById("umap-view").getBoundingClientRect();
    width = rect.width;
    height = rect.height;
    const xs = [], ys = [];
    backdropPoints.forEach(p => { xs.push(p.x); ys.push(p.y); });
    userPages.forEach(p => { xs.push(p.x); ys.push(p.y); });
    const xe = d3.extent(xs), ye = d3.extent(ys);
    const pad = 40;
    const k = Math.min(
      (width - 2 * pad) / Math.max(xe[1] - xe[0], 1e-9),
      (height - 2 * pad) / Math.max(ye[1] - ye[0], 1e-9)
    );
    const cx = (xe[0] + xe[1]) / 2;
    const cy = (ye[0] + ye[1]) / 2;
    toX = v => (v - cx) * k + width / 2;
    toY = v => height / 2 - (v - cy) * k;   // UMAP y up, screen y down
  }

  function drawBackdrop() {
    const dpr = window.devicePixelRatio || 1;
    canvas.width = width * dpr;
    canvas.height = height * dpr;
    ctx.save();
    ctx.scale(dpr, dpr);
    ctx.clearRect(0, 0, width, height);
    ctx.translate(zoomTransform.x, zoomTransform.y);
    ctx.scale(zoomTransform.k, zoomTransform.k);
    for (const p of backdropPoints) {
      ctx.fillStyle = p.color;
      ctx.beginPath();
      ctx.arc(toX(p.x), toY(p.y), 1.7, 0, 6.2832);
      ctx.fill();
    }
    ctx.restore();
  }

  function render() {
    computeScales();
    drawBackdrop();
    group.selectAll("circle")
      .data(userPages, p => p.title)
      .join("circle")
      .attr("cx", p => toX(p.x))
      .attr("cy", p => toY(p.y))
      .attr("r", radiusOf)
      .attr("fill", p => WW.colorFor(p.macro_topic))
      .on("mouseenter", (event, page) => {
        WW.highlight(page);
        WW.showPopup(page, event.clientX, event.clientY);
      })
      .on("mouseleave", () => WW.highlight(null));
  }

  function init(appState) {
    state = appState;
    canvas = document.getElementById("backdrop");
    ctx = canvas.getContext("2d");
    svg = d3.select("#umap-svg");
    group = svg.append("g");

    backdropPoints = (state.data.reference ? state.data.reference.backdrop : [])
      .map(p => ({
        ...p,
        color: WW.hexToRgba(WW.colorFor(p.macro_topic), 0.13),
      }));
    userPages = state.data.pages.filter(
      p => p.x !== null && p.x !== undefined
    );

    svg.call(
      d3.zoom()
        .scaleExtent([0.5, 25])
        .on("zoom", event => {
          zoomTransform = event.transform;
          group.attr("transform", event.transform);
          drawBackdrop();
        })
    );
    render();
  }

  /* Scales are derived from the #umap-view box, so the map has to re-fit
     whenever the sidebar or the window changes its width. */
  function relayout() {
    if (group) render();
  }

  function highlight(page) {
    if (!group) return;
    canvas.style.opacity = page ? 0.25 : 1;
    group.selectAll("circle")
      .attr("opacity", p => !page || p.title === page.title ? 1 : 0.15)
      .attr("r", p =>
        page && p.title === page.title ? radiusOf(p) * 1.5 : radiusOf(p));
  }

  return { init, highlight, relayout };
})();
