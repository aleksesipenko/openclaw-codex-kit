#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONTROL_CENTER_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
ROOT="${OPENCLAW_HOME:-$HOME/.openclaw}"
RUNTIME_DIR="$ROOT/runtime/cliproxyapi"
CONFIG="$RUNTIME_DIR/config.yaml"
BIN="$RUNTIME_DIR/bin/cliproxyapi"
PROXY_AUTH_DIR="$RUNTIME_DIR/auths"
SWITCHER_PY="$CONTROL_CENTER_DIR/switcher/codex-accounts.py"
CODEX_RAIL="$SCRIPT_DIR/codex-rail.sh"
IMPORTER_PY="$SCRIPT_DIR/codex-proxy-import.py"
QUOTA_SYNC="$SCRIPT_DIR/codex-proxy-quota-sync.py"
QUOTA_REFRESH="$SCRIPT_DIR/codex-refresh-quota-snapshot.py"

MODE="oauth"

usage() {
  cat <<'EOF'
Usage: codex-proxy-add-account.sh [--mode oauth|device]
EOF
}

require_file() {
  local path="$1"
  local label="$2"
  if [[ ! -e "$path" ]]; then
    echo "missing $label: $path" >&2
    exit 1
  fi
}

parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --mode)
        [[ $# -ge 2 ]] || { usage >&2; exit 1; }
        MODE="$2"
        shift 2
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

  case "$MODE" in
    oauth|device) ;;
    *)
      echo "unsupported mode: $MODE" >&2
      exit 1
      ;;
  esac
}

snapshot_auth_mtimes() {
  python3 - "$PROXY_AUTH_DIR" <<'PY'
import json
import sys
from pathlib import Path

root = Path(sys.argv[1])
payload = {}
for path in root.glob("*.json"):
    try:
        payload[path.name] = path.stat().st_mtime
    except FileNotFoundError:
        continue
print(json.dumps(payload))
PY
}

detect_changed_auth_file() {
  local before_json="$1"
  python3 - "$PROXY_AUTH_DIR" "$before_json" <<'PY'
import json
import sys
from pathlib import Path

root = Path(sys.argv[1])
before = json.loads(sys.argv[2])
candidates = []

for path in root.glob("*.json"):
    try:
        mtime = path.stat().st_mtime
    except FileNotFoundError:
        continue
    previous = before.get(path.name)
    if previous is None or mtime > float(previous) + 1e-6:
        candidates.append((mtime, str(path)))

if candidates:
    candidates.sort()
    print(candidates[-1][1])
    raise SystemExit(0)

fallback = []
for path in root.glob("*.json"):
    try:
        fallback.append((path.stat().st_mtime, str(path)))
    except FileNotFoundError:
        continue

if fallback:
    fallback.sort()
    print(fallback[-1][1])
PY
}

extract_saved_auth_path_from_log() {
  local log_path="$1"
  python3 - "$log_path" <<'PY'
import re
import sys
from pathlib import Path

text = Path(sys.argv[1]).read_text(errors="ignore")
text = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", text)
matches = re.findall(r"Authentication saved to ([^\r\n]+)", text)
if matches:
    print(matches[-1].strip())
PY
}

sync_snapshot_from_proxy_auth() {
  local proxy_auth_path="$1"
  python3 - "$SWITCHER_PY" "$proxy_auth_path" <<'PY'
import importlib.util
import json
import sys
import tempfile
from pathlib import Path

switcher_path = Path(sys.argv[1])
proxy_auth_path = Path(sys.argv[2])

spec = importlib.util.spec_from_file_location("codex_accounts_switcher", switcher_path)
module = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(module)

raw = json.loads(proxy_auth_path.read_text())
converted = {
    "auth_mode": "chatgpt",
    "OPENAI_API_KEY": None,
    "tokens": {
        "id_token": raw.get("id_token"),
        "access_token": raw.get("access_token"),
        "refresh_token": raw.get("refresh_token"),
        "account_id": raw.get("account_id"),
    },
    "last_refresh": raw.get("last_refresh"),
}

module.ensure_dirs()

with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as tmp:
    temp_path = Path(tmp.name)
    json.dump(converted, tmp, indent=2)

try:
    info = module.get_account_info(temp_path) or {}
    email = module._canonical_email(info.get("email"))
    account_id = info.get("account_id")
    if not email:
        raise SystemExit("Could not determine email from new proxy auth")

    canonical_name = module._canonical_snapshot_name(email, account_id, fallback=email)
    canonical_target = module.ACCOUNTS_DIR / f"{canonical_name}.json"
    match = module._resolve_matching_account(email, account_id)
    saved_targets = []

    primary_target = match if match is not None else canonical_target
    success, message = module.safe_save_token(temp_path, primary_target, force=False)
    if not success:
        raise SystemExit(message)
    saved_targets.append(str(primary_target))

    if primary_target != canonical_target:
        success, message = module.safe_save_token(temp_path, canonical_target, force=False)
        if not success and not canonical_target.exists():
            raise SystemExit(message)
        if success:
            saved_targets.append(str(canonical_target))

    module.ensure_canonical_snapshot_files()
    print(json.dumps({
        "email": email,
        "snapshot": str(canonical_target),
        "saved_targets": saved_targets,
    }))
finally:
    try:
        temp_path.unlink(missing_ok=True)
    except Exception:
        pass
PY
}

