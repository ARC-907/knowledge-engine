# ADR 0003 — Agent board authentication model

- **Status:** Accepted (3.0.0)
- **Date:** 2026-10

## Context

The agent board is a coordination bus for agents on one machine or one
private mesh. Up to 2.x it relied on a peer-trust gate (loopback plus the
Tailscale CGNAT range) and an optional `require_key_for_post` flag. Under the
default configuration, admin routes (key vault, provider bindings, config,
sweep) were reachable without any key from every trusted peer, `X-Forwarded-For`
handling trusted the leftmost address, scope names were slugified rather than
validated, and reads of an unknown scope created a database on disk.

## Decision

Three gates, evaluated as route dependencies before the request body is read:

1. **Trust** — the effective client address must be in the trusted CIDR list
   (default loopback + `100.64.0.0/10`; `KE_BOARD_TRUSTED_CIDRS` replaces it).
   Untrusted peers get 403 for everything. `X-Forwarded-For` is honoured only
   with `KE_TRUST_PROXY=1` and only from `KE_TRUSTED_PROXIES` (default
   loopback), walking the chain from the right; `serve`/`board-serve` derive
   uvicorn's proxy settings from the same two variables.
2. **Write** — keyless for trusted peers while `require_key_for_post=0`. With
   the flag on, non-loopback peers need an `X-Board-Key` with `board:write`;
   loopback stays keyless but a presented key must be valid. The MCP post
   tools apply the same rule (`board_key` argument).
3. **Admin** — every admin route requires a key with `board:admin`, from
   loopback too, regardless of `require_key_for_post`. 401 without a key, 403
   without the permission.

Keys live in the shared board's vault only; the gates run before a request is
routed to a scope, so one admin key administers every scope. The first master
key is minted once by `POST /board/keys/bootstrap-master`, which judges the raw
socket peer (loopback only), refuses `X-Forwarded-For`, answers 409 afterwards
and writes the key to `<KE_DATA_DIR>/board-master-key.txt` with mode 0600.
Scope names must match `^[a-z0-9][a-z0-9_-]{0,63}$`; reads never create a
scope; creation is explicit (`PUT /board/scopes/{name}`, admin) or implicit
on the first post, capped by `KE_BOARD_MAX_SCOPES`. Request bodies are capped
at 1 MiB, and secrets (key hashes, provider credentials) never appear in
responses or logs.

Engine CORS defaults to loopback origins with an explicit method/header list;
`KE_CORS_ORIGINS` widens it and `*` is honoured only when written explicitly.

## Consequences

- Breaking for 2.x operators: admin calls need a key (the dashboard's Config
  tab stores one in localStorage and sends it as `X-Board-Key`), legacy scope
  files with invalid names must be renamed, browser UIs on other origins need
  `KE_CORS_ORIGINS`, and a same-host TLS-terminating proxy needs
  `KE_TRUST_PROXY=1`.
- The core routes (`/search`, `/registry`, `/generate`) remain local-trust by
  design; authentication for them is a reverse-proxy concern
  (`01-user-docs/DEPLOYMENT.md`).
- `KE_BOARD_TRUSTED_CIDRS=""` means "no trusted peers", not "loopback only";
  loopback-only is `127.0.0.1/32,::1/128`.
- The effective startup policy is logged once (`CORS: allow_origins=...`,
  `board trust: ...`) so misconfiguration is visible.
