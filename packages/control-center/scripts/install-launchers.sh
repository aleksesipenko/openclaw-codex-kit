#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONTROL_CENTER_SCRIPT="$SCRIPT_DIR/control-center.sh"
OPENCLAW_HOME="${OPENCLAW_HOME:-$HOME/.openclaw}"
BACKUP_ROOT="$OPENCLAW_HOME/tooling/openclaw-codex-kit/backups"

if [[ ! -x "$CONTROL_CENTER_SCRIPT" ]]; then
  echo "missing control center script: $CONTROL_CENTER_SCRIPT" >&2
  exit 1
fi

mkdir -p "$HOME/.local/bin" "$HOME/Desktop"

backup_if_present() {
  local source_path="$1"
  local stamp="$2"
  if [[ -e "$source_path" ]]; then
    local backup_dir="$BACKUP_ROOT/launchers-$stamp"
    mkdir -p "$backup_dir"
    cp -p "$source_path" "$backup_dir/"
  fi
}

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
backup_if_present "$HOME/.local/bin/openclaw-codex" "$STAMP"
backup_if_present "$HOME/Desktop/OpenClaw Codex.command" "$STAMP"

cat >"$HOME/.local/bin/openclaw-codex" <<EOF
#!/usr/bin/env bash
set -euo pipefail
export OPENCLAW_HOME="\${OPENCLAW_HOME:-$OPENCLAW_HOME}"
exec "$CONTROL_CENTER_SCRIPT" "\$@"
EOF

cat >"$HOME/Desktop/OpenClaw Codex.command" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

"$HOME/.local/bin/openclaw-codex" "$@"
status=$?
echo
read -r -p "Press Enter to close... " _
exit "$status"
EOF

chmod +x "$HOME/.local/bin/openclaw-codex" "$HOME/Desktop/OpenClaw Codex.command"

echo "Installed:"
echo "  - $HOME/.local/bin/openclaw-codex"
echo "  - $HOME/Desktop/OpenClaw Codex.command"
if [[ -d "$BACKUP_ROOT/launchers-$STAMP" ]]; then
  echo "Backups:"
  echo "  - $BACKUP_ROOT/launchers-$STAMP"
fi
