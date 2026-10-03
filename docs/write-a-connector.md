# Write a connector

Create a subpackage under `sherd_connectors` that exposes `CONNECTOR`, or register a third-party package through the `sherd.connectors` entry-point group. The registry discovers it automatically; no central list needs editing.

A connector has `id`, `version`, `display_name`, `detect(path)`, `parse(path, context)` and `fixtures()`. `detect` should inspect only enough to identify a format. `parse` streams canonical rows and uses the timezone and identities in `ImportContext`. Keep logs to counts and ids.

Ship a `FORMAT.md` explaining how to export the source and synthetic golden fixtures in the reserved-value ranges. Run the golden harness, then check that `sherd dig` detects the export and the generated [connector index](connectors/index.md) includes it.
