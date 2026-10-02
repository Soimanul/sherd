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
    // "container" width only works for single and layered views; facets and concats
    // keep their own width.
    const enc = spec.encoding || {};
    const composite = Boolean(
      spec.facet || spec.repeat || spec.concat || spec.hconcat || spec.vconcat || enc.row || enc.column || enc.facet,
    );
    if (plot >= MIN_PLOT_WIDTH && !composite) {
      spec.width = "container";
      spec.autosize = { type: "fit-x", contains: "padding" };
    } else {
      el.classList.add("is-scroll");
      el.tabIndex = 0; // a scrolling region must be reachable by keyboard
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

/*
 * Pages (WP-15): charts in HTMX swaps, and focus. When HTMX swaps in an Ask answer or
 * consent card, focus moves to its heading so keyboard and screen-reader users land on what
 * just arrived. Filter swaps keep focus where it was (HTMX restores it to the control by id).
 */
(() => {
  "use strict";

  /* htmx runs with allowScriptTags: false, so it drops every <script> from swapped HTML,
     including the inert JSON blocks that hold chart specs. Read them from the response
     before the swap and put them back, still inert, before the shell draws charts. */
  let pendingSpecs = [];

  document.addEventListener("htmx:beforeSwap", (event) => {
    pendingSpecs = [];
    const text = event.detail.serverResponse;
    if (!event.detail.shouldSwap || typeof text !== "string" || !text.includes("application/json")) {
      return;
    }
    const parsed = new DOMParser().parseFromString(text, "text/html");
    parsed.querySelectorAll('script[type="application/json"][id]').forEach((block) => {
      pendingSpecs.push({ id: block.id, json: block.textContent });
    });
  });

  document.addEventListener("htmx:afterSwap", () => {
    pendingSpecs.forEach(({ id, json }) => {
      const chart = document.querySelector(`[data-chart="${CSS.escape(id)}"]`);
      if (!chart || document.getElementById(id)) return;
      const block = document.createElement("script");
      block.type = "application/json";
      block.id = id;
      block.textContent = json;
      chart.after(block);
    });
    pendingSpecs = [];
  });

  document.addEventListener("htmx:afterSettle", (event) => {
    const root = event.target;
    if (!(root instanceof Element)) return;
    const heading = root.matches("[data-focus]") ? root : root.querySelector("[data-focus]");
    if (heading) heading.focus({ preventScroll: false });
  });
})();