run_login_command() {
  local log_path="$1"
  shift

  if command -v script >/dev/null 2>&1; then
    script -q "$log_path" "$@"
  else
    "$@" | tee "$log_path"
  fi
}

current_codex_identity() {
  python3 - <<'PY'
import base64
import json
from pathlib import Path

path = Path.home() / ".codex" / "auth.json"
if not path.exists():
    raise SystemExit("unknown::missing-auth")

data = json.loads(path.read_text())
tokens = data.get("tokens") or {}
token = tokens.get("id_token") or tokens.get("access_token") or ""

payload = {}
if isinstance(token, str) and token.count(".") >= 2:
    try:
        body = token.split(".")[1]
        body += "=" * (-len(body) % 4)
        payload = json.loads(base64.urlsafe_b64decode(body.encode()).decode())
    except Exception:
        payload = {}

email = payload.get("email")
profile = payload.get("https://api.openai.com/profile")
if isinstance(profile, dict) and isinstance(profile.get("email"), str):
    email = profile["email"]

account_id = tokens.get("account_id") or "unknown-account"
print(f"{(email or 'unknown').strip().lower()}::{account_id}")
PY
}

import_proxy_pool() {
  require_file "$IMPORTER_PY" "proxy importer"
  python3 "$IMPORTER_PY" --source "$HOME/.codex/accounts" --dest "$PROXY_AUTH_DIR"
}

run_oauth_via_canonical_rail() {
  local auth_label

  require_file "$CODEX_RAIL" "canonical codex rail"
  require_file "$IMPORTER_PY" "proxy importer"

  echo ""
  echo "Starting canonical Codex browser OAuth via codex-rail.sh add."
  echo "This flow updates ~/.codex/auth.json, syncs OpenClaw auth-profiles,"
  echo "and then refreshes the proxy pool from ~/.codex/accounts."
  echo ""

  "$CODEX_RAIL" add
  import_proxy_pool

  if [[ -f "$QUOTA_REFRESH" ]]; then
    python3 "$QUOTA_REFRESH" >/dev/null 2>&1 || true
  fi

  if [[ -f "$QUOTA_SYNC" ]]; then
    python3 "$QUOTA_SYNC" --quiet >/dev/null 2>&1 || true
  fi

  auth_label="$(current_codex_identity)"

  echo ""
  echo "Added Codex account: ${auth_label%%::*}"
  echo "Active local ~/.codex/auth.json is now updated."
  echo "Proxy pool refreshed from ~/.codex/accounts."
}

run_device_login_via_proxy() {
  local before_json log_path proxy_auth_path snapshot_json auth_label

  before_json="$(snapshot_auth_mtimes)"
  log_path="$(mktemp "${TMPDIR:-/tmp}/codex-proxy-login.XXXXXX.log")"

  echo ""
  echo "Starting Codex device auth fallback."
  echo ""
  run_login_command "$log_path" "$BIN" -config "$CONFIG" -codex-device-login

  proxy_auth_path="$(extract_saved_auth_path_from_log "$log_path")"
  if [[ -z "$proxy_auth_path" || ! -f "$proxy_auth_path" ]]; then
    proxy_auth_path="$(detect_changed_auth_file "$before_json")"
  fi
  if [[ -z "$proxy_auth_path" || ! -f "$proxy_auth_path" ]]; then
    echo "login finished but could not locate the saved proxy auth file" >&2
    exit 1
  fi

  snapshot_json="$(sync_snapshot_from_proxy_auth "$proxy_auth_path")"
  auth_label="$(python3 -c 'import json,sys; data=json.load(sys.stdin); print(f"{data.get(\"email\", \"unknown\")}::{data.get(\"snapshot\", \"\")}")' <<<"$snapshot_json")"

  rm -f "$log_path"

  if [[ -f "$QUOTA_SYNC" ]]; then
    python3 "$QUOTA_SYNC" --quiet >/dev/null 2>&1 || true
  fi

  echo ""
  echo "Added Codex account: ${auth_label%%::*}"
  echo "Proxy auth file: $proxy_auth_path"
  echo "Snapshot saved: ${auth_label##*::}"
  echo "Device fallback is proxy-only; ~/.codex/auth.json stays unchanged here."
}

main() {
  parse_args "$@"
  require_file "$PROXY_AUTH_DIR" "proxy auth dir"

  if [[ "$MODE" == "oauth" ]]; then
    run_oauth_via_canonical_rail
    return
  fi

  require_file "$BIN" "cliproxyapi binary"
  require_file "$CONFIG" "cliproxyapi config"
  require_file "$SWITCHER_PY" "codex account switcher"
  run_device_login_via_proxy
}

main "$@"
