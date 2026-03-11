#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONTROL_CENTER_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
OPENCLAW_HOME="${OPENCLAW_HOME:-$HOME/.openclaw}"
SWITCHER_PY="$CONTROL_CENTER_DIR/switcher/codex-accounts.py"
RAIL="$SCRIPT_DIR/codex-rail.sh"
SESSION_REPAIR="$SCRIPT_DIR/codex-session-state-repair.py"
PING_AGENT="${CODEX_ROTATE_PING_AGENT:-main}"
PING_PEER="${CODEX_ROTATE_PING_PEER:-}"
PING_MODE="${CODEX_ROTATE_PING_MODE:-main}"
VALIDATE_AGENTS="${CODEX_ROTATE_VALIDATE_AGENTS:-main}"
LOCK_ROOT="${OPENCLAW_HOME}/tmp/locks"
LOCK_DIR="$LOCK_ROOT/codex-oauth.lock"
LOCK_WAIT_SECONDS="${CODEX_LOCK_WAIT_SECONDS:-180}"
export CODEX_QUOTA_PING_TIMEOUT_SECONDS="${CODEX_QUOTA_PING_TIMEOUT_SECONDS:-6}"
export CODEX_QUOTA_SESSION_SCAN_DELAY_SECONDS="${CODEX_QUOTA_SESSION_SCAN_DELAY_SECONDS:-0.25}"
HEALTHY_PROVIDER_ALLOWLIST="${CODEX_ROTATE_HEALTHY_PROVIDERS:-openai-codex,cliproxy-codex}"

if [[ ! -f "$SWITCHER_PY" ]]; then
  echo "missing switcher: $SWITCHER_PY" >&2
  exit 1
fi
if [[ ! -x "$RAIL" ]]; then
  echo "missing rail: $RAIL" >&2
  exit 1
fi
if [[ ! -f "$SESSION_REPAIR" ]]; then
  echo "missing session repair helper: $SESSION_REPAIR" >&2
  exit 1
fi

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
}

acquire_codex_lock

TMP="$(mktemp)"
ROLLBACK_DIR="$(mktemp -d)"
ROTATE_COMMIT=0

cleanup() {
  if [[ "${ROTATE_COMMIT:-0}" -eq 0 ]]; then
    if type restore_orders >/dev/null 2>&1; then
      restore_orders || true
    fi
  fi
  rm -f "$TMP"
  rm -rf "$ROLLBACK_DIR"
  if [[ "${CODEX_OAUTH_LOCK_OWNED:-0}" == "1" ]]; then
    rm -rf "$LOCK_DIR"
  fi
}
trap cleanup EXIT

python3 "$SWITCHER_PY" auto --json > "$TMP"

# Apply selected account to OpenClaw agents + runtime snapshot
"$RAIL" sync >/dev/null

python3 - "$TMP" <<'PY'
import json
import sys
from datetime import datetime
from pathlib import Path

p = Path(sys.argv[1])
d = json.loads(p.read_text())
all_accounts = d.get("all_accounts", {})

def as_float(value, default=0.0):
    try:
        if value is None:
            return float(default)
        return float(value)
    except Exception:
        return float(default)

def row(name, data):
    if "error" in data:
        return (name, None, None, None, data.get("error"))
    return (
        name,
        as_float(data.get("available", 0.0)),
        as_float(data.get("weekly_used", 0.0)),
        as_float(data.get("daily_used", 0.0)),
        None,
    )

rows = [row(k, v) for k, v in all_accounts.items()]
rows_ok = [r for r in rows if r[4] is None]
rows_ok.sort(key=lambda r: (-r[1], r[2], r[3], r[0]))

print("codex uptime rotation summary")
print("switched_to:", d.get("switched_to"))
print("already_active:", d.get("already_active"))
print()
print("top slots by weekly availability:")
for name, avail, weekly, daily, err in rows_ok[:5]:
    print(f"- {name}: available={avail:.0f}% weekly_used={weekly:.0f}% daily_used={daily:.0f}%")

rows_err = [r for r in rows if r[4] is not None]
if rows_err:
    print()
    print("slots with errors:")
    for name, *_rest, err in rows_err:
        print(f"- {name}: {err}")
PY

# Final sanity check
"$RAIL" verify

eval "$(
python3 - "$TMP" <<'PY'
import json
import shlex
import sys

