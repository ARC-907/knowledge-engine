"""One slug builder for folder names, project names and scope keys.

The defaults reproduce the registry's historical entry-id rule
(``library-<slug>``): lowercase, keep alphanumerics, turn space, ``-``, ``_``
and ``&`` into ``-``, drop everything else, collapse ``--`` and trim ``-``.
Callers with stricter rules (project-context paths, board scopes) pass
keyword options instead of re-implementing the walk.
"""

from __future__ import annotations

#: Characters that become ``-`` under the default rule.
DEFAULT_SEPARATORS = " -_&"


def slugify(
    name: str,
    *,
    separators: str = DEFAULT_SEPARATORS,
    keep: str = "",
    replace_unknown: bool = False,
    ascii_only: bool = False,
    strip: str = "-",
    max_length: int | None = None,
    fallback: str = "",
) -> str:
    """Return a lowercase slug of ``name``.

    Per character (after lower-casing): characters in ``keep`` and
    alphanumerics (ASCII-only when ``ascii_only``) are kept; characters in
    ``separators`` become ``-``; anything else becomes ``-`` when
    ``replace_unknown`` and is dropped otherwise. Runs of ``-`` collapse to
    one, ``strip`` characters are trimmed from both ends, the result is cut to
    ``max_length`` (and re-trimmed), and an empty result becomes ``fallback``.
    """
    out: list[str] = []
    for ch in name.lower():
        alnum = ch.isascii() and ch.isalnum() if ascii_only else ch.isalnum()
        if ch in keep or alnum:
            out.append(ch)
        elif ch in separators or replace_unknown:
            out.append("-")
    slug = "".join(out)
    while "--" in slug:
        slug = slug.replace("--", "-")
    slug = slug.strip(strip)
    if max_length is not None and len(slug) > max_length:
        slug = slug[:max_length].rstrip(strip)
    return slug or fallback


__all__ = ["DEFAULT_SEPARATORS", "slugify"]
