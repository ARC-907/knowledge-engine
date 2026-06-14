#!/usr/bin/env bash
# update-vendor.sh — re-download and SHA384-verify locally vendored
# front-end dependencies for the Knowledge Engine dashboard.
#
# Usage:
#   ./scripts/update-vendor.sh
#
# Idempotent: bump a pinned version below, or run to verify on-disk
# bundles match the upstream artifact.

set -euo pipefail

repo_root="$(cd "$(dirname "$0")/.." && pwd)"
vendor_root="$repo_root/ui/vendor"

# Pinned versions + expected SHA384 (base64) of upstream bundle.
# Format per row: name|version|url|relative_out_path|sha384_base64
assets=(
"tailwindcss|3.4.16|https://cdn.tailwindcss.com/3.4.16|tailwindcss/3.4.16/tailwind.min.js|mS5Uq7sE90lgbBDN8xgf34ibEgbZo4gB3tfLY40ZRle+M188BQw8onzNHg6GUZaA"
"alpinejs|3.14.1|https://cdn.jsdelivr.net/npm/alpinejs@3.14.1/dist/cdn.min.js|alpinejs/3.14.1/alpine.min.js|l8f0VcPi/M1iHPv8egOnY/15TDwqgbOR1anMIJWvU6nLRgZVLTLSaNqi/TOoT5Fh"
)

fail=0
tmpdir="$(mktemp -d)"
trap 'rm -rf "$tmpdir"' EXIT

for row in "${assets[@]}"; do
    IFS='|' read -r name version url out_path expected <<< "$row"
    dest="$vendor_root/$out_path"
    mkdir -p "$(dirname "$dest")"

    echo "[fetch] ${name}@${version}"
    tmpfile="$tmpdir/$(basename "$out_path")"
    curl -fsSL "$url" -o "$tmpfile"

    # macOS uses shasum; Linux uses sha384sum; fall back to openssl.
    if command -v sha384sum >/dev/null 2>&1; then
        actual_hex="$(sha384sum "$tmpfile" | awk '{print $1}')"
    elif command -v shasum >/dev/null 2>&1; then
        actual_hex="$(shasum -a 384 "$tmpfile" | awk '{print $1}')"
    else
        actual_hex="$(openssl dgst -sha384 "$tmpfile" | awk '{print $NF}')"
    fi
    actual_b64="$(printf '%s' "$actual_hex" | xxd -r -p | base64 | tr -d '\n')"

    if [ "$actual_b64" != "$expected" ]; then
        echo "  [FAIL] SHA384 mismatch"
        echo "    expected: sha384-$expected"
        echo "    actual:   sha384-$actual_b64"
        fail=1
        continue
    fi

    mv "$tmpfile" "$dest"
    size="$(wc -c < "$dest" | tr -d ' ')"
    echo "  [ok] ${size} bytes -> $dest"
done

if [ $fail -ne 0 ]; then
    echo
    echo "One or more vendored assets failed SHA384 verification." >&2
    exit 1
fi

echo
echo "Vendor refresh complete."
