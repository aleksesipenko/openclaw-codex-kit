# OpenClaw Codex Proxy Kit

Installable Codex account-rotation + local proxy kit for OpenClaw.

What you get:

- the `OpenClaw Codex.command` launcher
- a local CLIProxyAPI-backed OpenAI-compatible proxy
- OpenClaw config wiring for `cliproxy-codex`
- Codex account import, add-account, and rotation rails
- install, verify, sync, uninstall, and safety-audit scripts

## What It Uses

The local proxy layer is built around [CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI).

This kit packages an OpenClaw-friendly rail around it:

- build/update the proxy locally
- keep a clean auth pool under `~/.openclaw/runtime/cliproxyapi`
- install a simple launcher/control surface
- switch OpenClaw to the local OpenAI-compatible proxy provider

## Quickstart

1. Install OpenClaw first.
2. Clone this repo.
3. Run:

```bash
./scripts/install.sh
```

By default the installer will:

- copy the public kit into `~/.openclaw/tooling/openclaw-codex-kit`
- build CLIProxyAPI locally
- write a clean proxy config under `~/.openclaw/runtime/cliproxyapi`
- start the proxy service
- import existing `~/.codex/accounts` into the proxy pool when present
- patch `openclaw.json` to use `cliproxy-codex`
- restart the OpenClaw gateway
- install the launcher
- run verification

If there are no saved Codex accounts yet, open `~/Desktop/OpenClaw Codex.command` after install and add one through the menu.

## Layout

- `packages/control-center/` — launcher, proxy rail, rotation helpers
- `templates/` — CLIProxyAPI config template + OpenClaw config fragment
- `scripts/` — install / verify / sync / uninstall / audit / release
- `docs/` — short install and troubleshooting docs

## Safety

Before publishing or tagging:

```bash
python3 ./scripts/audit-public-safety.py
./scripts/build-release-tarball.sh
```