data = json.load(open(sys.argv[1]))
all_accounts = data.get("all_accounts") or {}
valid = []
for name, entry in all_accounts.items():
    if not isinstance(entry, dict):
        continue
    available = entry.get("available")
    if isinstance(available, (int, float)) and available > 0:
        valid.append(
            (
                name,
                float(available),
                float(entry.get("effective_daily_used") or 0.0),
                float(entry.get("effective_weekly_used") or 0.0),
            )
        )
valid.sort(key=lambda item: (-item[1], item[2], item[3], item[0]))
best = valid[0][0] if valid else ""
second = valid[1][0] if len(valid) > 1 else best
best_profile = shlex.quote('openai-codex:' + best) if best else "''"
heartbeat_profile = shlex.quote('openai-codex:' + second) if second else "''"
print(f"BEST_PROFILE_ID={best_profile}")
print(f"HEARTBEAT_PROFILE_ID={heartbeat_profile}")
PY
)"

ping_agent_codex() {
  local agent="$1"
  local mode="${2:-telegram}"
  local peer="${3:-$PING_PEER}"
  local out=""
  local status=""
  local provider=""
  local model=""
  local text=""

  if ! command -v openclaw >/dev/null 2>&1; then
    echo "openclaw binary not found; skipping live ping" >&2
    return 0
  fi

  if [[ "$mode" == "main" ]]; then
    out="$(openclaw agent --agent "$agent" --json --timeout 45 -m "reply exactly: pong" 2>&1)" || return 1
  else
    out="$(openclaw agent --agent "$agent" --channel telegram --to "$peer" --json --timeout 45 -m "reply exactly: pong" 2>&1)" || return 1
  fi

  status="$(printf '%s' "$out" | jq -r '.status // empty' 2>/dev/null || true)"
  provider="$(printf '%s' "$out" | jq -r '.result.meta.agentMeta.provider // empty' 2>/dev/null || true)"
  model="$(printf '%s' "$out" | jq -r '.result.meta.agentMeta.model // empty' 2>/dev/null || true)"
  text="$(printf '%s' "$out" | jq -r '.result.payloads[0].text // empty' 2>/dev/null || true)"
  if [[ "$status" == "ok" ]] && [[ ",$HEALTHY_PROVIDER_ALLOWLIST," == *",$provider,"* ]] && [[ "$text" == "pong" ]]; then
    echo "live_ping: ok agent=$agent mode=$mode peer=$peer provider=$provider model=$model"
    return 0
  fi

  echo "live_ping: failed agent=$agent mode=$mode peer=$peer status=$status provider=$provider model=$model" >&2
  return 1
}

clear_session_overrides() {
  local agent="$1"
  local peer="$2"
  local store="$OPENCLAW_HOME/agents/$agent/sessions/sessions.json"

  [[ -f "$store" ]] || return 0

  python3 - "$store" "$agent" "$peer" <<'PY'
import json
import re
import sys
from pathlib import Path

path = Path(sys.argv[1])
agent = sys.argv[2]
peer = sys.argv[3]

try:
    data = json.loads(path.read_text())
except Exception:
    raise SystemExit(0)

changed = False
pattern = re.compile(rf"^agent:{re.escape(agent)}:telegram:[^:]+:direct:{re.escape(peer)}$")
for key, node in data.items():
    if key != f"agent:{agent}:main" and not pattern.match(key):
        continue
    node = data.get(key)
    if not isinstance(node, dict):
        continue
    if node.get("authProfileOverride") is not None:
        node["authProfileOverride"] = None
        node["authProfileOverrideSource"] = None
        node["authProfileOverrideCompactionCount"] = 0
        changed = True
    if node.get("modelProvider") and node.get("modelProvider") != "openai-codex":
        node["modelProvider"] = None
        changed = True
    if node.get("model") and node.get("modelProvider") is None:
        node["model"] = None
        changed = True
    if node.get("lastAccountId") == "default":
        node["lastAccountId"] = None
        changed = True

if changed:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
PY
}

set_exact_codex_order() {
  local agent="$1"
  shift
  local profile_file="$HOME/.openclaw/agents/$agent/agent/auth-profiles.json"

  [[ -f "$profile_file" ]] || return 1

  python3 - "$profile_file" "$@" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
desired = [x for x in sys.argv[2:] if x]

doc = json.loads(path.read_text())
profiles = doc.get("profiles") or {}
if not isinstance(profiles, dict):
    raise SystemExit(1)

desired = [x for x in desired if isinstance(x, str) and x in profiles]
if not desired:
    raise SystemExit(1)

order = doc.setdefault("order", {})
order["openai-codex"] = desired
path.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n")
PY

  if command -v openclaw >/dev/null 2>&1; then
    openclaw secrets reload >/dev/null 2>&1 || true
  fi
}

