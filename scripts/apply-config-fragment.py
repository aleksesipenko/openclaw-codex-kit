#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
OPENCLAW_HOME = Path(os.environ.get("OPENCLAW_HOME", str(Path.home() / ".openclaw")))
CONFIG_PATH = Path(os.environ.get("OPENCLAW_CONFIG_PATH", str(OPENCLAW_HOME / "openclaw.json")))
FRAGMENT_PATH = ROOT / "templates" / "openclaw-kit.fragment.json"
PROXY_CONFIG_PATH = Path(os.environ.get("CLIPROXYAPI_CONFIG_PATH", str(OPENCLAW_HOME / "runtime" / "cliproxyapi" / "config.yaml")))
BACKUP_DIR = Path(os.environ.get("OPENCLAW_KIT_BACKUP_DIR", str(OPENCLAW_HOME / "tooling" / "openclaw-codex-kit" / "backups" / "config")))


def load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    return json.loads(path.read_text())


def deep_merge(base: dict, patch: dict) -> dict:
    out = dict(base)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        elif isinstance(value, list) and isinstance(out.get(key), list):
            seen = []
            for item in out[key] + value:
                if item not in seen:
                    seen.append(item)
            out[key] = seen
        else:
            out[key] = value
    return out


def proxy_settings() -> tuple[str, str]:
    if not PROXY_CONFIG_PATH.exists():
        env_base = os.environ.get("CLIPROXYAPI_BASE_URL", "").strip()
        env_key = os.environ.get("CLIPROXYAPI_KEY", "").strip()
        if env_base and env_key:
            return env_base, env_key
        raise SystemExit(f"proxy config not found: {PROXY_CONFIG_PATH}")

    text = PROXY_CONFIG_PATH.read_text(encoding="utf-8", errors="ignore")
    host_match = re.search(r'^\s*host:\s*"?([^"\n]+)"?\s*$', text, flags=re.MULTILINE)
    port_match = re.search(r"^\s*port:\s*(\d+)\s*$", text, flags=re.MULTILINE)
    key_match = re.search(r'api-keys:\s*\n\s*-\s*"([^"]+)"', text, flags=re.MULTILINE)

    host = host_match.group(1).strip() if host_match else "127.0.0.1"
    if not host or host in {"0.0.0.0", "::", "[::]"}:
        host = "127.0.0.1"
    port = port_match.group(1).strip() if port_match else "8317"
    api_key = key_match.group(1).strip() if key_match else os.environ.get("CLIPROXYAPI_KEY", "").strip()

    if not api_key:
        raise SystemExit(f"could not read proxy api key from {PROXY_CONFIG_PATH}")

    return f"http://{host}:{port}/v1", api_key


def rendered_fragment() -> dict:
    base_url, api_key = proxy_settings()
    text = FRAGMENT_PATH.read_text(encoding="utf-8")
    text = text.replace("__CLIPROXY_BASE_URL__", base_url)
    text = text.replace("__CLIPROXY_API_KEY__", api_key)
    return json.loads(text)


def main() -> int:
    config = load_json(CONFIG_PATH)
    fragment = rendered_fragment()
    merged = deep_merge(config, fragment)

    if CONFIG_PATH.exists():
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        backup_path = BACKUP_DIR / f"{CONFIG_PATH.name}.kit-backup-{stamp}"
        backup_path.write_text(CONFIG_PATH.read_text())

    CONFIG_PATH.write_text(json.dumps(merged, ensure_ascii=False, indent=2) + "\n")
    print(f"merged {FRAGMENT_PATH} into {CONFIG_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
