#!/usr/bin/env bash
set -euo pipefail

OPENCLAW_HOME="${OPENCLAW_HOME:-$HOME/.openclaw}"
TARGET_ROOT="$OPENCLAW_HOME/tooling/openclaw-codex-kit"
RUNTIME_DIR="$OPENCLAW_HOME/runtime/cliproxyapi"
PURGE_RUNTIME=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --purge-runtime)
      PURGE_RUNTIME=1
      shift
      ;;
    *)
      echo "unknown argument: $1" >&2
      exit 1
      ;;
  esac
done

if [[ -x "$TARGET_ROOT/packages/control-center/scripts/codex-proxy.sh" ]]; then
  "$TARGET_ROOT/packages/control-center/scripts/codex-proxy.sh" stop >/dev/null 2>&1 || true
fi

rm -rf "$TARGET_ROOT"
rm -f "$HOME/.local/bin/openclaw-codex" "$HOME/Desktop/OpenClaw Codex.command"

echo "Removed:"
echo "  - $TARGET_ROOT"
echo "  - $HOME/.local/bin/openclaw-codex"
echo "  - $HOME/Desktop/OpenClaw Codex.command"

if [[ "$PURGE_RUNTIME" -eq 1 ]]; then
  rm -rf "$RUNTIME_DIR"
  echo "  - $RUNTIME_DIR"
else
  echo "Runtime preserved:"
  echo "  - $RUNTIME_DIR"
  echo "Use --purge-runtime if you want to remove proxy auths/config too."
fi
