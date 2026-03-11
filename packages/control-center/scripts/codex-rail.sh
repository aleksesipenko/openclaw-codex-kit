#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONTROL_CENTER_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
OPENCLAW_HOME="${OPENCLAW_HOME:-$HOME/.openclaw}"

SWITCHER_DIR="${CODEX_SWITCHER_DIR:-$CONTROL_CENTER_DIR/switcher}"
SWITCHER_PY="$SWITCHER_DIR/codex-accounts.py"
IMPORTER="${CODEX_IMPORTER:-$SCRIPT_DIR/codex-import-from-cli.sh}"
AUTH_CONVERGER="${CODEX_AUTH_CONVERGER:-$SCRIPT_DIR/openclaw-auth-converge.py}"
LOCK_ROOT="${OPENCLAW_HOME}/tmp/locks"
LOCK_DIR="$LOCK_ROOT/codex-oauth.lock"
LOCK_WAIT_SECONDS="${CODEX_LOCK_WAIT_SECONDS:-180}"
SYNC_AGENTS="${CODEX_RAIL_SYNC_AGENTS:-main}"

usage() {
  cat <<'EOF'
codex-rail.sh — canonical Codex account rail (switcher + OpenClaw sync)

Usage:
  codex-rail.sh list [--verbose|--json]
  codex-rail.sh add [--name NAME] [--no-sync]
  codex-rail.sh use <account-name> [--no-sync]
  codex-rail.sh auto [--json] [--no-sync]
  codex-rail.sh sync [profile-id] [agent|all]
  codex-rail.sh verify

Notes:
- add/use/auto mutate ~/.codex/auth.json via codex-account-switcher skill.
- By default they immediately sync into OpenClaw auth-profiles for `main`.
- Use --no-sync only for manual debugging.
EOF
}

require_paths() {
  if [[ ! -f "$SWITCHER_PY" ]]; then
    echo "missing switcher script: $SWITCHER_PY" >&2
    echo "hint: clone/install skill into $SWITCHER_DIR" >&2
    exit 1
  fi
  if [[ ! -x "$IMPORTER" ]]; then
    echo "missing importer script: $IMPORTER" >&2
    exit 1
  fi
  if [[ ! -f "$AUTH_CONVERGER" ]]; then
    echo "missing auth converger: $AUTH_CONVERGER" >&2
    exit 1
  fi
}

acquire_codex_lock() {
  if [[ "${CODEX_OAUTH_LOCK_HELD:-0}" == "1" ]]; then
    return 0
  fi

  mkdir -p "$LOCK_ROOT"
  local started_at
  started_at="$(date +%s)"

  while ! mkdir "$LOCK_DIR" 2>/dev/null; do
    local owner=""
    if [[ -f "$LOCK_DIR/pid" ]]; then
      owner="$(<"$LOCK_DIR/pid")"
    fi
    if [[ -n "$owner" ]] && ! kill -0 "$owner" 2>/dev/null; then
      rm -rf "$LOCK_DIR"
      continue
    fi
    if (( "$(date +%s)" - started_at >= LOCK_WAIT_SECONDS )); then
      echo "codex oauth lock busy: $LOCK_DIR" >&2
      exit 1
    fi
    sleep 1
  done

  printf '%s\n' "$$" > "$LOCK_DIR/pid"
  export CODEX_OAUTH_LOCK_HELD=1
  export CODEX_OAUTH_LOCK_OWNED=1
  trap '[[ "${CODEX_OAUTH_LOCK_OWNED:-0}" == "1" ]] && rm -rf "$LOCK_DIR"' EXIT
}

sync_all_agents() {
  "$IMPORTER" "" "$SYNC_AGENTS"
  python3 "$AUTH_CONVERGER" $SYNC_AGENTS
  if command -v openclaw >/dev/null 2>&1; then
    openclaw secrets reload >/dev/null 2>&1 || true
  fi
}

