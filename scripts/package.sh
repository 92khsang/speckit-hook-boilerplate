#!/usr/bin/env bash
# Build the copyable archive in dist/.
set -euo pipefail
SOURCE_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
OUT="$SOURCE_DIR/dist/speckit-hook-boilerplate.zip"
STAGE=$(mktemp -d "${TMPDIR:-/tmp}/speckit-pkg.XXXXXX")
trap 'rm -rf "$STAGE"' EXIT

# The archive mirrors the repository layout, so scripts/install.sh works unchanged
# whether it is run from a checkout or from an unpacked archive.
PKG="$STAGE/speckit-hook-boilerplate"
mkdir -p "$PKG/template" "$PKG/scripts" "$PKG/docs"
cp -R "$SOURCE_DIR/template/." "$PKG/template/"
cp "$SOURCE_DIR/scripts/install.sh" "$SOURCE_DIR/scripts/probe-codex.sh" "$PKG/scripts/"
cp "$SOURCE_DIR/README.md" "$PKG/"
cp "$SOURCE_DIR/docs/guarantees.md" "$SOURCE_DIR/docs/migration.md" "$PKG/docs/"
find "$STAGE" -name '__pycache__' -type d -prune -exec rm -rf {} +
chmod +x "$PKG/template/.speckit-hooks/speckit-hook" "$PKG/scripts/install.sh" \
         "$PKG/scripts/probe-codex.sh"

mkdir -p "$SOURCE_DIR/dist"
rm -f "$OUT"
( cd "$STAGE" && zip -qr "$OUT" speckit-hook-boilerplate )
echo "Wrote $OUT"
unzip -l "$OUT" | tail -n +4 | head -30
