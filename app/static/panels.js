/* Sidebar statistics panels (D3): grouped category bars (you vs popular
   pages), visits per day, and visits by hour of day. On node hover the
   irrelevant parts fade and visit markers appear on the time panels.

   The panels stack vertically in a narrow column, so the category chart
   runs horizontally: topic names read straight out along the left edge at
   any panel width, where rotated labels under vertical bars did not.

   Everything here is drawn once, in a fixed logical coordinate space, and
   the SVG viewBox scales it to whatever width the sidebar currently has.
   Two things follow from that. Dragging the sidebar edge scales the marks
   and the type together, rather than giving the same-size text more room.
   And panel size no longer depends on the window: the charts keep their
   aspect ratio, the sidebar scrolls when they no longer fit, and a resize
   needs no redraw at all. */

window.WWpanels = (function () {
  let state = null;
  const cat = {};    // {svg, groups}
  const daily = {};  // {svg, x, y, plotHeight, markerLayer}
  const hours = {};  // {svg, x, y, plotHeight, markerLayer}
  // Type sizes for the chart interiors. The panel headings are styled in
  // style.css; these are SVG text, so they live here. Margins below are
  // sized to fit them: raising a font size means raising its margin too.
  const AXIS_FONT = 10;
  const LABEL_FONT = 10.5;
  const LEGEND_FONT = 9.5;

  const MARGIN = { top: 6, right: 12, bottom: 34, left: 40 };
  // Horizontal bars: the left margin holds topic labels, not an axis.
  const CAT_MARGIN = { top: 16, right: 10, bottom: 22, left: 110 };

  // Logical drawing size. The width is shared so all three panels use one
  // horizontal scale; the heights set each panel's aspect ratio, and the
  // categories panel is taller because it has one row per macro topic.
  const LOGICAL_W = 320;
  const LOGICAL_H = { categories: 250, daily: 165, hours: 165 };

  function panelSvg(id, logicalHeight) {
    return d3.select(`#${id} svg`).html(null)
      .attr("viewBox", `0 0 ${LOGICAL_W} ${logicalHeight}`)
      .attr("preserveAspectRatio", "xMidYMid meet");
  }

  /* Hand-written abbreviations for the macro topics too long to fit the
     label gutter. The slice below is the fallback for anything new that
     turns up in the reference sample; a <title> carries the full name. */
  const SHORT_LABELS = {
    "History and Society": "History/Society",
    "Philosophy and religion": "Phil. & religion",
  };

  function shortLabel(topic) {
    const label = topic.replace(/^Culture\./, "");
    if (SHORT_LABELS[label]) return SHORT_LABELS[label];
    if (label.length > 17) return label.slice(0, 16) + "...";
    return label;
  }

  /* -- categories --------------------------------------------------------- */
  function renderCategories() {
    const w = LOGICAL_W;
    const h = LOGICAL_H.categories;
    const svg = panelSvg("panel-categories", h);
    cat.svg = svg;

    const user = {};
    state.data.stats.topics.macro_topics.forEach(t => {
      user[t.macro_topic] = t.fraction;
    });
    const popular = {};
    if (state.data.reference) {
      state.data.reference.head_fractions.forEach(f => {
        popular[f.macro_topic] = f.fraction;
      });
    }
    const topics = [...new Set([...Object.keys(user), ...Object.keys(popular)])]
      .sort((a, b) => (user[b] || 0) - (user[a] || 0) || a.localeCompare(b));

    const y = d3.scaleBand().domain(topics)
      .range([CAT_MARGIN.top, h - CAT_MARGIN.bottom])
      .paddingInner(0.28).paddingOuter(0.1);
    const xMax = d3.max(topics, t => Math.max(user[t] || 0, popular[t] || 0)) || 1;
    const x = d3.scaleLinear().domain([0, xMax]).nice()
      .range([CAT_MARGIN.left, w - CAT_MARGIN.right]);

    svg.append("g")
      .attr("transform", `translate(0,${h - CAT_MARGIN.bottom})`)
      .call(d3.axisBottom(x).ticks(3, "~%").tickSizeOuter(0))
      .attr("font-size", AXIS_FONT).attr("color", "#8a8880");

    const half = y.bandwidth() / 2;
    cat.groups = svg.append("g").selectAll("g")
      .data(topics).join("g")
      .attr("class", "dimmable")
      .attr("data-topic", t => t);

    cat.groups.append("rect")   // you
      .attr("x", x(0))
      .attr("y", t => y(t))
      .attr("width", t => x(user[t] || 0) - x(0))
      .attr("height", half)
      .attr("fill", t => WW.colorFor(t));
    cat.groups.append("rect")   // popular pages baseline
      .attr("x", x(0))
      .attr("y", t => y(t) + half)
      .attr("width", t => x(popular[t] || 0) - x(0))
      .attr("height", half)
      .attr("fill", t => WW.hexToRgba(WW.colorFor(t), 0.45));
    cat.groups.append("text")
      .attr("x", CAT_MARGIN.left - 5)
      .attr("y", t => y(t) + y.bandwidth() / 2)
      .attr("dy", "0.32em")
      .attr("text-anchor", "end")
      .attr("font-size", LABEL_FONT)
      .attr("fill", "#6b6a64")
      .text(shortLabel)
      .append("title")            // full name when the label is truncated
      .text(t => t);

    const legend = svg.append("g")
      .attr("transform", `translate(${CAT_MARGIN.left}, 1)`)
      .attr("font-size", LEGEND_FONT).attr("fill", "#8a8880");
    legend.append("rect").attr("width", 9).attr("height", 9)
      .attr("fill", "#8a8880");
    legend.append("text").attr("x", 13).attr("y", 8).text("you");
    legend.append("rect").attr("x", 44).attr("width", 9).attr("height", 9)
      .attr("fill", WW.hexToRgba("#8a8880", 0.45));
    legend.append("text").attr("x", 57).attr("y", 8).text("popular");
  }

  /* -- visits per day ------------------------------------------------------ */
  function renderDaily() {
    const w = LOGICAL_W;
    const h = LOGICAL_H.daily;
    const svg = panelSvg("panel-daily", h);
    daily.svg = svg;

    const days = state.data.stats.daily.days.map(d => {
      const [yy, mm, dd] = d.date.split("-").map(Number);
      return { ...d, dateObj: new Date(yy, mm - 1, dd) };
    });
    if (!days.length) return;

    const x = d3.scaleTime()
      .domain(d3.extent(days, d => d.dateObj))
      .range([MARGIN.left, w - MARGIN.right]);
    const y = d3.scaleLinear()
      .domain([0, d3.max(days, d => d.visits) || 1]).nice()
      .range([h - MARGIN.bottom, MARGIN.top]);
    daily.x = x;
    daily.plotTop = MARGIN.top;
    daily.plotBottom = h - MARGIN.bottom;

    svg.append("g")
      .attr("transform", `translate(0,${h - MARGIN.bottom})`)
      .call(d3.axisBottom(x).ticks(3).tickSizeOuter(0))
      .attr("font-size", AXIS_FONT).attr("color", "#8a8880");
    svg.append("g")
      .attr("transform", `translate(${MARGIN.left},0)`)
      .call(d3.axisLeft(y).ticks(3))
      .attr("font-size", AXIS_FONT).attr("color", "#8a8880");

    const content = svg.append("g").attr("class", "dimmable");
    daily.content = content;
    content.append("path")
      .datum(days)
      .attr("fill", "none")
      .attr("stroke", WW.hexToRgba("#7295b8", 0.4))
      .attr("stroke-width", 1)
      .attr("d", d3.line()
        .x(d => x(d.dateObj)).y(d => y(d.visits)));
    content.append("path")
      .datum(days.filter(d => d.rolling_avg !== null))
      .attr("fill", "none")
      .attr("stroke", "#7295b8")
      .attr("stroke-width", 1.8)
      .attr("d", d3.line()
        .x(d => x(d.dateObj)).y(d => y(d.rolling_avg)));

    daily.markerLayer = svg.append("g");
  }

  /* -- time of day --------------------------------------------------------- */
  function renderHours() {
    const w = LOGICAL_W;
    const h = LOGICAL_H.hours;
    const svg = panelSvg("panel-hours", h);
    hours.svg = svg;

    const grid = state.data.stats.hour_grid.counts;   // 7 x 24
    const perHour = d3.range(24).map(hour =>
      d3.sum(grid, dayRow => dayRow[hour]));

    const x = d3.scaleLinear().domain([0, 24])
      .range([MARGIN.left, w - MARGIN.right]);
    const y = d3.scaleLinear().domain([0, d3.max(perHour) || 1]).nice()
      .range([h - MARGIN.bottom, MARGIN.top]);
    hours.x = x;
    hours.plotTop = MARGIN.top;
    hours.plotBottom = h - MARGIN.bottom;

    svg.append("g")
      .attr("transform", `translate(0,${h - MARGIN.bottom})`)
      .call(d3.axisBottom(x).tickValues([0, 6, 12, 18, 24])
        .tickFormat(v => `${v}:00`).tickSizeOuter(0))
      .attr("font-size", AXIS_FONT).attr("color", "#8a8880");
    svg.append("g")
      .attr("transform", `translate(${MARGIN.left},0)`)
      .call(d3.axisLeft(y).ticks(3))
      .attr("font-size", AXIS_FONT).attr("color", "#8a8880");

    const barWidth = (x(1) - x(0)) * 0.8;
    hours.content = svg.append("g").attr("class", "dimmable");
    hours.content.selectAll("rect")
      .data(perHour).join("rect")
      .attr("x", (v, hour) => x(hour) + (x(1) - x(0)) * 0.1)
      .attr("y", v => y(v))
      .attr("width", barWidth)
      .attr("height", v => y(0) - y(v))
      .attr("fill", WW.hexToRgba("#7295b8", 0.75));

    hours.markerLayer = svg.append("g");
  }

  /* -- hover linking ------------------------------------------------------- */
  function highlight(page) {
    if (cat.groups) {
      const topic = page ? (page.macro_topic || "Unclassified") : null;
      cat.groups.attr("opacity", function () {
        return !topic ||
          this.getAttribute("data-topic") === topic ? 1 : 0.2;
      });
    }
    for (const panel of [daily, hours]) {
      if (!panel.svg) continue;
      panel.content.attr("opacity", page ? 0.25 : 1);
      panel.markerLayer.selectAll("*").remove();
    }
    if (page && daily.svg) {
      const stamps = page.visits.map(ts => new Date(ts));  // local time
      daily.markerLayer.selectAll("line")
        .data(stamps).join("line")
        .attr("class", "marker")
        .attr("x1", d => daily.x(
          new Date(d.getFullYear(), d.getMonth(), d.getDate())))
        .attr("x2", d => daily.x(
          new Date(d.getFullYear(), d.getMonth(), d.getDate())))
        .attr("y1", daily.plotTop)
        .attr("y2", daily.plotBottom);
      hours.markerLayer.selectAll("line")
        .data(stamps).join("line")
        .attr("class", "marker")
        .attr("x1", d => hours.x(d.getHours() + d.getMinutes() / 60))
        .attr("x2", d => hours.x(d.getHours() + d.getMinutes() / 60))
        .attr("y1", hours.plotTop)
        .attr("y2", hours.plotBottom);
    }
  }

  function renderAll() {
    renderCategories();
    renderDaily();
    renderHours();
  }

  function init(appState) {
    state = appState;
    renderAll();
  }

  /* Nothing to do: the charts live in a fixed logical space and the
     viewBox rescales them for free. Kept so every view answers the same
     relayout() call from main.js. */
  function relayout() {}

  return { init, highlight, relayout };
})();
