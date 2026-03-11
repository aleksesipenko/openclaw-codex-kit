#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


def decode_jwt_payload(token: str | None) -> dict:
    if not isinstance(token, str) or token.count(".") < 2:
        return {}
    try:
        payload_b64 = token.split(".")[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)
        return json.loads(base64.urlsafe_b64decode(payload_b64.encode()).decode())
    except Exception:
        return {}


def canonical_email(value: str | None) -> str | None:
    if not isinstance(value, str):
        return None
    email = value.strip().lower()
    if not email or "@" not in email:
        return None
    return email


def extract_email(*tokens: str | None) -> str | None:
    for token in tokens:
        payload = decode_jwt_payload(token)
        email = canonical_email(payload.get("email"))
        if email:
            return email
        profile = payload.get("https://api.openai.com/profile")
        if isinstance(profile, dict):
            email = canonical_email(profile.get("email"))
            if email:
                return email
    return None


def short_account_id(account_id: str) -> str:
    return account_id.strip().lower().split("-", 1)[0][:8]


def parse_last_refresh(raw_value: object, fallback_mtime: float) -> tuple[float, str]:
    if isinstance(raw_value, str) and raw_value.strip():
        value = raw_value.strip()
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return dt.timestamp(), dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        except ValueError:
            pass
    dt = datetime.fromtimestamp(fallback_mtime, tz=timezone.utc)
    return fallback_mtime, dt.isoformat().replace("+00:00", "Z")


def load_candidates(source_dir: Path, skip_free: bool = True) -> dict[str, dict]:
    winners: dict[str, dict] = {}
    for path in sorted(source_dir.glob("*.json")):
        if path.name.startswith("."):
            continue
        try:
            raw = path.read_bytes()
        except OSError:
            continue
        if not raw or b"\x00" in raw:
            continue
        try:
            data = json.loads(raw.decode())
        except Exception:
            continue
        tokens = data.get("tokens") or {}
        if not isinstance(tokens, dict):
            continue
        access_token = tokens.get("access_token")
        refresh_token = tokens.get("refresh_token")
        account_id = tokens.get("account_id")
        id_token = tokens.get("id_token")
        if not all(isinstance(v, str) and v.strip() for v in (access_token, refresh_token, account_id)):
            continue
        email = extract_email(id_token, access_token)
        if not email:
            continue
        # Detect plan type from JWT
        plan = "?"
        for tok in (id_token, access_token):
            payload = decode_jwt_payload(tok)
            auth_info = payload.get("https://api.openai.com/auth", {})
            if isinstance(auth_info, dict):
                p = auth_info.get("chatgpt_plan_type", "")
                if p:
                    plan = p.lower()
                    break
        if skip_free and plan == "free":
            continue
        rank, last_refresh = parse_last_refresh(data.get("last_refresh"), path.stat().st_mtime)
        identity = account_id.strip()
        record = {
            "email": email,
            "account_id": account_id.strip(),
            "id_token": id_token if isinstance(id_token, str) else "",
            "access_token": access_token.strip(),
            "refresh_token": refresh_token.strip(),
            "last_refresh": last_refresh,
            "rank": rank,
            "source_file": path.name,
            "plan": plan,
        }
        previous = winners.get(identity)
        if previous is None or record["rank"] >= previous["rank"]:
            winners[identity] = record
    return winners


def write_auths(dest_dir: Path, records: dict[str, dict], wipe: bool = False) -> list[dict]:
    dest_dir.mkdir(parents=True, exist_ok=True)
    if wipe:
        for stale in dest_dir.glob("*.json"):
            stale.unlink()
    written: list[dict] = []
    for record in sorted(records.values(), key=lambda item: (item["email"], item["account_id"])):
        filename = f'{record["email"]}--{short_account_id(record["account_id"])}.json'
        payload = {
            "id_token": record["id_token"],
            "access_token": record["access_token"],
            "refresh_token": record["refresh_token"],
            "account_id": record["account_id"],
            "last_refresh": record["last_refresh"],
            "email": record["email"],
            "type": "codex",
            "disabled": False,
            "source_alias": record["source_file"],
        }
        out_path = dest_dir / filename
        out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
        os.chmod(out_path, 0o600)
        written.append({
            "file": str(out_path),
            "email": record["email"],
            "account_id": record["account_id"],
            "plan": record.get("plan", "?"),
            "source_alias": record["source_file"],
        })
    return written


def main() -> int:
    parser = argparse.ArgumentParser(description="Import Codex account snapshots into CLIProxyAPI auths.")
    parser.add_argument("--source", default=str(Path.home() / ".codex" / "accounts"))
    parser.add_argument("--dest", default=str(Path.home() / ".openclaw" / "runtime" / "cliproxyapi" / "auths"))
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--wipe", action="store_true", help="Wipe existing auths before import (default: additive)")
    parser.add_argument("--include-free", action="store_true", help="Include free-plan accounts (default: exclude)")
    args = parser.parse_args()

    source_dir = Path(args.source).expanduser()
    dest_dir = Path(args.dest).expanduser()
    if not source_dir.exists():
        print(f"source dir not found: {source_dir}", file=sys.stderr)
        return 1

    records = load_candidates(source_dir, skip_free=not args.include_free)
    written = write_auths(dest_dir, records, wipe=args.wipe)
    sync_script = Path(__file__).resolve().with_name("codex-proxy-quota-sync.py")
    if sync_script.exists():
        subprocess.run(
            [sys.executable, str(sync_script), "--auth-dir", str(dest_dir), "--quota-dir", str(source_dir), "--quiet"],
            check=False,
        )
    result = {
        "source": str(source_dir),
        "dest": str(dest_dir),
        "imported": len(written),
        "mode": "wipe" if args.wipe else "additive",
        "free_accounts": "included" if args.include_free else "excluded",
        "files": written,
    }
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        mode_label = "WIPE+IMPORT" if args.wipe else "ADDITIVE IMPORT"
        print(f"[{mode_label}] imported {len(written)} auth file(s) into {dest_dir}")
        for item in written:
            print(f"  {item['email']:<40} plan={item.get('plan','?'):<6} account_id={item['account_id'][:8]} <- {item['source_alias']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
