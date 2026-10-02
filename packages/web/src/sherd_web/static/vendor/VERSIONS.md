# Vendored JavaScript

Unmodified upstream builds, served from `static/vendor/`. No CDN, no build step.
To upgrade: replace the file from the npm tarball below, update this table, and re-run
`design/preview.html` to check the charts still render.

| File | Package | Version | Source | SHA-256 of the file | Licence |
| --- | --- | --- | --- | --- | --- |
| `vega.min.js` | `vega` | 5.33.1 | https://registry.npmjs.org/vega/-/vega-5.33.1.tgz (`package/build/vega.min.js`) | `463f3db6a40b20e9747b4ed38f37ed0add508838f9141b1cf8366784b07b30c8` | BSD-3-Clause, `LICENSE-vega` |
| `vega-lite.min.js` | `vega-lite` | 5.23.0 | https://registry.npmjs.org/vega-lite/-/vega-lite-5.23.0.tgz (`package/build/vega-lite.min.js`) | `58c27358e26f2d319cf62f45bc17a4c8362f08645001df2ec8d341eee4097c7f` | BSD-3-Clause, `LICENSE-vega-lite` |
| `vega-embed.min.js` | `vega-embed` | 6.29.0 | https://registry.npmjs.org/vega-embed/-/vega-embed-6.29.0.tgz (`package/build/vega-embed.min.js`) | `12d02acfbe3ec59ef9a37dd4822a2e04e2961b5bbb671bbe661d2221715b99da` | BSD-3-Clause, `LICENSE-vega-embed` |

`LICENSE-vega` comes from https://github.com/vega/vega/blob/v5.33.1/LICENSE (the npm package
does not ship one); the other two are the `LICENSE` files inside their tarballs.

Compatibility: vega-lite 5.23 needs vega `^5.24`; vega-embed 6.x is the last line that
supports vega 5 (7.x needs vega 6). Load order: `vega`, `vega-lite`, `vega-embed`.

The files keep their `sourceMappingURL` comments; the `.map` files are not vendored, so
devtools reports a missing source map. That is a local 404, not a network request.

Call `vegaEmbed` with `actions: false`: the default action menu links to the online Vega
editor.