verify_sync() {
  python3 - "$SYNC_AGENTS" <<'PY'
import base64
import json
import sys
from pathlib import Path

agents = [value for value in sys.argv[1].split() if value]
root = Path.home() / ".openclaw" / "agents"
codex_auth = Path.home() / ".codex" / "auth.json"

def decode_jwt_payload(token: str | None) -> dict:
    if not isinstance(token, str) or token.count(".") < 2:
        return {}
    try:
        payload_b64 = token.split(".")[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)
        return json.loads(base64.urlsafe_b64decode(payload_b64.encode()).decode())
    except Exception:
        return {}


def canonical_email(value: str | None) -> str | None:
    if not isinstance(value, str):
        return None
    email = value.strip().lower()
    if not email or "@" not in email:
        return None
    return email


def profile_email(profile: dict) -> str | None:
    if not isinstance(profile, dict):
        return None
    direct = canonical_email(profile.get("email"))
    if direct:
        return direct
    payload = decode_jwt_payload(profile.get("access"))
    direct = canonical_email(payload.get("email"))
    if direct:
        return direct
    nested = payload.get("https://api.openai.com/profile")
    if isinstance(nested, dict):
        return canonical_email(nested.get("email"))
    return None


def profile_account_id(profile: dict) -> str | None:
    if not isinstance(profile, dict):
        return None
    value = profile.get("accountId")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


current_email = None
current_account_id = None
if codex_auth.exists():
    try:
        data = json.loads(codex_auth.read_text())
        tokens = data.get("tokens") or {}
        current_email = profile_email({"email": None, "access": tokens.get("id_token") or tokens.get("access_token")})
        current_account_id = tokens.get("account_id")
    except Exception:
        current_email = None
        current_account_id = None

print("current_codex_email:", current_email or "unknown")
print("current_codex_account_id:", current_account_id or "unknown")
ok = True
for a in agents:
    p = root / a / "agent" / "auth-profiles.json"
    if not p.exists():
        print(f"[{a}] missing auth-profiles.json")
        ok = False
        continue
    doc = json.loads(p.read_text())
    profiles = (doc.get("profiles") or {})
    codex_profiles = {k: v for k, v in profiles.items() if k.startswith("openai-codex:")}
    legacy = [k for k in codex_profiles if k.startswith("openai-codex:acc-")]
    matches = []
    for k, v in codex_profiles.items():
        email_match = profile_email(v or {}) == current_email
        account_match = profile_account_id(v or {}) == current_account_id if current_account_id else False
        if current_account_id:
            if email_match and account_match:
                matches.append(k)
        elif email_match:
            matches.append(k)
    canonical = [k for k in codex_profiles if "@" in k]
    shared = "yes" if a != "main" else "n/a"
    print(
        f"[{a}] local_codex_profiles={len(codex_profiles)} canonical={len(canonical)} "
        f"legacy={len(legacy)} current_identity_matches={len(matches)} shared_via_main={shared}"
    )
    if a == "main":
        if current_email and len(matches) != 1:
            ok = False
        if len(canonical) == 0:
            ok = False
    else:
        if codex_profiles:
            ok = False
    if legacy:
        ok = False

if not ok:
    raise SystemExit(1)
PY
}

main() {
  require_paths
  acquire_codex_lock

  local cmd="${1:-list}"
  shift || true

  case "$cmd" in
    list)
      python3 "$SWITCHER_PY" list "$@"
      ;;

    add)
      local sync=1
      local pass=()
      while [[ $# -gt 0 ]]; do
        case "$1" in
          --no-sync)
            sync=0
            shift
            ;;
          *)
            pass+=("$1")
            shift
            ;;
        esac
      done
      if [[ ${#pass[@]} -gt 0 ]]; then
        python3 "$SWITCHER_PY" add "${pass[@]}"
      else
        python3 "$SWITCHER_PY" add
      fi
      [[ "$sync" -eq 1 ]] && sync_all_agents
      ;;

    use)
      local name="${1:-}"
      if [[ -z "$name" ]]; then
        echo "usage: codex-rail.sh use <account-name> [--no-sync]" >&2
        exit 2
      fi
      shift || true
      local sync=1
      while [[ $# -gt 0 ]]; do
        case "$1" in
          --no-sync)
            sync=0
            ;;
          *)
            echo "unknown arg for use: $1" >&2
            exit 2
            ;;
        esac
        shift
      done
      python3 "$SWITCHER_PY" use "$name"
      [[ "$sync" -eq 1 ]] && sync_all_agents
      ;;

    auto)
      local sync=1
      local pass=()
      while [[ $# -gt 0 ]]; do
        case "$1" in
          --no-sync)
            sync=0
            ;;
          *)
            pass+=("$1")
            ;;
        esac
        shift
      done
      if [[ ${#pass[@]} -gt 0 ]]; then
        python3 "$SWITCHER_PY" auto "${pass[@]}"
      else
        python3 "$SWITCHER_PY" auto
      fi
      [[ "$sync" -eq 1 ]] && sync_all_agents
      ;;

    sync)
      local profile_id="${1:-}"
      local target="${2:-$SYNC_AGENTS}"
      "$IMPORTER" "$profile_id" "$target"
      python3 "$AUTH_CONVERGER" $target
      if command -v openclaw >/dev/null 2>&1; then
        openclaw secrets reload >/dev/null 2>&1 || true
      fi
      ;;

    verify)
      verify_sync
      ;;

    help|-h|--help)
      usage
      ;;

    *)
      echo "unknown command: $cmd" >&2
      usage >&2
      exit 2
      ;;
  esac
}

main "$@"
