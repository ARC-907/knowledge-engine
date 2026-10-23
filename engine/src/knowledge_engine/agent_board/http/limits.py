"""Board HTTP: request-body limits (in-route ``Content-Length`` check + streamed cap)."""

from __future__ import annotations

from fastapi import FastAPI, HTTPException, Request

from ...core.http import BodySizeLimitMiddleware

# Hard ceiling on a single board request body. The per-field caps in
# schemas.validate enforce semantic limits; this is the brute-force
# defence against an attacker streaming megabytes at the FastAPI body
# parser before validation has a chance to fire. Enforced for streamed
# bodies by ``core.http.BodySizeLimitMiddleware`` (installed by both app
# factories); the `Content-Length` check below is the in-route fast path.
MAX_REQUEST_BODY_BYTES = 1_048_576  # 1 MiB


def _check_body_size(req: Request) -> None:
    cl = req.headers.get("content-length")
    if not cl:
        return
    try:
        size = int(cl)
    except (TypeError, ValueError):
        return
    if size > MAX_REQUEST_BODY_BYTES:
        raise HTTPException(
            413,
            f"request body too large (max {MAX_REQUEST_BODY_BYTES} bytes; got {size})",
        )


def install_body_limit(app: FastAPI, prefix: str) -> None:
    """Cap streamed bodies under ``prefix``: only middleware stops a chunked upload."""
    app.add_middleware(
        BodySizeLimitMiddleware, max_bytes=MAX_REQUEST_BODY_BYTES, path_prefix=prefix
    )


__all__ = ["MAX_REQUEST_BODY_BYTES", "install_body_limit"]
