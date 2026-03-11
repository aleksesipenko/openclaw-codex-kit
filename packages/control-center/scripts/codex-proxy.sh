#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
KIT_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
OPENCLAW_HOME="${OPENCLAW_HOME:-$HOME/.openclaw}"
RUNTIME_DIR="$OPENCLAW_HOME/runtime/cliproxyapi"
CONFIG="$RUNTIME_DIR/config.yaml"
BIN="$RUNTIME_DIR/bin/cliproxyapi"
AUTH_DIR="$RUNTIME_DIR/auths"
LOG_DIR="$RUNTIME_DIR/logs"
STATE_DIR="$RUNTIME_DIR/state"
PID_FILE="$STATE_DIR/cliproxyapi.pid"
MGMT_KEY_FILE="$RUNTIME_DIR/.management_key"
CONFIG_TEMPLATE="$KIT_ROOT/templates/cliproxyapi.config.yaml"
IMPORTER="$SCRIPT_DIR/codex-proxy-import.py"
SRC_DIR="${CLIPROXYAPI_SRC_DIR:-$OPENCLAW_HOME/tooling/openclaw-codex-kit/.cache/cliproxyapi-src}"
PLIST="$HOME/Library/LaunchAgents/com.openclaw.codex-proxy.plist"
LABEL="com.openclaw.codex-proxy"
HOST="${CLIPROXYAPI_HOST:-127.0.0.1}"
PORT="${CLIPROXYAPI_PORT:-8317}"
UPSTREAM_PROXY_URL="${CLIPROXYAPI_UPSTREAM_PROXY_URL:-}"
CLIPROXYAPI_REPO="${CLIPROXYAPI_REPO:-https://github.com/router-for-me/CLIProxyAPI.git}"

require_file() {
  local path="$1"
  local label="$2"
  if [[ ! -e "$path" ]]; then
    echo "missing $label: $path" >&2
    exit 1
  fi
}

ensure_layout() {
  mkdir -p "$RUNTIME_DIR/bin" "$AUTH_DIR" "$LOG_DIR" "$STATE_DIR" "$(dirname "$PLIST")" "$(dirname "$SRC_DIR")"
}

generate_secret() {
  python3 - <<'PY'
import secrets
print(secrets.token_urlsafe(24))
PY
}

config_api_key() {
  [[ -f "$CONFIG" ]] || return 1
  python3 - "$CONFIG" <<'PY'
import re
import sys
text = open(sys.argv[1], "r", encoding="utf-8").read()
match = re.search(r'api-keys:\s*\n\s*-\s*"([^"]+)"', text)
if match:
    print(match.group(1))
PY
}

config_value() {
  local key="$1"
  [[ -f "$CONFIG" ]] || return 1
  python3 - "$CONFIG" "$key" <<'PY'
import re
import sys

text = open(sys.argv[1], "r", encoding="utf-8").read()
key = sys.argv[2]
patterns = {
    "host": r'^\s*host:\s*"?([^"\n]+)"?\s*$',
    "port": r"^\s*port:\s*(\d+)\s*$",
    "proxy-url": r'^\s*proxy-url:\s*"?([^"\n]*)"?\s*$',
}
pattern = patterns.get(key)
if not pattern:
    raise SystemExit(1)
match = re.search(pattern, text, flags=re.MULTILINE)
if match:
    print(match.group(1))
PY
}

mgmt_key() {
  [[ -f "$MGMT_KEY_FILE" ]] || return 1
  tr -d '\n' <"$MGMT_KEY_FILE"
}

base_url() {
  local host
  local port
  host="$(config_value host 2>/dev/null || true)"
  port="$(config_value port 2>/dev/null || true)"
  host="${host:-$HOST}"
  port="${port:-$PORT}"
  if [[ -z "$host" || "$host" == "0.0.0.0" || "$host" == "::" || "$host" == "[::]" ]]; then
    host="127.0.0.1"
  fi
  printf 'http://%s:%s' "$host" "$port"
}

