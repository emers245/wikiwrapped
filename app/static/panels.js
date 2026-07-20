/* Bottom statistics panels (D3): grouped category bars (you vs popular
   pages), visits per day, and visits by hour of day. On node hover the
   irrelevant parts fade and visit markers appear on the time panels. */

window.WWpanels = (function () {
  let state = null;
  const cat = {};    // {svg, groups}
  const daily = {};  // {svg, x, y, plotHeight, markerLayer}
  const hours = {};  // {svg, x, y, plotHeight, markerLayer}
  const MARGIN = { top: 6, right: 12, bottom: 30, left: 34 };

  function panelSize(id) {
    const rect = document.querySelector(`#${id} svg`).getBoundingClientRect();
    return { w: Math.max(rect.width, 120), h: Math.max(rect.height, 80) };
  }

  function shortLabel(topic) {
    let label = topic.replace(/^Culture\./, "");
    if (label === "History and Society") label = "History/Society";
    if (label.length > 14) label = label.slice(0, 13) + "...";
    return label;
  }

  /* -- categories --------------------------------------------------------- */
  function renderCategories() {
    const { w, h } = panelSize("panel-categories");
    const svg = d3.select("#panel-categories svg").html(null)
      .attr("viewBox", `0 0 ${w} ${h}`);
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

    const x = d3.scaleBand().domain(topics)
      .range([MARGIN.left, w - MARGIN.right]).paddingInner(0.3).paddingOuter(0.1);
    const yMax = d3.max(topics, t => Math.max(user[t] || 0, popular[t] || 0)) || 1;
    const y = d3.scaleLinear().domain([0, yMax]).nice()
      .range([h - MARGIN.bottom, MARGIN.top]);

    svg.append("g")
      .attr("transform", `translate(${MARGIN.left},0)`)
      .call(d3.axisLeft(y).ticks(3, "~%"))
      .attr("font-size", 8).attr("color", "#8a8880");

    const half = x.bandwidth() / 2;
    cat.groups = svg.append("g").selectAll("g")
      .data(topics).join("g")
      .attr("class", "dimmable")
      .attr("data-topic", t => t);

    cat.groups.append("rect")   // you
      .attr("x", t => x(t))
      .attr("y", t => y(user[t] || 0))
      .attr("width", half)
      .attr("height", t => y(0) - y(user[t] || 0))
      .attr("fill", t => WW.colorFor(t));
    cat.groups.append("rect")   // popular pages baseline
      .attr("x", t => x(t) + half)
      .attr("y", t => y(popular[t] || 0))
      .attr("width", half)
      .attr("height", t => y(0) - y(popular[t] || 0))
      .attr("fill", t => WW.hexToRgba(WW.colorFor(t), 0.45));
    cat.groups.append("text")
      .attr("x", t => x(t) + half)
      .attr("y", h - MARGIN.bottom + 8)
      .attr("transform", t =>
        `rotate(-28, ${x(t) + half}, ${h - MARGIN.bottom + 8})`)
      .attr("text-anchor", "end")
      .attr("font-size", 7.5)
      .attr("fill", "#6b6a64")
      .text(shortLabel);

    const legend = svg.append("g")
      .attr("transform", `translate(${w - MARGIN.right - 92}, ${MARGIN.top})`)
      .attr("font-size", 8).attr("fill", "#8a8880");
    legend.append("rect").attr("width", 8).attr("height", 8)
      .attr("fill", "#8a8880");
    legend.append("text").attr("x", 11).attr("y", 7).text("you");
    legend.append("rect").attr("x", 38).attr("width", 8).attr("height", 8)
      .attr("fill", WW.hexToRgba("#8a8880", 0.45));
    legend.append("text").attr("x", 49).attr("y", 7).text("popular");
  }

  /* -- visits per day ------------------------------------------------------ */
  function renderDaily() {
    const { w, h } = panelSize("panel-daily");
    const svg = d3.select("#panel-daily svg").html(null)
      .attr("viewBox", `0 0 ${w} ${h}`);
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
      .call(d3.axisBottom(x).ticks(4).tickSizeOuter(0))
      .attr("font-size", 8).attr("color", "#8a8880");
    svg.append("g")
      .attr("transform", `translate(${MARGIN.left},0)`)
      .call(d3.axisLeft(y).ticks(3))
      .attr("font-size", 8).attr("color", "#8a8880");

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
    const { w, h } = panelSize("panel-hours");
    const svg = d3.select("#panel-hours svg").html(null)
      .attr("viewBox", `0 0 ${w} ${h}`);
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
      .attr("font-size", 8).attr("color", "#8a8880");
    svg.append("g")
      .attr("transform", `translate(${MARGIN.left},0)`)
      .call(d3.axisLeft(y).ticks(3))
      .attr("font-size", 8).attr("color", "#8a8880");

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
    window.addEventListener("resize", () => {
      clearTimeout(window.WWpanelsResize);
      window.WWpanelsResize = setTimeout(renderAll, 150);
    });
  }

  return { init, highlight };
})();