snapshot_orders() {
  local agent=""
  local order_json=""
  for agent in $VALIDATE_AGENTS; do
    order_json="$(openclaw models auth order get --agent "$agent" --provider openai-codex --json 2>/dev/null || true)"
    [[ -z "$order_json" ]] && continue
    printf '%s' "$order_json" | jq -r '.order[]?' > "$ROLLBACK_DIR/$agent.order" 2>/dev/null || true
  done
}

restore_orders() {
  local file=""
  local agent=""
  local -a saved=()
  local line=""
  shopt -s nullglob
  for file in "$ROLLBACK_DIR"/*.order; do
    agent="$(basename "$file" .order)"
    saved=()
    while IFS= read -r line; do
      [[ -z "$line" ]] && continue
      saved+=("$line")
    done < "$file"
    [[ ${#saved[@]} -eq 0 ]] && continue
    set_exact_codex_order "$agent" "${saved[@]}" || true
  done
  shopt -u nullglob
}

ensure_valid_profile_for_agent() {
  local agent="$1"
  local peer="$2"
  local mode="$3"
  local order_json=""
  local -a order=()
  local -a final_order=()
  local pid=""
  local p=""

  order_json="$(openclaw models auth order get --agent "$agent" --provider openai-codex --json)"
  while IFS= read -r line; do
    [[ -z "$line" ]] && continue
    order+=("$line")
  done < <(printf '%s' "$order_json" | jq -r '.order[]?')
  if [[ ${#order[@]} -eq 0 ]]; then
    echo "profile_validate: no openai-codex profiles for agent=$agent" >&2
    return 1
  fi

  for pid in "${order[@]}"; do
    # Reset sticky session-level override so probe reflects auth order.
    clear_session_overrides "$agent" "$peer"

    # Strict validation: test exactly one profile, no silent profile fallback.
    set_exact_codex_order "$agent" "$pid"
    if ping_agent_codex "$agent" "$mode" "$peer"; then
      # Keep validated profile first but retain the rest as warm fallback slots.
      final_order=("$pid")
      for p in "${order[@]}"; do
        [[ "$p" == "$pid" ]] && continue
        final_order+=("$p")
      done
      set_exact_codex_order "$agent" "${final_order[@]}"
      echo "profile_validate: prioritized agent=$agent profile=$pid"
      return 0
    fi
  done

  # Restore original order if no valid candidate found.
  set_exact_codex_order "$agent" "${order[@]}" || true
  echo "profile_validate: failed agent=$agent (no valid profile found)" >&2
  return 1
}

final_sanity_check() {
  local agent="$1"
  local peer="$2"
  local mode="$3"

  clear_session_overrides "$agent" "$peer"
  ping_agent_codex "$agent" "$mode" "$peer" || return 1

  if [[ "$agent" == "main" && "$mode" == "telegram" ]]; then
    clear_session_overrides "$agent" "$peer"
    ping_agent_codex "$agent" "main" "$peer" || return 1
  fi

  return 0
}

snapshot_orders

probe_mode="$PING_MODE"
if [[ "$probe_mode" == "telegram" && -z "$PING_PEER" ]]; then
  probe_mode="main"
fi

for agent in $VALIDATE_AGENTS; do
  ensure_valid_profile_for_agent "$agent" "$PING_PEER" "$probe_mode"
done

SESSION_REPAIR_ARGS=()
if [[ -n "$PING_PEER" ]]; then
  SESSION_REPAIR_ARGS+=(--peer "$PING_PEER")
fi
python3 "$SESSION_REPAIR" "${SESSION_REPAIR_ARGS[@]}" --heartbeat-profile "$HEARTBEAT_PROFILE_ID" $VALIDATE_AGENTS >/dev/null

# Final single-lane sanity check for the primary operator agent.
if ! final_sanity_check "$PING_AGENT" "$PING_PEER" "$probe_mode"; then
  echo "live_ping failed after profile pinning; restarting gateway and retrying once..." >&2
  openclaw gateway restart >/dev/null
  sleep 2
  final_sanity_check "$PING_AGENT" "$PING_PEER" "$probe_mode"
fi

ROTATE_COMMIT=1
