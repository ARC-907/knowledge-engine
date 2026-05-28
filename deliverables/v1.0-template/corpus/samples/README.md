# Samples

Tiny synthetic examples demonstrating the structure the engine expects.

- `demo-library/` — a minimal library with two topic folders and `README.md` / `CATALOG.md`.
- `demo-skill/` — a minimal skill package (`SKILL.md` + one helper file).
- `assets/` — placeholder for binary assets (images, diagrams) referenced by content.

Use these as templates when authoring new content. Once a folder is registered in
`corpus/registry.json` (or auto-registered by the watcher), the indexer will pick it
up on the next index run.
