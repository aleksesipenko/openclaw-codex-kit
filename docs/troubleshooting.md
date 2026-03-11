# Troubleshooting

## `install.sh` fails on missing `openclaw`

Install OpenClaw first and make sure `openclaw --version` works in your shell.

## `install.sh` fails on missing `go`

This kit builds CLIProxyAPI locally. Install Go first, then rerun:

```bash
go version
```

## Proxy starts but the pool is empty

That usually means there were no saved accounts under `~/.codex/accounts`.

Open the launcher and add one manually:

```bash
~/Desktop/OpenClaw\ Codex.command
```

## OpenClaw is still not using `cliproxy-codex`

Re-apply the fragment and restart the gateway:

```bash
python3 ./scripts/apply-config-fragment.py
openclaw gateway restart
```

## Linux or WSL shows manual service mode

That is fine. This kit will run the proxy as a background process there instead of using `launchctl`.

## Safety audit fails

Do not publish yet. Remove the flagged file or string and rerun:

```bash
python3 ./scripts/audit-public-safety.py
```
