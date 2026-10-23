#!/usr/bin/env python3
"""Refresh or verify the dashboard's vendored front-end assets.

Standard library only, so it runs with any Python 3.12+ on any platform::

    python scripts/vendor.py          # download pinned assets, verify, write
    python scripts/vendor.py --check  # verify the committed files, no network

Each asset is pinned to an exact upstream URL and the SHA-384 digest of that
artifact (base64, the same form a ``<script integrity>`` attribute uses). A
download whose digest does not match is discarded and the script exits 1.
To bump a version: edit the row in ``ASSETS`` with the new URL and digest,
run the script, commit the result together with the ``index.html`` change.

Tailwind is *not* vendored any more: its CSS is compiled from
``ui-src/input.css`` (see ``01-user-docs/THEMING.md``).
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import sys
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = REPO_ROOT / "engine" / "src" / "knowledge_engine" / "server" / "static"
VENDOR_DIR = STATIC_DIR / "vendor"
DOWNLOAD_TIMEOUT_S = 60


@dataclass(frozen=True)
class Asset:
    name: str
    version: str
    url: str
    path: str  # relative to VENDOR_DIR
    sha384: str  # base64 of the raw SHA-384 digest


ASSETS: tuple[Asset, ...] = (
    Asset(
        name="alpinejs",
        version="3.14.1",
        url="https://cdn.jsdelivr.net/npm/alpinejs@3.14.1/dist/cdn.min.js",
        path="alpinejs/3.14.1/alpine.min.js",
        sha384="l8f0VcPi/M1iHPv8egOnY/15TDwqgbOR1anMIJWvU6nLRgZVLTLSaNqi/TOoT5Fh",
    ),
)


def sha384_b64(data: bytes) -> str:
    return base64.b64encode(hashlib.sha384(data).digest()).decode("ascii")


def download(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "knowledge-engine-vendor/1"})
    with urllib.request.urlopen(req, timeout=DOWNLOAD_TIMEOUT_S) as resp:  # noqa: S310 - pinned https URLs
        return resp.read()


def write_atomic(dest: Path, data: bytes) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=dest.parent, prefix=dest.name + ".", delete=False) as tmp:
        tmp.write(data)
        tmp_path = Path(tmp.name)
    tmp_path.replace(dest)


def check_asset(asset: Asset, vendor_dir: Path) -> bool:
    dest = vendor_dir / asset.path
    if not dest.is_file():
        print(f"[missing] {asset.name}@{asset.version}: {dest}")
        return False
    actual = sha384_b64(dest.read_bytes())
    if actual != asset.sha384:
        print(f"[FAIL] {asset.name}@{asset.version}: SHA-384 mismatch")
        print(f"    expected: sha384-{asset.sha384}")
        print(f"    actual:   sha384-{actual}")
        return False
    print(f"[ok] {asset.name}@{asset.version} ({dest.stat().st_size} bytes)")
    return True


def fetch_asset(asset: Asset, vendor_dir: Path) -> bool:
    print(f"[fetch] {asset.name}@{asset.version} <- {asset.url}")
    try:
        data = download(asset.url)
    except (urllib.error.URLError, OSError) as exc:
        print(f"[FAIL] {asset.name}@{asset.version}: download failed: {exc}")
        return False
    actual = sha384_b64(data)
    if actual != asset.sha384:
        print(f"[FAIL] {asset.name}@{asset.version}: SHA-384 mismatch, file not written")
        print(f"    expected: sha384-{asset.sha384}")
        print(f"    actual:   sha384-{actual}")
        return False
    dest = vendor_dir / asset.path
    write_atomic(dest, data)
    print(f"[ok] {len(data)} bytes -> {dest}")
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--check",
        action="store_true",
        help="verify the committed files against the pinned digests without downloading",
    )
    parser.add_argument(
        "--vendor-dir",
        type=Path,
        default=VENDOR_DIR,
        help=f"where vendored assets live (default: {VENDOR_DIR})",
    )
    parser.add_argument(
        "--only",
        metavar="NAME",
        action="append",
        help="limit to the named asset(s); may be repeated",
    )
    args = parser.parse_args(argv)

    assets = [a for a in ASSETS if not args.only or a.name in args.only]
    if not assets:
        known = ", ".join(a.name for a in ASSETS)
        print(f"no such asset; known: {known}", file=sys.stderr)
        return 2

    action = check_asset if args.check else fetch_asset
    ok = all([action(asset, args.vendor_dir) for asset in assets])  # noqa: C419 - run every asset
    if not ok:
        print("\nOne or more vendored assets failed verification.", file=sys.stderr)
        return 1
    print("\nVendored assets verified." if args.check else "\nVendor refresh complete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
