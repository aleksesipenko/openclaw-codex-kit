# Architecture

This repo has one product surface:

- a Codex proxy + account-rotation rail for OpenClaw

The moving parts are small:

- `packages/control-center/`
  Owns the launcher, account helpers, and the local proxy control rail.
- `templates/cliproxyapi.config.yaml`
  Canonical CLIProxyAPI runtime config template.
- `templates/openclaw-kit.fragment.json`
  The OpenClaw config fragment that adds `cliproxy-codex` and points defaults at it.
- `scripts/install.sh`
  Deploys the kit into `~/.openclaw`, builds the proxy, applies config, restarts the gateway, and verifies the result.

Everything else is support tooling around that flow.

This repo is the committable source-of-truth; the OpenClaw home is the install target.
