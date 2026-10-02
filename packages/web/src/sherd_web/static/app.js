/*
 * sherd web shell behaviour. Loaded with `defer` after the vendored Vega files and
 * htmx, and before Alpine (CSP build), so the components below exist when it starts.
 *
 * The page CSP has no 'unsafe-eval' and no 'unsafe-inline', so:
 *  - Vega parses expressions with vega-interpreter (ast: true) instead of eval;
 *  - vega-embed and its tooltip run without their injected stylesheets (app.css has them);
 *  - chart specs arrive as <script type="application/json"> blocks, never as code.
 */
(() => {
  "use strict";

  const rendered = new WeakSet();
  const MIN_PLOT_WIDTH = 280;

  const chartFailed = (el, error) => {
    el.classList.add("is-error");
    el.removeAttribute("role");
    el.textContent = "This chart could not be drawn. The summary and table below still hold its data.";
    console.error("sherd: chart failed", el.dataset.chart, error);
  };

  /* Charts fill their card's width; the theme and data stay as the server sent them. */
  const renderChart = (el) => {
    if (rendered.has(el)) return;
    rendered.add(el);
    const source = document.getElementById(el.dataset.chart);
    if (!source) return chartFailed(el, new Error("missing spec"));
    let spec;
    try {
      spec = JSON.parse(source.textContent);
    } catch (error) {
      return chartFailed(el, error);
    }
    // Fit the card's width unless that would squeeze the plot (bump charts keep a wide
    // label gutter); then keep the spec's own width and let the frame scroll sideways.
    const pad = spec.padding && typeof spec.padding === "object" ? spec.padding : {};
    const plot = el.clientWidth - (pad.left || 0) - (pad.right || 0);
    if (plot >= MIN_PLOT_WIDTH) {
      spec.width = "container";
      spec.autosize = { type: "fit-x", contains: "padding" };
    } else {
      el.classList.add("is-scroll");
    }
    window
      .vegaEmbed(el, spec, {
        mode: "vega-lite",
        renderer: "svg",
        actions: false,
        ast: true,
        expr: window.vega.expressionInterpreter,
        defaultStyle: false,
        tooltip: { disableDefaultStyle: true },
      })
      .catch((error) => chartFailed(el, error));
  };

  const renderCharts = (root) => {
    const charts = root.querySelectorAll("[data-chart]");
    if (!charts.length) return;
    if (!window.vegaEmbed || !window.vega || !window.vega.expressionInterpreter) {
      charts.forEach((el) => chartFailed(el, new Error("Vega did not load")));
      return;
    }
    charts.forEach(renderChart);
  };

  /* Wait for Fira Sans so Vega measures labels with the real font. */
  const start = () => document.fonts.ready.then(() => renderCharts(document));
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", start, { once: true });
  } else {
    start();
  }

  /* Content swapped in by htmx (WP-15 pages) may carry charts too. */
  document.addEventListener("htmx:afterSettle", (event) => renderCharts(event.target));

  /* Alpine components. The CSP build cannot evaluate inline expressions, so every
     behaviour is a named component and templates only reference its members. */
  document.addEventListener("alpine:init", () => {
    const Alpine = window.Alpine;

    /* A set of buttons that each reveal one command panel. */
    Alpine.data("commands", () => ({
      current: "",
      toggle() {
        const name = this.$el.dataset.command;
        this.current = this.current === name ? "" : name;
      },
      get digOpen() {
        return this.current === "dig";
      },
      get demoOpen() {
        return this.current === "demo";
      },
    }));

    /* Copy a command's text; the button label confirms for a moment. */
    Alpine.data("copyable", () => ({
      label: "",
      timer: 0,
      init() {
        const button = this.$el.querySelector("[data-copy-target]");
        this.label = button ? button.dataset.label : "";
      },
      copy() {
        const button = this.$el;
        const target = document.getElementById(button.dataset.copyTarget);
        if (!target || !navigator.clipboard) return;
        const text = target.textContent.replace(/^\$\s*/, "").trim();
        navigator.clipboard.writeText(text).then(
          () => {
            this.label = button.dataset.done;
            clearTimeout(this.timer);
            this.timer = setTimeout(() => {
              this.label = button.dataset.label;
            }, 1400);
          },
          () => {},
        );
      },
    }));
  });
})();