write_config() {
  ensure_layout
  require_file "$CONFIG_TEMPLATE" "CLIProxyAPI config template"

  local api_key
  local management_key
  local host
  local port
  local upstream_proxy_url

  api_key="${CLIPROXYAPI_KEY:-$(config_api_key 2>/dev/null || true)}"
  [[ -n "$api_key" ]] || api_key="$(generate_secret)"

  management_key="${CLIPROXYAPI_MANAGEMENT_KEY:-$(mgmt_key 2>/dev/null || true)}"
  [[ -n "$management_key" ]] || management_key="$(generate_secret)"

  host="${CLIPROXYAPI_HOST:-$(config_value host 2>/dev/null || true)}"
  port="${CLIPROXYAPI_PORT:-$(config_value port 2>/dev/null || true)}"
  upstream_proxy_url="${CLIPROXYAPI_UPSTREAM_PROXY_URL:-$(config_value proxy-url 2>/dev/null || true)}"

  host="${host:-$HOST}"
  port="${port:-$PORT}"
  upstream_proxy_url="${upstream_proxy_url:-$UPSTREAM_PROXY_URL}"

  python3 - "$CONFIG_TEMPLATE" "$CONFIG" "$host" "$port" "$AUTH_DIR" "$api_key" "$management_key" "$upstream_proxy_url" <<'PY'
import sys
from pathlib import Path

template_path = Path(sys.argv[1])
out_path = Path(sys.argv[2])
replacements = {
    "__HOST__": sys.argv[3],
    "__PORT__": sys.argv[4],
    "__AUTH_DIR__": sys.argv[5],
    "__API_KEY__": sys.argv[6],
    "__MANAGEMENT_KEY__": sys.argv[7],
    "__UPSTREAM_PROXY_URL__": sys.argv[8],
}
text = template_path.read_text(encoding="utf-8")
for old, new in replacements.items():
    text = text.replace(old, new.replace("\\", "\\\\"))
out_path.write_text(text, encoding="utf-8")
PY

  printf '%s\n' "$management_key" >"$MGMT_KEY_FILE"
  chmod 600 "$CONFIG" "$MGMT_KEY_FILE"
}

build_binary() {
  ensure_layout
  for bin in git go; do
    if ! command -v "$bin" >/dev/null 2>&1; then
      echo "missing prerequisite: $bin" >&2
      exit 1
    fi
  done

  if [[ ! -d "$SRC_DIR/.git" ]]; then
    rm -rf "$SRC_DIR"
    git clone --depth 1 "$CLIPROXYAPI_REPO" "$SRC_DIR"
  else
    git -C "$SRC_DIR" fetch --depth 1 origin
    git -C "$SRC_DIR" reset --hard origin/HEAD
  fi

  (
    cd "$SRC_DIR"
    GOTOOLCHAIN=auto go build -o "$BIN" ./cmd/server
  )
  chmod +x "$BIN"
}

write_plist() {
  cat >"$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$BIN</string>
    <string>-config</string>
    <string>$CONFIG</string>
  </array>
  <key>WorkingDirectory</key>
  <string>$RUNTIME_DIR</string>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>$LOG_DIR/service.out.log</string>
  <key>StandardErrorPath</key>
  <string>$LOG_DIR/service.err.log</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>HOME</key>
    <string>$HOME</string>
    <key>PATH</key>
    <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$HOME/.local/bin</string>
  </dict>
</dict>
</plist>
EOF
}

stop_background_process() {
  if [[ -f "$PID_FILE" ]]; then
    local pid
    pid="$(<"$PID_FILE")"
    if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
      kill "$pid" >/dev/null 2>&1 || true
      for _ in {1..10}; do
        if ! kill -0 "$pid" 2>/dev/null; then
          break
        fi
        sleep 1
      done
      if kill -0 "$pid" 2>/dev/null; then
        kill -9 "$pid" >/dev/null 2>&1 || true
      fi
    fi
    rm -f "$PID_FILE"
  fi
}

start_service() {
  ensure_layout
  require_file "$BIN" "CLIProxyAPI binary"
  require_file "$CONFIG" "CLIProxyAPI config"

  if command -v launchctl >/dev/null 2>&1; then
    write_plist
    launchctl bootout "gui/$UID/$LABEL" >/dev/null 2>&1 || true
    if launchctl bootstrap "gui/$UID" "$PLIST" >/dev/null 2>&1; then
      launchctl kickstart -k "gui/$UID/$LABEL" >/dev/null 2>&1 || true
      return 0
    fi
  fi

  stop_background_process
  nohup "$BIN" -config "$CONFIG" >>"$LOG_DIR/service.out.log" 2>>"$LOG_DIR/service.err.log" &
  printf '%s\n' "$!" >"$PID_FILE"
}

