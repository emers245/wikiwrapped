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
   needs no redraw at all.

   The panels are also the controls. Clicking a category bar isolates that
   topic, a two-grip range slider under the daily chart picks a span of
   dates, and clicking an hour bar picks an hour of the day; each can be
   swept automatically with the play button beside it. The selections are
   published on the WW filter bus and applied by the map and the graph -
   the panels never filter themselves, so the track a grip runs along
   cannot move out from under the grip while it is being dragged. */

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
  const CONTROL_FONT = 8.5;

  // Bottom margins hold the axis plus, on the time panels, the slider and
  // the transport row; each is measured against the layout further down.
  // The daily panel's top margin holds its legend, as the categories one
  // does; its height grew by the same 12 units, so the plot area itself is
  // unchanged.
  const DAILY_MARGIN = { top: 18, right: 12, bottom: 60, left: 40 };
  const HOURS_MARGIN = { top: 6, right: 12, bottom: 46, left: 40 };
  // Horizontal bars: the left margin holds topic labels, not an axis.
  const CAT_MARGIN = { top: 16, right: 10, bottom: 22, left: 110 };

  // Logical drawing size. The width is shared so all three panels use one
  // horizontal scale; the heights set each panel's aspect ratio, and the
  // categories panel is taller because it has one row per macro topic.
  const LOGICAL_W = 320;
  const LOGICAL_H = { categories: 250, daily: 208, hours: 172 };

  const DAY_MS = 86400000;
  const SPEEDS = [0.5, 1, 2, 4];
  const DEFAULT_SPEED = 1;        // index into SPEEDS
  const DAILY_SWEEP_S = 20;       // seconds for one pass of the whole range
  const HOURS_STEP_S = 0.8;       // seconds per hour
  const GRIP_R = 4.5;
  const LABEL_SPLAY_PX = 62;      // grip labels below this gap splay apart

  const fmtDay = d3.timeFormat("%b %-d %y");

  let hoverTopic = null;          // transient, set by the highlight bus

  function panelSvg(id, logicalHeight) {
    return d3.select(`#${id} svg`).html(null)
      .attr("viewBox", `0 0 ${LOGICAL_W} ${logicalHeight}`)
      .attr("preserveAspectRatio", "xMidYMid meet");
  }

  function clamp(value, low, high) {
    return Math.min(Math.max(value, low), high);
  }

  /* Nearest local midnight. Built from calendar fields rather than by
     rounding the epoch value, so the days either side of a DST change are
     23 and 25 hours long, as they actually are. */
  function snapDay(ms) {
    const d = new Date(ms);
    const floor = new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime();
    const next = new Date(d.getFullYear(), d.getMonth(), d.getDate() + 1).getTime();
    return (ms - floor) < (next - ms) ? floor : next;
  }

  function hourLabel(hour) {
    return `${hour}:00 - ${hour + 1}:00`;
  }

  /* Hand-written abbreviations for the macro topics too long to fit the
     label gutter. The slice below is the fallback for anything new that
     turns up in the reference sample; a <title> carries the full name. */
  const SHORT_LABELS = {
    "History and Society": "History/Society",
    "Philosophy and religion": "Phil. & religion",
  };

  function shortLabel(topic) {
    const label = WW.topicLabel(topic);
    if (SHORT_LABELS[label]) return SHORT_LABELS[label];
    if (label.length > 17) return label.slice(0, 16) + "...";
    return label;
  }

  /* -- transport controls -------------------------------------------------- */
  /* A play/pause button and a speed multiplier, drawn in SVG so they scale
     with the panel like every other mark here. A cycling multiplier rather
     than a third slider: the row has about 14 logical units of height to
     work with, and two grips are already competing for that space.

     `controller` supplies the live state and the actions; refresh()
     repaints the glyph and the labels after either changes. */
  function sweepControls(parent, left, top, controller) {
    const H = 13;
    const PLAY_PATH = "M7,3 L14,6.5 L7,10 Z";
    const PAUSE_PATH = "M7.4,3 h2.2 v7 h-2.2 Z M11.4,3 h2.2 v7 h-2.2 Z";

    const group = parent.append("g");

    const play = group.append("g")
      .attr("class", "ctl")
      .attr("transform", `translate(${left},${top})`)
      .on("click", () => { controller.toggle(); refresh(); });
    play.append("rect").attr("width", 21).attr("height", H).attr("rx", 3);
    const glyph = play.append("path").attr("class", "ctl-glyph");
    play.append("title").text("Play / pause the sweep");

    const speed = group.append("g")
      .attr("class", "ctl")
      .attr("transform", `translate(${left + 25},${top})`)
      .on("click", () => { controller.cycleSpeed(); refresh(); });
    speed.append("rect").attr("width", 24).attr("height", H).attr("rx", 3);
    const speedText = speed.append("text")
      .attr("x", 12).attr("y", H / 2).attr("dy", "0.34em")
      .attr("text-anchor", "middle").attr("font-size", CONTROL_FONT);
    speed.append("title").text("Sweep speed");

    const readout = group.append("text")
      .attr("x", left + 55).attr("y", top + H / 2).attr("dy", "0.34em")
      .attr("font-size", CONTROL_FONT).attr("fill", "#8a8880");

    function refresh() {
      glyph.attr("d", controller.playing() ? PAUSE_PATH : PLAY_PATH);
      play.classed("on", controller.playing());
      speedText.text(SPEEDS[controller.speedIndex()] + "x");
      readout.text(controller.readout());
    }
    refresh();
    return refresh;
  }

  /* -- categories ---------------------------------------------------------- */
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

    // Clicking anywhere that is not a bar row drops the topic selection.
    svg.append("rect")
      .attr("width", w).attr("height", h).attr("fill", "transparent")
      .on("click", () => selectTopic(null));

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
      .attr("class", "dimmable cat-row")
      .attr("data-topic", t => t)
      .on("click", (event, topic) => {
        event.stopPropagation();     // do not reach the clear-all backdrop
        selectTopic(WW.filters.topic === topic ? null : topic);
      });

    /* A full-width transparent target per row. The bars themselves are a
       poor one: a topic at two percent of the reading is a three-pixel
       sliver, and that is exactly the row worth clicking. */
    cat.groups.append("rect")
      .attr("class", "cat-hit")
      .attr("x", 2)
      .attr("y", t => y(t) - y.step() * 0.14)
      .attr("width", w - CAT_MARGIN.right - 2)
      .attr("height", y.step());

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
      .text(t => WW.topicLabel(t));

    // Outline marking the selected row; hidden until there is one.
    cat.groups.append("rect")
      .attr("class", "cat-selected")
      .attr("x", 2.5)
      .attr("y", t => y(t) - y.step() * 0.14 + 0.5)
      .attr("width", w - CAT_MARGIN.right - 3)
      .attr("height", y.step() - 1)
      .attr("rx", 3);

    const legend = svg.append("g")
      .attr("transform", `translate(${CAT_MARGIN.left}, 1)`)
      .attr("font-size", LEGEND_FONT).attr("fill", "#8a8880");
    legend.append("rect").attr("width", 9).attr("height", 9)
      .attr("fill", "#8a8880");
    legend.append("text").attr("x", 13).attr("y", 8).text("you");
    legend.append("rect").attr("x", 44).attr("width", 9).attr("height", 9)
      .attr("fill", WW.hexToRgba("#8a8880", 0.45));
    legend.append("text").attr("x", 57).attr("y", 8).text("popular");

    paintCategories();
  }

  function selectTopic(topic) {
    WW.setFilter({ topic: topic });
  }

  /* Hover and selection both want to say "this row, not the others", so
     they are resolved in one place. Hover wins while it is active, since
     it is answering a question the pointer is asking right now. */
  function paintCategories() {
    if (!cat.groups) return;
    const selected = WW.filters.topic;
    cat.groups.attr("opacity", function () {
      const topic = this.getAttribute("data-topic");
      if (hoverTopic !== null) return topic === hoverTopic ? 1 : 0.2;
      if (selected !== null) return topic === selected ? 1 : 0.35;
      return 1;
    });
    cat.groups.select(".cat-selected").attr("display", function () {
      return this.parentNode.getAttribute("data-topic") === selected
        ? null : "none";
    });
  }

  /* -- visits per day ------------------------------------------------------ */
  function renderDaily() {
    const w = LOGICAL_W;
    const h = LOGICAL_H.daily;
    const M = DAILY_MARGIN;
    const svg = panelSvg("panel-daily", h);
    daily.svg = svg;
    daily.ready = false;

    const days = state.data.stats.daily.days.map(d => {
      const [yy, mm, dd] = d.date.split("-").map(Number);
      return { ...d, dateObj: new Date(yy, mm - 1, dd) };
    });
    if (!days.length) return;

    /* The domain runs one day past the last day plotted, so that a
       selection covering the whole of that final day can be expressed:
       the range is half-open, [start, end). The end grip is therefore
       labelled with the last day it includes, not with its own position. */
    const t0 = days[0].dateObj.getTime();
    const t1 = days[days.length - 1].dateObj.getTime() + DAY_MS;

    const x = d3.scaleTime().domain([t0, t1])
      .range([M.left, w - M.right]);
    const y = d3.scaleLinear()
      .domain([0, d3.max(days, d => d.visits) || 1]).nice()
      .range([h - M.bottom, M.top]);
    daily.x = x;
    daily.plotTop = M.top;
    daily.plotBottom = h - M.bottom;
    daily.span = [t0, t1];
    daily.range = [t0, t1];
    daily.playing = false;
    daily.speedIndex = DEFAULT_SPEED;
    daily.raf = null;
    daily.ready = true;

    // Behind the series, so the line and the average still read on top.
    daily.band = svg.append("rect")
      .attr("class", "range-band")
      .attr("y", M.top)
      .attr("height", (h - M.bottom) - M.top);

    svg.append("g")
      .attr("transform", `translate(0,${h - M.bottom})`)
      .call(d3.axisBottom(x).ticks(3).tickSizeOuter(0))
      .attr("font-size", AXIS_FONT).attr("color", "#8a8880");
    svg.append("g")
      .attr("transform", `translate(${M.left},0)`)
      .call(d3.axisLeft(y).ticks(3))
      .attr("font-size", AXIS_FONT).attr("color", "#8a8880");

    /* One definition per series, shared by the line and by its legend
       swatch below, so the key cannot drift out of step with the chart. */
    const raw = { stroke: WW.hexToRgba("#7295b8", 0.4), width: 1 };
    const avg = { stroke: "#7295b8", width: 1.8 };

    const content = svg.append("g").attr("class", "dimmable");
    daily.content = content;
    content.append("path")
      .datum(days)
      .attr("fill", "none")
      .attr("stroke", raw.stroke)
      .attr("stroke-width", raw.width)
      .attr("d", d3.line()
        .x(d => x(d.dateObj)).y(d => y(d.visits)));
    content.append("path")
      .datum(days.filter(d => d.rolling_avg !== null))
      .attr("fill", "none")
      .attr("stroke", avg.stroke)
      .attr("stroke-width", avg.width)
      .attr("d", d3.line()
        .x(d => x(d.dateObj)).y(d => y(d.rolling_avg)));

    /* Line swatches rather than the filled squares the categories panel
       uses: these two series are told apart by weight as much as by
       shade, and a square shows neither. The window length is read from
       the payload rather than written as "7", since the server decides
       it (stats.daily_series rolling_window). */
    const avgWindow = state.data.stats.daily.rolling_window;
    const legend = svg.append("g")
      .attr("transform", `translate(${M.left}, 1)`)
      .attr("font-size", LEGEND_FONT).attr("fill", "#8a8880");
    [
      { series: raw, label: "visits", at: 0 },
      { series: avg, label: `${avgWindow}-day average`, at: 62 },
    ].forEach(entry => {
      legend.append("line")
        .attr("x1", entry.at).attr("x2", entry.at + 14)
        .attr("y1", 6).attr("y2", 6)
        .attr("stroke", entry.series.stroke)
        .attr("stroke-width", entry.series.width);
      legend.append("text")
        .attr("x", entry.at + 18).attr("y", 6).attr("dy", "0.32em")
        .text(entry.label);
    });

    daily.markerLayer = svg.append("g");

    /* Slider row. Laid out against LOGICAL_H.daily = 208:
           1  legend row, clear of the plot at 18
         148  plot floor and axis line
         160  axis tick labels clear
         169  slider track and grips
         181  grip date labels
         187  transport row, 13 high, clearing the panel at 200. */
    const trackY = h - M.bottom + 21;
    const slider = svg.append("g").attr("class", "range-slider");

    slider.append("line")
      .attr("class", "slider-track")
      .attr("x1", x(t0)).attr("x2", x(t1))
      .attr("y1", trackY).attr("y2", trackY);
    daily.trackFill = slider.append("line")
      .attr("class", "slider-fill")
      .attr("y1", trackY).attr("y2", trackY);

    /* Dragging the filled span moves the whole window without resizing
       it, which is the same gesture the play button automates. */
    daily.trackFill.call(d3.drag()
      .on("start", event => { daily.dragFrom = event.x; })
      .on("drag", event => {
        const width = daily.range[1] - daily.range[0];
        const shift = x.invert(event.x) - x.invert(daily.dragFrom);
        daily.dragFrom = event.x;
        let start = clamp(daily.range[0] + shift, t0, t1 - width);
        daily.range = [start, start + width];
        drawDailyRange();
        publishDaily();
      })
      .on("end", () => {
        daily.range = [snapDay(daily.range[0]), snapDay(daily.range[1])];
        drawDailyRange();
        publishDaily();
      }));

    daily.grips = [0, 1].map(index => {
      const grip = slider.append("circle")
        .attr("class", "slider-grip")
        .attr("r", GRIP_R)
        .attr("cy", trackY)
        .attr("tabindex", 0)
        .attr("role", "slider")
        .attr("aria-label", index === 0 ? "Range start date" : "Range end date");
      grip.append("title")
        .text(index === 0 ? "Drag to set the first day"
                          : "Drag to set the last day");
      grip.call(d3.drag().on("drag", event => moveGrip(index, x.invert(event.x))));
      grip.on("keydown", event => {
        const step = event.key === "ArrowLeft" ? -1
          : event.key === "ArrowRight" ? 1 : 0;
        if (!step) return;
        event.preventDefault();
        moveGrip(index, daily.range[index] + step * DAY_MS);
      });
      return grip;
    });

    daily.gripLabels = [0, 1].map(() =>
      slider.append("text")
        .attr("class", "grip-label")
        .attr("y", trackY + 12)
        .attr("font-size", CONTROL_FONT));

    // Double-clicking the track is the way back to the whole range.
    slider.on("dblclick", () => {
      daily.range = [t0, t1];
      drawDailyRange();
      publishDaily();
    });

    daily.refreshControls = sweepControls(svg, M.left, h - M.bottom + 39, {
      playing: () => daily.playing,
      speedIndex: () => daily.speedIndex,
      toggle: () => setDailyPlaying(!daily.playing),
      cycleSpeed: () => {
        daily.speedIndex = (daily.speedIndex + 1) % SPEEDS.length;
      },
      readout: () => {
        const width = daily.range[1] - daily.range[0];
        if (width >= daily.span[1] - daily.span[0]) return "all dates";
        return Math.max(1, Math.round(width / DAY_MS)) + " day window";
      },
    });

    drawDailyRange();
  }

  /* Snap while dragging, not only on release: a date range that lands
     between two midnights reads as an off-by-one against the daily marks
     it is drawn over. The sweep is the exception and moves continuously. */
  function moveGrip(index, value) {
    const [t0, t1] = daily.span;
    const snapped = clamp(snapDay(value), t0, t1);
    const range = daily.range.slice();
    // Clamp against the other grip rather than swapping them: a range
    // that inverts mid-drag makes the filled band flicker inside out.
    if (index === 0) range[0] = Math.min(snapped, range[1] - DAY_MS);
    else range[1] = Math.max(snapped, range[0] + DAY_MS);
    daily.range = [clamp(range[0], t0, t1), clamp(range[1], t0, t1)];
    drawDailyRange();
    publishDaily();
  }

  function drawDailyRange() {
    if (!daily.ready) return;
    const x = daily.x;
    const [start, end] = daily.range;
    const px0 = x(start);
    const px1 = x(end);
    /* A band over the whole plot is not a selection, it is the absence of
       one; shading everything just makes the panel look filtered when it
       is not. The grips still sit at the ends, which is enough to show
       what dragging them would do. */
    const whole = start <= daily.span[0] && end >= daily.span[1];
    daily.band
      .attr("x", px0)
      .attr("width", whole ? 0 : Math.max(px1 - px0, 0));
    daily.trackFill.attr("x1", px0).attr("x2", px1);
    daily.grips[0].attr("cx", px0);
    daily.grips[1].attr("cx", px1);

    /* The end grip names the last day it includes, so a full selection
       reads as the last day with data rather than the day after it. */
    const texts = [fmtDay(new Date(start)), fmtDay(new Date(end - 1))];
    const splayed = (px1 - px0) < LABEL_SPLAY_PX;
    placeGripLabel(daily.gripLabels[0], px0, splayed ? "end" : "middle",
                   splayed ? -3 : 0, texts[0]);
    placeGripLabel(daily.gripLabels[1], px1, splayed ? "start" : "middle",
                   splayed ? 3 : 0, texts[1]);
    if (daily.refreshControls) daily.refreshControls();
  }

  /* Position a grip label, then pull it back inside the drawing area if it
     hangs over an edge. At the ends of the track a centred label is half
     outside the viewBox, and the SVG does not clip - it just loses the
     overhang, which is how a date silently becomes "Aug 9 2". */
  function placeGripLabel(label, px, anchor, offset, text) {
    label.attr("text-anchor", anchor).attr("x", px + offset).text(text);
    const box = label.node().getBBox();
    const overLeft = 1 - box.x;
    const overRight = (box.x + box.width) - (LOGICAL_W - 1);
    if (overLeft > 0) label.attr("x", px + offset + overLeft);
    else if (overRight > 0) label.attr("x", px + offset - overRight);
  }

  /* A range covering everything is published as null rather than as its
     own bounds, so the common case skips the per-page scan entirely. */
  function publishDaily() {
    const [start, end] = daily.range;
    const full = start <= daily.span[0] && end >= daily.span[1];
    WW.setFilter({ dayRange: full ? null : [start, end] });
  }

  function setDailyPlaying(playing) {
    daily.playing = playing;
    if (daily.raf !== null) cancelAnimationFrame(daily.raf);
    daily.raf = null;
    daily.lastFrame = null;
    if (playing) daily.raf = requestAnimationFrame(dailyTick);
    if (daily.refreshControls) daily.refreshControls();
  }

  function dailyTick(now) {
    if (!daily.playing) return;
    const elapsed = daily.lastFrame === null ? 0 : (now - daily.lastFrame) / 1000;
    daily.lastFrame = now;

    const [t0, t1] = daily.span;
    const span = t1 - t0;
    const width = daily.range[1] - daily.range[0];
    // A window as wide as the data has nowhere to sweep to; let it sit.
    if (width < span) {
      const step = elapsed * (span / DAILY_SWEEP_S) * SPEEDS[daily.speedIndex];
      let start = daily.range[0] + step;
      if (start + width > t1) start = t0;   // end hit the last day: wrap around
      daily.range = [start, start + width];
      drawDailyRange();
      publishDaily();
    }
    daily.raf = requestAnimationFrame(dailyTick);
  }

  /* -- time of day --------------------------------------------------------- */
  function renderHours() {
    const w = LOGICAL_W;
    const h = LOGICAL_H.hours;
    const M = HOURS_MARGIN;
    const svg = panelSvg("panel-hours", h);
    hours.svg = svg;

    const grid = state.data.stats.hour_grid.counts;   // 7 x 24
    const perHour = d3.range(24).map(hour =>
      d3.sum(grid, dayRow => dayRow[hour]));

    const x = d3.scaleLinear().domain([0, 24])
      .range([M.left, w - M.right]);
    const y = d3.scaleLinear().domain([0, d3.max(perHour) || 1]).nice()
      .range([h - M.bottom, M.top]);
    hours.x = x;
    hours.plotTop = M.top;
    hours.plotBottom = h - M.bottom;
    hours.playing = false;
    hours.speedIndex = DEFAULT_SPEED;
    hours.raf = null;

    svg.append("rect")
      .attr("width", w).attr("height", h).attr("fill", "transparent")
      .on("click", () => selectHour(null));

    svg.append("g")
      .attr("transform", `translate(0,${h - M.bottom})`)
      .call(d3.axisBottom(x).tickValues([0, 6, 12, 18, 24])
        .tickFormat(v => `${v}:00`).tickSizeOuter(0))
      .attr("font-size", AXIS_FONT).attr("color", "#8a8880");
    svg.append("g")
      .attr("transform", `translate(${M.left},0)`)
      .call(d3.axisLeft(y).ticks(3))
      .attr("font-size", AXIS_FONT).attr("color", "#8a8880");

    const barWidth = (x(1) - x(0)) * 0.8;
    hours.content = svg.append("g").attr("class", "dimmable");

    /* One hour is picked at a time rather than a range, which is what
       makes the wrap from 23:00 back to 00:00 meaningful: the day is a
       cycle, and a two-ended range over it cannot cross midnight. */
    hours.bars = hours.content.selectAll("g")
      .data(d3.range(24)).join("g")
      .attr("class", "hour-bar")
      .on("click", (event, hour) => {
        event.stopPropagation();
        selectHour(WW.filters.hour === hour ? null : hour);
      });
    // Full-height hit target: the empty hours are the ones worth clicking
    // to confirm they are empty.
    hours.bars.append("rect")
      .attr("class", "hour-hit")
      .attr("x", hour => x(hour))
      .attr("y", M.top)
      .attr("width", x(1) - x(0))
      .attr("height", (h - M.bottom) - M.top);
    hours.bars.append("rect")
      .attr("class", "hour-fill")
      .attr("x", hour => x(hour) + (x(1) - x(0)) * 0.1)
      .attr("y", hour => y(perHour[hour]))
      .attr("width", barWidth)
      .attr("height", hour => y(0) - y(perHour[hour]));
    hours.bars.append("title")
      .text(hour => `${hourLabel(hour)} - ${perHour[hour]} visits`);

    hours.markerLayer = svg.append("g");

    hours.refreshControls = sweepControls(svg, M.left, h - M.bottom + 19, {
      playing: () => hours.playing,
      speedIndex: () => hours.speedIndex,
      toggle: () => setHoursPlaying(!hours.playing),
      cycleSpeed: () => {
        hours.speedIndex = (hours.speedIndex + 1) % SPEEDS.length;
      },
      readout: () => WW.filters.hour === null
        ? "all hours" : hourLabel(WW.filters.hour),
    });

    paintHours();
  }

  function selectHour(hour) {
    WW.setFilter({ hour: hour });
  }

  function paintHours() {
    if (!hours.bars) return;
    const selected = WW.filters.hour;
    hours.bars.select(".hour-fill").attr("fill", hour =>
      selected === null || selected === hour
        ? WW.hexToRgba("#7295b8", 0.75)
        : WW.hexToRgba(WW.GREY, 0.85));
    if (hours.refreshControls) hours.refreshControls();
  }

  function setHoursPlaying(playing) {
    hours.playing = playing;
    if (hours.raf !== null) cancelAnimationFrame(hours.raf);
    hours.raf = null;
    hours.lastStep = null;
    if (playing) hours.raf = requestAnimationFrame(hoursTick);
    if (hours.refreshControls) hours.refreshControls();
  }

  function hoursTick(now) {
    if (!hours.playing) return;
    if (hours.lastStep === null) hours.lastStep = now;
    const interval = HOURS_STEP_S * 1000 / SPEEDS[hours.speedIndex];
    if (now - hours.lastStep >= interval) {
      hours.lastStep = now;
      const current = WW.filters.hour;
      selectHour(current === null ? 0 : (current + 1) % 24);
    }
    hours.raf = requestAnimationFrame(hoursTick);
  }

  /* -- hover linking ------------------------------------------------------- */
  function highlight(page) {
    hoverTopic = page ? (page.macro_topic || "Unclassified") : null;
    paintCategories();

    for (const panel of [daily, hours]) {
      if (!panel.svg || !panel.content) continue;
      panel.content.attr("opacity", page ? 0.25 : 1);
      panel.markerLayer.selectAll("*").remove();
    }
    if (!page) return;
    const stamps = page._stamps;      // parsed once in main.js, local time
    if (daily.ready) {
      daily.markerLayer.selectAll("line")
        .data(stamps).join("line")
        .attr("class", "marker")
        .attr("x1", d => daily.x(
          new Date(d.getFullYear(), d.getMonth(), d.getDate())))
        .attr("x2", d => daily.x(
          new Date(d.getFullYear(), d.getMonth(), d.getDate())))
        .attr("y1", daily.plotTop)
        .attr("y2", daily.plotBottom);
    }
    if (hours.svg) {
      hours.markerLayer.selectAll("line")
        .data(stamps).join("line")
        .attr("class", "marker")
        .attr("x1", d => hours.x(d.getHours() + d.getMinutes() / 60))
        .attr("x2", d => hours.x(d.getHours() + d.getMinutes() / 60))
        .attr("y1", hours.plotTop)
        .attr("y2", hours.plotBottom);
    }
  }

  /* -- filter bus ---------------------------------------------------------- */
  /* Repaint the panel chrome from the filter state. Called after every
     setFilter, so a selection cleared from anywhere - Escape, another
     panel - is reflected here without that caller knowing what we draw. */
  function syncFilters() {
    paintCategories();
    paintHours();
  }

  function clearAll() {
    if (daily.playing) setDailyPlaying(false);
    if (hours.playing) setHoursPlaying(false);
    if (daily.ready) {
      daily.range = daily.span.slice();
      drawDailyRange();
    }
    WW.setFilter({ topic: null, dayRange: null, hour: null });
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

  return { init, highlight, relayout, syncFilters, clearAll };
})();
