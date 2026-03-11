#!/usr/bin/env bash
set -euo pipefail
export TERM="${TERM:-xterm-256color}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONTROL_CENTER_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
OPENCLAW_HOME="${OPENCLAW_HOME:-$HOME/.openclaw}"

# ── Paths ─────────────────────────────────────────────────────────────
RUNTIME_DIR="$OPENCLAW_HOME/runtime/cliproxyapi"
BIN="$RUNTIME_DIR/bin/cliproxyapi"
CONFIG="$RUNTIME_DIR/config.yaml"
AUTH_DIR="$RUNTIME_DIR/auths"
MGMT_KEY_FILE="$RUNTIME_DIR/.management_key"
PID_FILE="$RUNTIME_DIR/state/cliproxyapi.pid"
PORT=8317
BASE="http://127.0.0.1:$PORT"
MGMT="$BASE/v0/management"
PANEL_URL="$BASE/management.html"
LABEL="com.openclaw.codex-proxy"
PROXY_CTL="$SCRIPT_DIR/codex-proxy.sh"
QUOTA_SYNC="$SCRIPT_DIR/codex-proxy-quota-sync.py"
QUOTA_REFRESH="$SCRIPT_DIR/codex-refresh-quota-snapshot.py"
ADD_ACCOUNT_HELPER="$SCRIPT_DIR/codex-proxy-add-account.sh"
PROXY_IMPORTER="$SCRIPT_DIR/codex-proxy-import.py"
CODEX_ACCOUNTS_DIR="${CODEX_ACCOUNTS_DIR:-$HOME/.codex/accounts}"
HAS_LAUNCHCTL=0
if command -v launchctl >/dev/null 2>&1; then
  HAS_LAUNCHCTL=1
fi

OPEN_CMD=""
if command -v open >/dev/null 2>&1; then
  OPEN_CMD="open"
elif command -v xdg-open >/dev/null 2>&1; then
  OPEN_CMD="xdg-open"
fi

# ── Helpers ───────────────────────────────────────────────────────────
mgmt_key()  { cat "$MGMT_KEY_FILE" 2>/dev/null | tr -d '\n'; }
api_key()   { python3 -c "import re,sys;t=open(sys.argv[1]).read();m=re.search(r'api-keys:\s*\n\s*-\s*\"([^\"]+)\"',t);print(m.group(1) if m else '')" "$CONFIG" 2>/dev/null; }
mgmt_curl() { curl -fsS -H "Authorization: Bearer $(mgmt_key)" "$@" 2>/dev/null; }
api_curl()  { curl -fsS -H "Authorization: Bearer $(api_key)" "$@" 2>/dev/null; }
quota_sync() { [[ -f "$QUOTA_SYNC" ]] && python3 "$QUOTA_SYNC" --quiet >/dev/null 2>&1 || true; }
auth_files_json() { mgmt_curl "$MGMT/auth-files" 2>/dev/null || echo ""; }

COL_RESET='\033[0m'
COL_BOLD='\033[1m'
COL_DIM='\033[2m'
COL_GREEN='\033[32m'
COL_YELLOW='\033[33m'
COL_RED='\033[31m'
COL_CYAN='\033[36m'
COL_MAGENTA='\033[35m'
COL_WHITE='\033[97m'
COL_BG_DARK='\033[48;5;236m'
COL_LINE='\033[38;5;240m'

hr()       { printf "${COL_LINE}"; printf '─%.0s' {1..56}; printf "${COL_RESET}\n"; }
header()   { clear; printf "\n${COL_BOLD}${COL_CYAN}  ⚡ OpenClaw Codex Proxy Manager${COL_RESET}\n"; hr; }
wait_key() { echo ""; read -r -p "  Нажми Enter... " _; }