stop_service() {
  if command -v launchctl >/dev/null 2>&1; then
    launchctl bootout "gui/$UID/$LABEL" >/dev/null 2>&1 || true
  else
    stop_background_process
  fi
}

proxy_health() {
  local key
  key="$(config_api_key)"
  curl -fsS -H "Authorization: Bearer $key" "$(base_url)/v1/models"
}

wait_for_health() {
  for _ in {1..30}; do
    if proxy_health >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  return 1
}

service_state() {
  if command -v launchctl >/dev/null 2>&1; then
    if launchctl print "gui/$UID/$LABEL" >/dev/null 2>&1; then
      echo "running"
    else
      echo "stopped"
    fi
    return 0
  fi

  if [[ -f "$PID_FILE" ]]; then
    local pid
    pid="$(<"$PID_FILE")"
    if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
      echo "running"
      return 0
    fi
  fi
  echo "stopped"
}

show_status() {
  echo "base_url: $(base_url)/v1"
  echo "service_state: $(service_state)"
  echo "runtime_dir: $RUNTIME_DIR"
  echo "auth_dir: $AUTH_DIR"
  if proxy_health >/tmp/cliproxy-status.$$ 2>/tmp/cliproxy-status.err.$$; then
    echo "proxy_health: ok"
    python3 - /tmp/cliproxy-status.$$ <<'PY'
import json
import sys
data = json.load(open(sys.argv[1], "r", encoding="utf-8"))
models = [item.get("id") for item in data.get("data", []) if isinstance(item, dict)]
print("models:", ", ".join(models[:8]))
PY
  else
    echo "proxy_health: failed"
    cat /tmp/cliproxy-status.err.$$ >&2 || true
  fi
  rm -f /tmp/cliproxy-status.$$ /tmp/cliproxy-status.err.$$
}

import_auths() {
  require_file "$IMPORTER" "proxy importer"
  if [[ ! -d "${CODEX_ACCOUNTS_DIR:-$HOME/.codex/accounts}" ]]; then
    echo "source account pool not found: ${CODEX_ACCOUNTS_DIR:-$HOME/.codex/accounts}" >&2
    return 1
  fi
  python3 "$IMPORTER" --source "${CODEX_ACCOUNTS_DIR:-$HOME/.codex/accounts}" --dest "$AUTH_DIR"
}

panel_url() {
  printf '%s/management.html\n' "$(base_url)"
}

open_panel() {
  local url
  url="$(panel_url)"
  if command -v open >/dev/null 2>&1; then
    open "$url"
  elif command -v xdg-open >/dev/null 2>&1; then
    xdg-open "$url"
  else
    echo "$url"
  fi
}

setup() {
  local do_import=1
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --no-import)
        do_import=0
        shift
        ;;
      *)
        echo "unknown argument for setup: $1" >&2
        exit 1
        ;;
    esac
  done

  build_binary
  write_config
  start_service
  if ! wait_for_health; then
    echo "proxy did not become healthy in time" >&2
    exit 1
  fi

  if [[ "$do_import" -eq 1 ]] && [[ -d "${CODEX_ACCOUNTS_DIR:-$HOME/.codex/accounts}" ]]; then
    import_auths || true
  fi
}

usage() {
  cat <<'EOF'
Usage: codex-proxy.sh [setup|build|start|restart|stop|status|service-state|import|panel-url|open-panel]
EOF
}

cmd="${1:-status}"
shift || true

case "$cmd" in
  setup) setup "$@" ;;
  build) build_binary ;;
  start|restart) start_service; wait_for_health || true ;;
  stop) stop_service ;;
  status) show_status ;;
  service-state) service_state ;;
  import) import_auths ;;
  panel-url) panel_url ;;
  open-panel) open_panel ;;
  *)
    usage >&2
    exit 2
    ;;
esac
