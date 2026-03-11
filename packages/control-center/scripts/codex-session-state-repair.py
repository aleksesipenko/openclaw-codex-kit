#!/usr/bin/env python3
import argparse
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path


DEFAULT_AGENTS = ["main", "builder", "research", "tessa"]
STICKY_FIELDS = [
    "model",
    "modelProvider",
    "lastModel",
    "lastProvider",
    "providerOverride",
    "modelOverride",
    "fallbackNoticeActiveModel",
    "fallbackNoticeSelectedModel",
    "fallbackNoticeReason",
]
AUTH_FIELDS = [
    "authProfileOverride",
    "authProfileOverrideSource",
    "authProfileOverrideCompactionCount",
]


def load_store(path: Path) -> dict:
    return json.loads(path.read_text())


def write_backup(path: Path) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = path.with_name(f"{path.name}.bak-codex-session-state-repair-{stamp}")
    backup.write_text(path.read_text())
    return backup


def clear_sticky_state(node: dict) -> bool:
    changed = False
    for field in STICKY_FIELDS:
        if node.get(field) is not None:
            node[field] = None
            changed = True
    if node.get("lastAccountId") == "default":
        node["lastAccountId"] = None
        changed = True
    return changed


def clear_auth_override(node: dict) -> bool:
    changed = False
    for field in AUTH_FIELDS:
        new_value = 0 if field == "authProfileOverrideCompactionCount" else None
        if node.get(field) != new_value:
            node[field] = new_value
            changed = True
    return changed


def set_auth_override(node: dict, profile_id: str) -> bool:
    changed = False
    wanted = {
        "authProfileOverride": profile_id,
        "authProfileOverrideSource": "manual",
        "authProfileOverrideCompactionCount": 0,
    }
    for field, value in wanted.items():
        if node.get(field) != value:
            node[field] = value
            changed = True
    return changed


def repair_store(path: Path, agent: str, peer: str | None, heartbeat_profile: str | None) -> tuple[bool, list[str]]:
    if not path.exists():
        return False, []

    data = load_store(path)
    if not isinstance(data, dict):
        return False, []

    direct_pattern = (
        re.compile(rf"^agent:{re.escape(agent)}:telegram:[^:]+:direct:{re.escape(peer)}$")
        if peer
        else None
    )
    changed = False
    touched: list[str] = []

    for key, node in data.items():
        if not isinstance(node, dict):
            continue

        is_main = key == f"agent:{agent}:main"
        is_heartbeat = key == f"agent:{agent}:heartbeat"
        is_direct = bool(direct_pattern and direct_pattern.match(key))
        if not any((is_main, is_heartbeat, is_direct)):
            continue

        local_change = clear_sticky_state(node)
        if is_main or is_direct:
            local_change = clear_auth_override(node) or local_change
        elif is_heartbeat:
            if heartbeat_profile:
                local_change = set_auth_override(node, heartbeat_profile) or local_change
            else:
                local_change = clear_auth_override(node) or local_change

        if local_change:
            touched.append(key)
            changed = True

    if changed:
        write_backup(path)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")

    return changed, touched


def main() -> int:
    parser = argparse.ArgumentParser(description="Clear sticky fallback/model state from live Codex sessions.")
    parser.add_argument("agents", nargs="*", default=DEFAULT_AGENTS)
    parser.add_argument("--peer", default="", help="Optional Telegram peer id for direct work sessions.")
    parser.add_argument(
        "--heartbeat-profile",
        default=None,
        help="Auth profile id to pin on heartbeat sessions, e.g. openai-codex:alex@example.com--abcd1234",
    )
    args = parser.parse_args()

    root = Path(os.environ.get("OPENCLAW_HOME", str(Path.home() / ".openclaw"))) / "agents"
    any_change = False

    for agent in args.agents:
        store = root / agent / "sessions" / "sessions.json"
        changed, touched = repair_store(store, agent, args.peer, args.heartbeat_profile)
        any_change = any_change or changed
        print(
            json.dumps(
                {
                    "agent": agent,
                    "store": str(store),
                    "changed": changed,
                    "touched": touched,
                    "heartbeat_profile": args.heartbeat_profile if touched else None,
                },
                ensure_ascii=False,
            )
        )

    return 0 if any_change or True else 0


if __name__ == "__main__":
    raise SystemExit(main())
