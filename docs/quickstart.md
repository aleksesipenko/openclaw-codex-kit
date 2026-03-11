# Quickstart

## 1. Prereqs

- OpenClaw installed and working
- `python3`
- `git`
- `go`
- `curl`
- `rsync`

## 2. Install the kit

```bash
./scripts/install.sh
```

This deploys the public kit into:

- `~/.openclaw/tooling/openclaw-codex-kit`
- `~/.openclaw/runtime/cliproxyapi`
- `~/.local/bin/openclaw-codex`
- `~/Desktop/OpenClaw Codex.command`

It also:

- builds CLIProxyAPI locally
- writes the proxy config
- starts the proxy
- imports existing `~/.codex/accounts` when available
- applies the OpenClaw provider fragment
- restarts the gateway

## 3. Verify

```bash
./scripts/verify.sh
```

## 4. Add accounts later if needed

If the proxy pool is empty, open:

- `~/Desktop/OpenClaw Codex.command`

Then add a Codex account through the menu.

## 5. Sync later from the repo clone

```bash
./scripts/sync-to-live.sh
```
