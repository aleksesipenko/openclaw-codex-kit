# OpenClaw Codex Proxy Kit v0.2.0

Focused release of the installable OpenClaw Codex proxy kit.

What is included:

- `OpenClaw Codex.command`
- local proxy bootstrap around [CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI)
- Codex account import/add-account rails
- OpenClaw config fragment + apply helper
- install / verify / sync / uninstall / safety-audit scripts

Recommended flow:

1. Install OpenClaw
2. Run `./scripts/install.sh`
3. Open `~/Desktop/OpenClaw Codex.command` if you still need to add a fresh account
4. Run `./scripts/verify.sh`

PRs, fixes, and packaging improvements are welcome.
