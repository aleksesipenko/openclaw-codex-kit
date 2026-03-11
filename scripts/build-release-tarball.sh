#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
DEFAULT_OUTPUT="${TMPDIR:-/tmp}/openclaw-codex-kit-${STAMP}.tar.gz"
OUTPUT_PATH="${1:-$DEFAULT_OUTPUT}"

for bin in python3 tar; do
  if ! command -v "$bin" >/dev/null 2>&1; then
    echo "missing prerequisite: $bin" >&2
    exit 1
  fi
done

python3 "$REPO_ROOT/scripts/audit-public-safety.py"

tar -czf "$OUTPUT_PATH" \
  -C "$REPO_ROOT" \
  .gitignore \
  LICENSE \
  README.md \
  docs \
  packages \
  scripts \
  templates

echo "release tarball: $OUTPUT_PATH"
