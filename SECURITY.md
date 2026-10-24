# Security Policy

## Supported versions

| Version | Supported          |
| ------- | ------------------ |
| 3.x     | Yes                |
| < 3.0   | No — please upgrade |

Security fixes land on the `main` branch and ship in the next 3.x release.

## Reporting a vulnerability

Please **do not** open a public issue for a suspected vulnerability.

1. Use GitHub's private vulnerability reporting for this repository:
   <https://github.com/ARC-907/knowledge-engine/security/advisories/new>
2. If that is unavailable to you, email the address listed on the
   maintainer's GitHub profile (<https://github.com/ARC-907>) with the
   subject line `knowledge-engine security`.

Include the version (the `version` field of `GET /health`), how the
engine is deployed (localhost / LAN / behind a proxy), and reproduction
steps. You should hear back within 7 days; coordinated disclosure after a
fix is the default.

## Trust model

1. The engine is **local-first**: it binds to `127.0.0.1` and assumes the
   machine's users are trusted.
2. Core routes (`/search`, `/registry`, `/generate`) have **no
   authentication**; gate them at a reverse proxy before exposing them.
3. CORS is limited to loopback origins; `KE_CORS_ORIGINS` widens it and `*`
   is honoured only when set explicitly.
4. The agent board trusts **peers** on loopback and the Tailscale CGNAT range
   (`100.64.0.0/10`) by default; everyone else gets `403`.
   `KE_BOARD_TRUSTED_CIDRS` replaces that list; an empty value trusts no
   peer at all, so list `127.0.0.1/32,::1/128` for loopback-only.
5. Trusted peers may read, and post while `require_key_for_post=0`
   (default); set it to `1` to require `X-Board-Key` on non-loopback writes.
   "Loopback" is judged on the effective client address: with
   `KE_TRUST_PROXY=1` that is the forwarded address, not the proxy's.
6. Board **admin** routes (key vault, provider registry, config, sweep,
   scope creation) always require a key with `board:admin`. Keys are
   verified against the shared board's vault, whatever `scope` a request
   names.
7. The first master key is minted once via `POST /board/keys/bootstrap-master`
   from loopback only; the raw key is shown once and never stored.
8. `X-Forwarded-For` is ignored unless `KE_TRUST_PROXY=1` and the socket peer
   is a trusted proxy (`KE_TRUSTED_PROXIES`, default loopback). `serve` and
   `board-serve` configure uvicorn's proxy-header handling from the same two
   variables; uvicorn's `FORWARDED_ALLOW_IPS` is not consulted.
9. Board request bodies are capped at 1 MiB; scope names are validated and
   capped in number (`KE_BOARD_MAX_SCOPES`); reads never create state.
10. Provider API keys live only in your environment; the database stores the
    variable name, never the secret.

## Hardening checklist

- [ ] Keep the engine bound to `127.0.0.1` unless others need it; put a
      reverse proxy with TLS and authentication in front for LAN or internet
      exposure (see [`01-user-docs/DEPLOYMENT.md`](01-user-docs/DEPLOYMENT.md)).
- [ ] **CORS**: leave `KE_CORS_ORIGINS` unset unless a browser UI is served
      from another origin; list that origin explicitly instead of `*`.
- [ ] **Board keys**: run `knowledge-engine board keys bootstrap-master` on
      the host once (inside the container for Docker), copy the key from
      `<data_dir>/board-master-key.txt`, delete the file, then mint per-agent
      keys with the narrowest permissions
      (`board keys create NAME --permission write`).
- [ ] **Peer trust**: set `KE_BOARD_TRUSTED_CIDRS` to exactly the networks
      that may reach the board, loopback included (`127.0.0.1/32,::1/128`
      for loopback only; an empty string trusts nobody). Set
      `require_key_for_post=1` if untrusted machines share those networks.
- [ ] **Reverse proxy**: set `KE_TRUST_PROXY=1` only when a proxy you control
      fronts the engine, and add its address to `KE_TRUSTED_PROXIES` if it is
      not on the same host; never enable it on a directly exposed server.
- [ ] Watch the startup log lines `CORS: allow_origins=...` and
      `board trust: ...` on stderr after every configuration change. They
      are `INFO` lines, emitted by `knowledge-engine serve` / `board-serve`
      as long as `KE_LOG_LEVEL` (default `INFO`) is not raised above that.
- [ ] Keep provider secrets in the environment or a gitignored `.env`; never
      commit them.
