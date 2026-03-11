# Control Center Package

Public support bundle for the OpenClaw Codex launcher UX.

Contents:

- `scripts/control-center.sh` — the interactive control center
- `scripts/codex-proxy.sh` — canonical local CLIProxyAPI bootstrap / start / stop / status rail
- `scripts/install-launchers.sh` — installs `openclaw-codex` and `OpenClaw Codex.command`
- Codex rail / rotation / quota helpers
- bundled `codex-accounts.py` switcher

This package is deployable and syncable without depending on a private OpenClaw workspace.
It is built around [CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI), but owns the OpenClaw-specific install and account-rotation story.

Platform note:

- macOS: full launcher + `launchctl` service controls
- Linux/WSL: launcher works, browser opening uses `xdg-open` when present, proxy runs in a managed background process
