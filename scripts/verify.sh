#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
OPENCLAW_HOME="${OPENCLAW_HOME:-$HOME/.openclaw}"
TARGET_ROOT="$OPENCLAW_HOME/tooling/openclaw-codex-kit"
RUNTIME_DIR="$OPENCLAW_HOME/runtime/cliproxyapi"
CONFIG_PATH="${OPENCLAW_CONFIG_PATH:-$OPENCLAW_HOME/openclaw.json}"

require() {
  local path="$1"
  local label="$2"
  if [[ ! -e "$path" ]]; then
    echo "missing $label: $path" >&2
    exit 1
  fi
}

for bin in python3 openclaw curl; do
  if ! command -v "$bin" >/dev/null 2>&1; then
    echo "missing prerequisite: $bin" >&2
    exit 1
  fi
done

require "$TARGET_ROOT/packages/control-center/scripts/control-center.sh" "installed control-center"
require "$TARGET_ROOT/packages/control-center/scripts/codex-proxy.sh" "installed proxy rail"
require "$TARGET_ROOT/packages/control-center/scripts/codex-rail.sh" "installed codex rail"
require "$HOME/.local/bin/openclaw-codex" "launcher binary"
require "$HOME/Desktop/OpenClaw Codex.command" "desktop launcher"
require "$RUNTIME_DIR/bin/cliproxyapi" "proxy binary"
require "$RUNTIME_DIR/config.yaml" "proxy config"
require "$RUNTIME_DIR/.management_key" "proxy management key"

proxy_values="$(python3 - "$RUNTIME_DIR/config.yaml" <<'PY'
import re
import sys
text = open(sys.argv[1], "r", encoding="utf-8").read()
host = re.search(r'^\s*host:\s*"?([^"\n]+)"?\s*$', text, flags=re.MULTILINE)
port = re.search(r'^\s*port:\s*(\d+)\s*$', text, flags=re.MULTILINE)
key = re.search(r'api-keys:\s*\n\s*-\s*"([^"]+)"', text, flags=re.MULTILINE)
host_value = host.group(1).strip() if host else "127.0.0.1"
if not host_value or host_value in {"0.0.0.0", "::", "[::]"}:
    host_value = "127.0.0.1"
port_value = port.group(1).strip() if port else "8317"
key_value = key.group(1).strip() if key else ""
print(f"http://{host_value}:{port_value}/v1 {key_value}")
PY
)"
PROXY_URL="${proxy_values%% *}"
PROXY_KEY="${proxy_values#* }"

if ! curl -fsS "$PROXY_URL/models" -H "Authorization: Bearer $PROXY_KEY" >/dev/null; then
  echo "proxy health check failed on $PROXY_URL/models" >&2
  exit 1
fi

python3 - "$CONFIG_PATH" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
if not path.exists():
    raise SystemExit(f"missing OpenClaw config: {path}")
doc = json.loads(path.read_text())
providers = (((doc.get("models") or {}).get("providers")) or {})
cliproxy = providers.get("cliproxy-codex") or {}
primary = ((((doc.get("agents") or {}).get("defaults") or {}).get("model")) or {}).get("primary")
if not cliproxy:
    raise SystemExit("cliproxy-codex provider missing from openclaw.json")
if primary != "cliproxy-codex/gpt-5.4":
    raise SystemExit(f"unexpected primary model: {primary!r}")
print("openclaw config: ok")
PY

echo "verify: ok"
