# Vendored JavaScript

Unmodified upstream builds, served from `static/vendor/`. No CDN, no build step.
To upgrade: replace the file from the npm tarball below, update this table, and re-run
`design/preview.html` to check the charts still render.

| File | Package | Version | Source | SHA-256 of the file | Licence |
| --- | --- | --- | --- | --- | --- |
| `vega.min.js` | `vega` | 5.33.1 | https://registry.npmjs.org/vega/-/vega-5.33.1.tgz (`package/build/vega.min.js`) | `463f3db6a40b20e9747b4ed38f37ed0add508838f9141b1cf8366784b07b30c8` | BSD-3-Clause, `LICENSE-vega` |
| `vega-lite.min.js` | `vega-lite` | 5.23.0 | https://registry.npmjs.org/vega-lite/-/vega-lite-5.23.0.tgz (`package/build/vega-lite.min.js`) | `58c27358e26f2d319cf62f45bc17a4c8362f08645001df2ec8d341eee4097c7f` | BSD-3-Clause, `LICENSE-vega-lite` |
| `vega-embed.min.js` | `vega-embed` | 6.29.0 | https://registry.npmjs.org/vega-embed/-/vega-embed-6.29.0.tgz (`package/build/vega-embed.min.js`) | `12d02acfbe3ec59ef9a37dd4822a2e04e2961b5bbb671bbe661d2221715b99da` | BSD-3-Clause, `LICENSE-vega-embed` |
| `vega-interpreter.min.js` | `vega-interpreter` | 1.2.1 | https://registry.npmjs.org/vega-interpreter/-/vega-interpreter-1.2.1.tgz (`package/build/vega-interpreter.min.js`) | `ee191cf04168e0c043214cea7c4e5ef80e908daa0e757c19c8061df5f898cf89` | BSD-3-Clause, `LICENSE-vega` |
| `htmx.min.js` | `htmx.org` | 2.0.11 | https://registry.npmjs.org/htmx.org/-/htmx.org-2.0.11.tgz (`package/dist/htmx.min.js`) | `d6fdc75f204e6bdefa99b69bf1e6d4ac69b8a364f77929f45c13476b4000f717` | 0BSD, `LICENSE-htmx` |
| `alpine-csp.min.js` | `@alpinejs/csp` | 3.17.4 | https://registry.npmjs.org/@alpinejs/csp/-/csp-3.17.4.tgz (`package/dist/cdn.min.js`, renamed) | `0d18d7f8d7910e2e0212f0f056b12f50bebc3abb7d88d2f7c7cb4c336fe4519a` | MIT, `LICENSE-alpine` |

`LICENSE-vega` comes from https://github.com/vega/vega/blob/v5.33.1/LICENSE (the npm package
does not ship one); the other two are the `LICENSE` files inside their tarballs.

Compatibility: vega-lite 5.23 needs vega `^5.24`; vega-embed 6.x is the last line that
supports vega 5 (7.x needs vega 6). Load order: `vega`, `vega-lite`, `vega-embed`.

The files keep their `sourceMappingURL` comments; the `.map` files are not vendored, so
devtools reports a missing source map. That is a local 404, not a network request.

Call `vegaEmbed` with `actions: false`: the default action menu links to the online Vega
editor.

Added by WP-14b (web shell):

- `vega-interpreter` lets Vega run under the app's CSP, which has no `'unsafe-eval'`: call
  `vegaEmbed` with `ast: true, expr: vega.expressionInterpreter`. It is the 1.x
  (`v1-maintenance`) line, built against vega 5.33.1; 2.x needs vega 6. It is part of the
  vega monorepo, so `LICENSE-vega` covers it. Load it after `vega.min.js`, which it extends.
- The app also passes `defaultStyle: false` and `tooltip: {disableDefaultStyle: true}`:
  vega-embed and vega-tooltip otherwise inject `<style>` elements, which the CSP blocks.
  `app.css` carries the tooltip rules instead.
- `htmx` is configured by the `htmx-config` meta tag in `base.html`
  (`includeIndicatorStyles: false`, `allowEval: false`, `allowScriptTags: false`,
  `selfRequestsOnly: true`), so it injects no styles and evaluates nothing.
- `alpine-csp.min.js` is Alpine's CSP build: it never calls `eval`/`Function`, so
  components are registered in `app.js` with `Alpine.data()` and templates only name
  their members. Load it after `app.js`.
- `LICENSE-htmx` is the `LICENSE` inside the htmx tarball. `LICENSE-alpine` comes from
  https://github.com/alpinejs/alpine/blob/v3.17.4/LICENSE.md (the npm package ships none).
- The one URL inside these three files is an Alpine error-message string
  (`https://alpinejs.dev/plugins/…`, shown in the console when a missing plugin is used);
  nothing fetches it. The web tests allow exactly that string.