# ── Dashboard ─────────────────────────────────────────────────────────
show_dashboard() {
  header
  local proxy_ok=false models_json="" auth_json="" usage_json=""
  quota_sync

  # 1. Service status
  if [[ "$HAS_LAUNCHCTL" -eq 1 ]]; then
    if launchctl print "gui/$UID/$LABEL" &>/dev/null; then
      printf "  🟢 Сервис:    ${COL_GREEN}работает${COL_RESET}\n"
    else
      printf "  🔴 Сервис:    ${COL_RED}остановлен${COL_RESET}\n"
    fi
  elif [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE" 2>/dev/null)" 2>/dev/null; then
    printf "  🟢 Сервис:    ${COL_GREEN}работает${COL_RESET} ${COL_DIM}(background mode)${COL_RESET}\n"
  else
    printf "  ⚪ Сервис:    ${COL_YELLOW}остановлен${COL_RESET} ${COL_DIM}(background mode)${COL_RESET}\n"
  fi

  # 2. HTTP health + models
  if models_json=$(api_curl "$BASE/v1/models" 2>/dev/null); then
    proxy_ok=true
    local model_count
    model_count=$(echo "$models_json" | python3 -c "import json,sys;print(len(json.load(sys.stdin).get('data',[])))" 2>/dev/null || echo "0")
    printf "  🟢 HTTP:      ${COL_GREEN}127.0.0.1:${PORT}${COL_RESET}  ${COL_DIM}($model_count моделей)${COL_RESET}\n"
  else
    printf "  🔴 HTTP:      ${COL_RED}недоступен${COL_RESET}\n"
  fi

  # 3. Routing strategy
  local strategy
  strategy=$(python3 - "$CONFIG" <<'PY' 2>/dev/null || echo "?"
import re
import sys
text = open(sys.argv[1], "r", encoding="utf-8").read()
match = re.search(r'^\s*strategy:\s*"?([^"\n]+)"?\s*$', text, flags=re.MULTILINE)
print(match.group(1) if match else "round-robin")
PY
)
  printf "  🔄 Роутинг:   ${COL_WHITE}${strategy}${COL_RESET}\n"

  hr

  # 4. Accounts
  printf "  ${COL_BOLD}📋 АККАУНТЫ${COL_RESET}\n\n"
  if $proxy_ok; then
    auth_json=$(mgmt_curl "$MGMT/auth-files" 2>/dev/null || echo "")
  fi

  if [[ -n "$auth_json" ]]; then
    CODEX_ACCOUNTS_DIR="$CODEX_ACCOUNTS_DIR" python3 - "$auth_json" <<'PY'
import glob
import json
import math
import os
from pathlib import Path
import sys
from datetime import datetime, timezone

data = json.loads(sys.argv[1])
files = data.get("files", [])
if not files:
    print("  \033[2m(пул пуст)\033[0m")
    sys.exit(0)

now = datetime.now(timezone.utc)


def render_quota(name, data):
    if not data:
        return ""

    used = data.get("used_percent", 0.0)
    rem = max(0.0, 100.0 - used)

    wait_time = ""
    reset_ts = data.get("resets_at")
    if reset_ts and used >= 100.0:
        delta = reset_ts - now.timestamp()
        if delta > 0:
            hours = int(delta // 3600)
            minutes = int((delta % 3600) // 60)
            if hours > 0:
                wait_time = f"сброс через {hours}ч {minutes}м"
            else:
                wait_time = f"сброс через {minutes}м"

    rem_blocks = max(0, min(10, int(math.ceil(rem / 10.0))))
    bar = "█" * rem_blocks + "░" * (10 - rem_blocks)
    if rem >= 50:
        color = "\033[32m"
    elif rem >= 20:
        color = "\033[33m"
    else:
        color = "\033[31m"

    info = f"      \033[2m└ {name}:\033[0m {color}{bar} {rem:.1f}%\033[0m"
    if wait_time:
        info += f"  \033[31m{wait_time}\033[0m"
    return info


def find_quota_path(auth_name, email):
    auth_stem = Path(auth_name or "").stem
    quota_dir = Path(os.environ.get("CODEX_ACCOUNTS_DIR", str(Path.home() / ".codex" / "accounts")))
    candidates = [
        quota_dir / f".{auth_stem}.quota.json",
        quota_dir / f".{email}.quota.json",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate

    pattern = str(quota_dir / f".{email}*.quota.json")
    matches = sorted(glob.glob(pattern), key=lambda p: Path(p).stat().st_mtime, reverse=True)
    return matches[0] if matches else None


def auth_freshness(auth_entry):
    raw_value = auth_entry.get("last_refresh", "")
    if isinstance(raw_value, str) and raw_value.strip():
        try:
            return datetime.fromisoformat(raw_value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            pass
    return 0.0


def quota_snapshot_is_stale(quota_path, auth_entry, email):
    if not quota_path:
        return False
    if Path(quota_path).name != f".{email}.quota.json":
        return False
    fresh_ts = auth_freshness(auth_entry)
    if fresh_ts <= 0:
        return False
    return Path(quota_path).stat().st_mtime + 60 < fresh_ts


def parse_status_message(raw):
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except Exception:
        return None
    error = data.get("error")
    return error if isinstance(error, dict) else None


def format_retry_time(reset_ts):
    if not isinstance(reset_ts, (int, float)):
        return ""
    reset_dt = datetime.fromtimestamp(reset_ts, tz=timezone.utc).astimezone()
    return reset_dt.strftime("%H:%M")


def local_quota_supports_limit(rate_limits):
    for key in ("primary", "secondary"):
        window = rate_limits.get(key, {})
        if not isinstance(window, dict):
            continue
        used = float(window.get("used_percent", 0.0) or 0.0)
        if used >= 95.0:
            return True
    return False


for i, f in enumerate(files, 1):
    email = f.get("email", "?")
    status = f.get("status", "?")
    disabled = f.get("disabled", False)
    unavail = f.get("unavailable", False)
    msg = f.get("status_message", "")
    parsed_error = parse_status_message(msg)
    error_type = (parsed_error or {}).get("type", "")
    error_reset = (parsed_error or {}).get("resets_at")
    retry_after = f.get("next_retry_after", "")
    plan = f.get("id_token", {}).get("plan_type", "?")
    sub_until_str = f.get("id_token", {}).get("chatgpt_subscription_active_until", "")

    if disabled:
        icon = "⛔"
        status_color = "\033[31m"
        status_label = "DISABLED"
    elif error_type == "usage_limit_reached":
        icon = "🟠"
        status_color = "\033[33m"
        status_label = "RATE LIMIT"
    elif unavail:
        icon = "🟡"
        status_color = "\033[33m"
        status_label = "UNAVAIL"
    elif status in ("active", "ready"):
        icon = "🟢"
        status_color = "\033[32m"
        status_label = "ACTIVE"
    elif any(key in status.lower() for key in ("rate", "limit", "quota")):
        icon = "🟠"
        status_color = "\033[33m"
        status_label = status.upper()[:20]
    elif any(key in status.lower() for key in ("error", "fail", "expired")):
        icon = "🔴"
        status_color = "\033[31m"
        status_label = status.upper()[:20]
    else:
        icon = "⚪"
        status_color = "\033[37m"
        status_label = status[:20]

    plan_badges = {
        "team": "\033[35m★ TEAM\033[0m",
        "plus": "\033[36m◆ PLUS\033[0m",
        "pro": "\033[33m♦ PRO\033[0m",
        "free": "\033[2m○ FREE\033[0m",
    }
    plan_badge = plan_badges.get(plan.lower(), f"\033[2m{plan}\033[0m") if plan else "\033[2m?\033[0m"

    sub_info = ""
    if sub_until_str:
        try:
            sub_dt = datetime.fromisoformat(sub_until_str)
            if sub_dt.tzinfo is None:
                sub_dt = sub_dt.replace(tzinfo=timezone.utc)
            days_left = (sub_dt - now).days
            date_label = sub_dt.astimezone().strftime("%Y-%m-%d")
            if days_left < 0:
                sub_info = f"\033[31mплан истёк {date_label} ({-days_left}д назад) ⚠\033[0m"
            elif days_left <= 3:
                sub_info = f"\033[31mплан до {date_label} (ещё {days_left}д) ⚠\033[0m"
            elif days_left <= 7:
                sub_info = f"\033[33mплан до {date_label} (ещё {days_left}д)\033[0m"
            else:
                sub_info = f"\033[32mплан до {date_label} (ещё {days_left}д)\033[0m"
        except Exception:
            sub_info = sub_until_str[:10]

    extra = ""
    if error_type == "usage_limit_reached":
        reset_label = format_retry_time(error_reset)
        if reset_label:
            extra = f"\n       \033[2m└ live cooldown до {reset_label}\033[0m"
        elif retry_after:
            extra = f"\n       \033[2m└ live cooldown до {retry_after[:16].replace('T', ' ')}\033[0m"
        else:
            extra = "\n       \033[2m└ live usage limit reached\033[0m"
    elif msg and msg != "ok":
        if 'level "minimal" not supported' in msg:
            extra = "\n       \033[2m└ probe OpenClaw стукнулся в unsupported thinking=minimal; это не похоже на квоту\033[0m"
        else:
            extra = f"\n       \033[2m└ {msg}\033[0m"

    quota_info = ""
    quota_path = find_quota_path(f.get("name", ""), email)
    if quota_path:
        try:
            if quota_snapshot_is_stale(quota_path, f, email):
                quota_info += "\n      \033[2m└ local email quota snapshot устарел после свежего OAuth; игнорируем его\033[0m"
            else:
                with open(quota_path) as qf:
                    qdata = json.load(qf)
                rate_limits = qdata.get("rate_limits", {})
                if error_type == "usage_limit_reached" and not local_quota_supports_limit(rate_limits):
                    quota_info += "\n      \033[2m└ local quota snapshot расходится с live cooldown; доверяй live status\033[0m"
                else:
                    primary = render_quota("5 часов", rate_limits.get("primary", {}))
                    secondary = render_quota("Неделя", rate_limits.get("secondary", {}))
                    if primary:
                        quota_info += f"\n{primary}"
                    if secondary:
                        quota_info += f"\n{secondary}"
        except Exception:
            pass

    print(f"  {icon} {i}. \033[1m{email}\033[0m")
    print(f"     {plan_badge}  {status_color}{status_label}\033[0m  | {sub_info}{extra}", end="")
    if quota_info:
        print(quota_info, end="")
    print("\n")
PY
  else
    # Fallback: just list files
    local count=0
    for f in "$AUTH_DIR"/*.json; do
      [[ -f "$f" ]] || continue
      count=$((count + 1))
      printf "  ⚪ %d. %s\n" "$count" "$(basename "$f")"
    done
    [[ $count -eq 0 ]] && printf "  ${COL_DIM}(пул пуст)${COL_RESET}\n"
    echo ""
  fi

  hr

  # 5. Usage stats
  if $proxy_ok; then
    usage_json=$(mgmt_curl "$MGMT/usage" 2>/dev/null || echo "")
    if [[ -n "$usage_json" ]]; then
      python3 - "$usage_json" <<'PY'
import json
import sys

data = json.loads(sys.argv[1])
usage = data.get("usage", {})
total = usage.get("total_requests", 0)
ok = usage.get("success_count", 0)
fail = usage.get("failure_count", 0)
tokens = usage.get("total_tokens", 0)

if total > 0:
    rate = ok / total * 100
    bar = "█" * int(rate / 10) + "░" * (10 - int(rate / 10))
    color = "\033[32m" if rate >= 90 else ("\033[33m" if rate >= 70 else "\033[31m")
    print(f"  📊 Запросы: {ok}✓ / {fail}✗ из {total}  {color}{bar} {rate:.0f}%\033[0m")
    if tokens:
        if tokens > 1_000_000:
            print(f"  📈 Токены:  {tokens / 1_000_000:.1f}M")
        elif tokens > 1_000:
            print(f"  📈 Токены:  {tokens / 1_000:.1f}K")
        else:
            print(f"  📈 Токены:  {tokens}")
else:
    print("  📊 Запросы: \033[2mнет данных (после перезапуска)\033[0m")
PY
    fi
  fi

  echo ""
}

# ── Add Codex Account (browser OAuth) ─────────────────────────────────
add_codex_account() {
  header
  printf "  ${COL_BOLD}🔑 Добавление Codex аккаунта${COL_RESET}\n\n"
  printf "  ${COL_DIM}Ниже пойдёт canonical browser OAuth через codex-rail.sh add.${COL_RESET}\n"
  printf "  ${COL_DIM}Он обновит ~/.codex/auth.json, синканёт OpenClaw rail и потом обновит proxy pool.${COL_RESET}\n\n"

  if "$ADD_ACCOUNT_HELPER" --mode oauth; then
    quota_sync
    printf "\n  ${COL_GREEN}✓ Аккаунт добавлен в canonical rail и proxy pool${COL_RESET}\n"
    return 0
  fi

  printf "\n  ${COL_RED}✗ Canonical OAuth flow не завершился${COL_RESET}\n"
  return 1
}

# ── Remove single account ────────────────────────────────────────────
remove_account() {
  header
  printf "  ${COL_BOLD}🗑  Удаление аккаунта${COL_RESET}\n\n"

  local auth_json
  auth_json=$(mgmt_curl "$MGMT/auth-files" 2>/dev/null || echo "")
  if [[ -z "$auth_json" ]]; then
    printf "  ${COL_RED}Management API недоступен${COL_RESET}\n"
    return 1
  fi

  local names
  names=$(echo "$auth_json" | python3 -c "
import json,sys
data = json.load(sys.stdin)
for i, f in enumerate(data.get('files',[]),1):
    email = f.get('email','?')
    name = f.get('name','?')
    plan = f.get('id_token',{}).get('plan_type','?')
    print(f'  {i}. {email} ({plan}) — {name}')
" 2>/dev/null)

  if [[ -z "$names" ]]; then
    printf "  ${COL_DIM}Пул пуст, нечего удалять${COL_RESET}\n"
    return 0
  fi

  echo "$names"
  echo ""
  read -r -p "  Номер для удаления (0 = отмена): " choice
  if [[ "$choice" == "0" || -z "$choice" ]]; then return 0; fi

  local filename
  filename=$(echo "$auth_json" | python3 -c "
import json,sys
data = json.load(sys.stdin)
files = data.get('files',[])
idx = int(sys.argv[1]) - 1
if 0 <= idx < len(files):
    print(files[idx].get('name',''))
" "$choice" 2>/dev/null)

  if [[ -z "$filename" ]]; then
    printf "  ${COL_RED}Неверный номер${COL_RESET}\n"
    return 1
  fi

  read -r -p "  Удалить $filename? [y/N]: " confirm
  if [[ "$confirm" != "y" && "$confirm" != "Y" ]]; then
    printf "  Отменено.\n"
    return 0
  fi

  local result
  result=$(curl -fsS -X DELETE -H "Authorization: Bearer $(mgmt_key)" "$MGMT/auth-files?name=$filename" 2>/dev/null || echo "")
  if echo "$result" | grep -q '"ok"'; then
    printf "  ${COL_GREEN}✓ Удалён: $filename${COL_RESET}\n"
  else
    printf "  ${COL_RED}✗ Ошибка удаления${COL_RESET}\n"
  fi
}

# ── Wipe all accounts ─────────────────────────────────────────────────
wipe_all_accounts() {
  header
  printf "  ${COL_BOLD}${COL_RED}⚠  Очистка всего пула${COL_RESET}\n\n"

  local auth_json
  auth_json=$(mgmt_curl "$MGMT/auth-files" 2>/dev/null || echo "")
  local count
  count=$(echo "$auth_json" | python3 -c "import json,sys;print(len(json.load(sys.stdin).get('files',[])))" 2>/dev/null || echo "0")

  printf "  Сейчас в пуле: ${COL_BOLD}${count}${COL_RESET} аккаунтов\n\n"
  printf "  ${COL_RED}Все аккаунты будут удалены.${COL_RESET}\n"
  printf "  ${COL_DIM}После этого добавь аккаунты заново через OAuth.${COL_RESET}\n\n"
  read -r -p "  Точно удалить всё? (напиши DELETE): " confirm
  if [[ "$confirm" != "DELETE" ]]; then
    printf "  Отменено.\n"
    return 0
  fi

  local result
  result=$(curl -fsS -X DELETE -H "Authorization: Bearer $(mgmt_key)" "$MGMT/auth-files?all=true" 2>/dev/null || echo "")
  local deleted
  deleted=$(echo "$result" | python3 -c "import json,sys;print(json.load(sys.stdin).get('deleted',0))" 2>/dev/null || echo "0")
  printf "  ${COL_GREEN}✓ Удалено: ${deleted} аккаунтов${COL_RESET}\n"
  printf "  ${COL_DIM}Пул пуст. Используй «Добавить аккаунт» для нового OAuth.${COL_RESET}\n"
}

# ── Service control ───────────────────────────────────────────────────
restart_proxy() {
  header
  printf "  ${COL_BOLD}🔄 Поднятие / перезапуск прокси...${COL_RESET}\n\n"
  if "$PROXY_CTL" restart >/dev/null 2>&1; then
    printf "  ${COL_GREEN}✓ Прокси запущен и отвечает${COL_RESET}\n"
  else
    printf "  ${COL_RED}✗ Не удалось поднять прокси${COL_RESET}\n"
  fi
}

stop_proxy() {
  header
  printf "  ${COL_BOLD}⏹  Остановка прокси...${COL_RESET}\n\n"
  "$PROXY_CTL" stop >/dev/null 2>&1 || true
  printf "  ${COL_GREEN}✓ Остановлен${COL_RESET}\n"
}

setup_proxy() {
  header
  printf "  ${COL_BOLD}🧱 Сборка / настройка прокси...${COL_RESET}\n\n"
  if "$PROXY_CTL" setup >/dev/null 2>&1; then
    quota_sync
    printf "  ${COL_GREEN}✓ CLIProxyAPI собран, настроен и поднят${COL_RESET}\n"
  else
    printf "  ${COL_RED}✗ Не удалось подготовить прокси${COL_RESET}\n"
  fi
}

# ── Open web panel ────────────────────────────────────────────────────
open_panel() {
  if [[ -n "$OPEN_CMD" ]]; then
    "$OPEN_CMD" "$PANEL_URL" >/dev/null 2>&1 &
    printf "  ${COL_GREEN}✓ Открыт в браузере${COL_RESET}\n"
  else
    printf "  ${COL_YELLOW}Не нашла open/xdg-open.${COL_RESET}\n"
    printf "  ${COL_DIM}%s${COL_RESET}\n" "$PANEL_URL"
  fi
}

# ── Import from ~/.codex/accounts ─────────────────────────────────────
import_from_codex() {
  header
  printf "  ${COL_BOLD}📥 Импорт из ~/.codex/accounts${COL_RESET}\n\n"
  python3 "$PROXY_IMPORTER"
  [[ -f "$QUOTA_REFRESH" ]] && python3 "$QUOTA_REFRESH" >/dev/null 2>&1 || true
  quota_sync
  echo ""
  printf "  ${COL_GREEN}✓ Готово${COL_RESET}\n"
}

# ── Main Menu ─────────────────────────────────────────────────────────
main() {
  while true; do
    show_dashboard

    printf "  ${COL_BOLD}ДЕЙСТВИЯ:${COL_RESET}\n\n"
    printf "  ${COL_WHITE}1${COL_RESET}  🔑  Добавить Codex аккаунт (OAuth -> rail + proxy)\n"
    printf "  ${COL_WHITE}2${COL_RESET}  🗑   Удалить аккаунт\n"
    printf "  ${COL_WHITE}3${COL_RESET}  💣  Очистить весь пул\n"
    printf "  ${COL_WHITE}4${COL_RESET}  📥  Импорт из ~/.codex/accounts\n"
    printf "  ${COL_WHITE}5${COL_RESET}  🧱  Сборка / настройка прокси\n"
    printf "  ${COL_WHITE}6${COL_RESET}  🔄  Перезапуск прокси\n"
    printf "  ${COL_WHITE}7${COL_RESET}  ⏹   Стоп прокси\n"
    printf "  ${COL_WHITE}8${COL_RESET}  🌐  Открыть веб-панель\n"
    printf "  ${COL_WHITE}0${COL_RESET}  ✖   Выход\n"
    echo ""
    read -r -p "  → " choice

    case "$choice" in
      1) add_codex_account; wait_key ;;
      2) remove_account; wait_key ;;
      3) wipe_all_accounts; wait_key ;;
      4) import_from_codex; wait_key ;;
      5) setup_proxy; wait_key ;;
      6) restart_proxy; wait_key ;;
      7) stop_proxy; wait_key ;;
      8) open_panel ;;
      0|q|й) break ;;
      *) ;;
    esac
  done

  echo ""
  printf "  ${COL_DIM}👋${COL_RESET}\n"
}

main "$@"
