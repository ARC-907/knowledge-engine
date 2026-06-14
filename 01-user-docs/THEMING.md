# Theming the Dashboard

The Knowledge-Engine dashboard is deliberately a **single HTML file** —
`ui/index.html`. There is no build step, no framework toolchain, no
`node_modules`. That makes it trivial to restyle, fork, or replace.

## How the dashboard is built

`ui/index.html` is one self-contained file:

- **Tailwind CSS** — pulled from a CDN (`cdn.tailwindcss.com`). All styling is
  Tailwind utility classes in the markup.
- **Alpine.js** — pulled from a CDN. A small `dashboard()` component holds the
  state and talks to the engine's HTTP API.
- **No backend coupling** — the dashboard only consumes the engine's HTTP API
  (`/info`, `/registry`, `/search`, `/search/reindex`,
  `/registry/{id}/toggle`). Anything that speaks those endpoints can replace
  it.

The engine serves `ui/` as static files at `/ui/` (see `app.py`). Edit the
file, refresh the browser — that's the whole loop.

## Recolouring (the quick win)

The default theme is dark slate with a sky-blue accent. The colors are Tailwind
classes in the markup. To re-skin, find-and-replace the palette:

| Role | Default classes | Change to (example: emerald accent) |
| --- | --- | --- |
| Page background | `bg-slate-950` | keep, or `bg-zinc-950` |
| Body text | `text-slate-100` | keep |
| Cards / panels | `bg-slate-900`, `border-slate-800` | keep, or `bg-zinc-900` |
| Accent (buttons, focus) | `bg-sky-600`, `hover:bg-sky-500`, `focus:border-sky-500` | `bg-emerald-600`, `hover:bg-emerald-500`, `focus:border-emerald-500` |
| Reindex button | `bg-emerald-700`, `hover:bg-emerald-600` | pick a contrasting color |

Tailwind ships every color in shades `50`–`950`. Swap `sky` for `indigo`,
`violet`, `rose`, `amber`, etc. — the whole palette is available with no config.

## Light mode

The default is dark. For a light theme, flip the base classes:

- `bg-slate-950` → `bg-white`
- `text-slate-100` → `text-slate-900`
- `bg-slate-900` (cards) → `bg-slate-50`
- `border-slate-800` → `border-slate-200`
- adjust the muted-text classes (`text-slate-400/500/600`) toward darker shades

## Layout and content

The markup is plain HTML. You can:

- Change the page title (`<title>` and the `<h1>`).
- Add your own logo or header.
- Reorder the sections (search block, results, registry table).
- Add columns to the registry table — the entry objects already carry
  `description`, `tags`, and (for libraries) `framework`; surface them.

## Removing the CDN dependency (offline dashboard)

The default dashboard fetches Tailwind and Alpine from a CDN, so the **first
load needs internet**. For a fully offline dashboard:

1. Download `alpinejs` and the Tailwind standalone build (or a precompiled
   Tailwind CSS file).
2. Drop them into `ui/` alongside `index.html`.
3. Change the two `<script>`/`<link>` tags from CDN URLs to local paths
   (`./alpine.min.js`, `./tailwind.css`).

Because the engine serves the whole `ui/` directory statically, any local
asset you add there is served at `/ui/<filename>` automatically.

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

Build it however you like and either serve it from `ui/` or host it separately
and point it at the engine's URL. The full route list is in
[ARCHITECTURE.md](../ARCHITECTURE.md).

## A note on the search snippets

Search results come back with `<mark>…</mark>` tags around matched terms — the
default dashboard renders them with Alpine's `x-html`. If you build your own
dashboard, either render the snippet HTML the same way, or strip the tags if
you don't want highlighting. The `<mark>` element is styled by your CSS;
Tailwind gives it a default yellow highlight.
