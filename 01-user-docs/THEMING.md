# Theming the Dashboard

The Knowledge-Engine dashboard is three small files under
`engine/src/knowledge_engine/server/static/`, served by the engine at `/ui/`:

| File | What it is |
| --- | --- |
| `index.html` | The markup. Plain HTML with Tailwind utility classes and Alpine.js directives. |
| `app.js` | The Alpine component: state, API calls, tabs, theme toggle. Classic script, no bundler. |
| `tailwind.css` | **Compiled** Tailwind CSS (≈18 KB). Generated from `ui-src/input.css`; committed. |
| `vendor/alpinejs/<ver>/alpine.min.js` | Alpine.js, vendored and SHA-384-pinned by `scripts/vendor.py`. |

Nothing is fetched from a CDN, and the published Python package never needs
Node: the compiled stylesheet ships inside the wheel. Node is only needed by
people who change the markup or the theme and rebuild the CSS.

## The edit → rebuild loop

Tailwind only emits the classes it finds in `index.html` and `app.js`. So:

1. Edit `index.html` / `app.js` (or the tokens in `ui-src/input.css`).
2. Rebuild the stylesheet:

   ```bash
   just ui-css
   # or, without just:
   npm --prefix ui-src install
   npm --prefix ui-src run build:css
   ```

   `npm --prefix ui-src run watch:css` rebuilds on every save while you work.
3. Refresh the browser (the engine serves the `static/` directory as-is).
4. Commit the regenerated `tailwind.css` together with your markup change.

Requirements: Node 22+ and npm. `ui-src/package.json` pins Tailwind v4 and
`@tailwindcss/cli` as dev dependencies; `ui-src/node_modules/` is gitignored.

If you add a class to the markup and forget to rebuild, the class simply has
no effect — nothing breaks, it just does not style anything.

## Light and dark

The dashboard follows the operating system's colour scheme by default and has
a toggle in the header. The choice is stored in `localStorage` (`ke-theme`)
and applied as `data-theme="light|dark"` on `<html>` before the first paint.

In the CSS, `dark:` variants key off that attribute (not `prefers-color-scheme`
directly), so the toggle and the OS setting cannot disagree.

## Recolouring

Surfaces and text use **semantic tokens** defined in `ui-src/input.css`, one
block for light and one for dark:

| Token | Utility classes | Light default | Dark default |
| --- | --- | --- | --- |
| `--ke-canvas` | `bg-canvas` | white | slate-950 |
| `--ke-panel` / `--ke-panel-2` | `bg-panel`, `bg-panel-2` | slate-50 / slate-100 | slate-900 / slate-800 |
| `--ke-edge` / `--ke-edge-soft` | `border-edge`, `border-edge-soft` | slate-200 / slate-100 | slate-800 / slate-900 |
| `--ke-fg` / `--ke-muted` / `--ke-faint` | `text-fg`, `text-muted`, `text-faint` | slate-900 / 600 / 500 | slate-100 / 400 / 500 |
| `--ke-accent` | focus ring colour | sky-600 | sky-400 |

To re-skin, change those two blocks (any Tailwind palette variable such as
`var(--color-zinc-900)` or a raw colour works) and rebuild. Accent colours on
buttons and badges (`bg-sky-600`, `bg-emerald-700`, `bg-violet-700`, …) are
ordinary Tailwind classes in `index.html` with `dark:` counterparts where the
contrast differs; find-and-replace `sky` with `indigo`, `emerald`, `rose`, etc.
and rebuild.

## Layout and content

The markup is plain HTML. You can:

- Change the page title (`<title>` and the `<h1>`).
- Add your own logo or header.
- Reorder the sections or tabs (the tab list lives in `app.js`: `tabs`).
- Add columns to the registry table — the entry objects already carry
  `description`, `tags`, and (for libraries) `framework`; surface them.

Keep the accessibility scaffolding when you do: the tab strip is a proper
`role="tablist"` with arrow-key navigation, every form control has a label
(visible or `sr-only`), and the theme toggle has an `aria-label`.

## Behind a path prefix

API calls in `app.js` are resolved relative to the page
(`new URL('..', location.href)`), so mounting the engine under a prefix such as
`https://host/ke/` keeps the dashboard at `https://host/ke/ui/` working with no
configuration.

## Building a different dashboard entirely

The dashboard is just a client of the HTTP API. If you'd rather build a React,
Vue, Svelte, or plain-JS dashboard, you can — point it at the same endpoints:

| Endpoint | Use |
| --- | --- |
| `GET /info` | Corpus counts, providers, lifecycle state |
| `GET /registry` | List entries (`?kind=`, `?enabled_only=`) |
| `GET /search?q=` | Search (returns ranked hits with `<mark>` snippets) |
| `POST /search/reindex` | Rebuild the index |
| `PATCH /registry/{id}/toggle` | Enable/disable an entry |
| `/board/*` | Agent Board; admin actions need an `X-Board-Key` header |

Build it however you like and either serve it from `server/static/` or host it
separately and point it at the engine's URL. The full route list is in
[ARCHITECTURE.md](../02-dev-docs/ARCHITECTURE.md).

## A note on the search snippets

Search results come back with `<mark>…</mark>` tags around matched terms. The
default dashboard renders snippets with Alpine's `x-text` (never `x-html`),
because indexed markdown can contain arbitrary user-authored HTML; the tags
therefore show as literal text. If you build your own dashboard and want
highlighting, sanitise the snippet before rendering it as HTML.
