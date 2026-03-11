#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
OPENCLAW_HOME="${OPENCLAW_HOME:-$HOME/.openclaw}"
TARGET_ROOT="$OPENCLAW_HOME/tooling/openclaw-codex-kit"
PROXY_SETUP_ARGS=()
NO_CONFIG=0
NO_RESTART=0
NO_VERIFY=0

usage() {
  cat <<'EOF'
Usage: ./scripts/install.sh [--no-config] [--no-restart] [--no-import] [--no-verify]
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --no-config)
      NO_CONFIG=1
      shift
      ;;
    --no-restart)
      NO_RESTART=1
      shift
      ;;
    --no-import)
      PROXY_SETUP_ARGS+=("--no-import")
      shift
      ;;
    --no-verify)
      NO_VERIFY=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "unknown argument: $1" >&2
      usage >&2
      exit 1
      ;;
  esac
done

for bin in python3 git go curl rsync; do
  if ! command -v "$bin" >/dev/null 2>&1; then
    echo "missing prerequisite: $bin" >&2
    exit 1
  fi
done

if ! command -v openclaw >/dev/null 2>&1; then
  echo "missing prerequisite: openclaw" >&2
  echo "install OpenClaw first, then rerun this script" >&2
  exit 1
fi

mkdir -p "$OPENCLAW_HOME/tooling"

rsync -a --delete \
  --exclude='.git' \
  --exclude='node_modules' \
  --exclude='dist' \
  --exclude='__pycache__' \
  --exclude='*.pyc' \
  --exclude='*.bak*' \
  "$REPO_ROOT/packages" \
  "$REPO_ROOT/templates" \
  "$REPO_ROOT/docs" \
  "$REPO_ROOT/scripts" \
  "$TARGET_ROOT/"

chmod +x \
  "$TARGET_ROOT/packages/control-center/scripts/"*.sh \
  "$TARGET_ROOT/packages/control-center/switcher/codex-accounts.py" \
  "$TARGET_ROOT/scripts/"*.sh \
  "$TARGET_ROOT/scripts/"*.py

"$TARGET_ROOT/packages/control-center/scripts/install-launchers.sh"
"$TARGET_ROOT/packages/control-center/scripts/codex-proxy.sh" setup "${PROXY_SETUP_ARGS[@]}"

if [[ "$NO_CONFIG" -eq 0 ]]; then
  python3 "$TARGET_ROOT/scripts/apply-config-fragment.py"
fi

echo "Installed public kit to $TARGET_ROOT"
echo "Proxy runtime lives under $OPENCLAW_HOME/runtime/cliproxyapi"

if [[ "$NO_RESTART" -eq 0 ]]; then
  openclaw gateway restart
fi

if [[ "$NO_VERIFY" -eq 0 ]]; then
  "$TARGET_ROOT/scripts/verify.sh"
fi

if [[ "$NO_CONFIG" -ne 0 || "$NO_RESTART" -ne 0 ]]; then
  echo
  echo "Next step:"
  [[ "$NO_CONFIG" -ne 0 ]] && echo "  python3 \"$TARGET_ROOT/scripts/apply-config-fragment.py\""
  [[ "$NO_RESTART" -ne 0 ]] && echo "  openclaw gateway restart"
fi
