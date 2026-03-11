#!/usr/bin/env python3
from __future__ import annotations

import argparse
import glob
import json
from datetime import datetime, timezone
from pathlib import Path


SYNC_KEY = "_quota_sync"


def load_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


def find_quota_file(quota_dir: Path, email: str, auth_stem: str) -> Path | None:
    candidates = [
        quota_dir / f".{auth_stem}.quota.json",
        quota_dir / f".{email}.quota.json",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate

    pattern = str(quota_dir / f".{email}*.quota.json")
    matches = sorted(glob.glob(pattern), key=lambda p: Path(p).stat().st_mtime, reverse=True)
    return Path(matches[0]) if matches else None


def parse_auth_freshness(auth: dict, auth_path: Path) -> float:
    raw_value = auth.get("last_refresh")
    if isinstance(raw_value, str) and raw_value.strip():
        try:
            dt = datetime.fromisoformat(raw_value.replace("Z", "+00:00"))
            return dt.timestamp()
        except ValueError:
            pass
    return auth_path.stat().st_mtime


def quota_snapshot_is_stale(auth: dict, auth_path: Path, quota_path: Path) -> bool:
    # Email-level quota caches can lag behind a freshly reauthed snapshot and falsely
    # disable a now-live account. Only trust the generic email cache if it is at least
    # as fresh as the auth snapshot itself.
    if quota_path.name != f".{str(auth.get('email') or '').strip().lower()}.quota.json":
        return False
    try:
        return quota_path.stat().st_mtime + 60 < parse_auth_freshness(auth, auth_path)
    except FileNotFoundError:
        return False


def compute_reasons(quota: dict, now_ts: float) -> list[str]:
    rate_limits = quota.get("rate_limits", {})
    reasons: list[str] = []

    for key, label in (("primary", "primary"), ("secondary", "secondary")):
        window = rate_limits.get(key, {})
        if not isinstance(window, dict):
            continue
        used = float(window.get("used_percent", 0.0) or 0.0)
        resets_at = window.get("resets_at")
        if used < 100.0:
            continue
        if isinstance(resets_at, (int, float)) and resets_at > now_ts:
            reasons.append(label)

    return reasons


def sync_auth_file(auth_path: Path, quota_dir: Path, now_ts: float) -> dict:
    auth = load_json(auth_path)
    if not isinstance(auth, dict):
        return {"file": auth_path.name, "status": "skipped", "reason": "invalid_auth_json"}

    email = str(auth.get("email") or "").strip().lower()
    if not email:
        return {"file": auth_path.name, "status": "skipped", "reason": "missing_email"}

    quota_path = find_quota_file(quota_dir, email, auth_path.stem)
    if quota_path is None:
        return {"file": auth_path.name, "email": email, "status": "skipped", "reason": "quota_not_found"}

    sync_meta = auth.get(SYNC_KEY)
    managed_before = isinstance(sync_meta, dict) and bool(sync_meta.get("managed_disabled"))
    disabled_before = bool(auth.get("disabled", False))

    quota = load_json(quota_path)
    if not isinstance(quota, dict):
        return {"file": auth_path.name, "email": email, "status": "skipped", "reason": "invalid_quota_json"}

    if quota_snapshot_is_stale(auth, auth_path, quota_path):
        if managed_before:
            auth["disabled"] = False
            auth[SYNC_KEY] = {
                "managed_disabled": False,
                "reasons": [],
                "quota_file": quota_path.name,
                "synced_at": datetime.fromtimestamp(now_ts, tz=timezone.utc).isoformat().replace("+00:00", "Z"),
                "note": "stale_email_quota_snapshot_ignored",
            }
            auth_path.write_text(json.dumps(auth, ensure_ascii=False, indent=2) + "\n")
        return {
            "file": auth_path.name,
            "email": email,
            "status": "skipped",
            "reason": "stale_quota_snapshot",
            "disabled": bool(auth.get("disabled", False)),
            "quota_file": quota_path.name,
        }

    reasons = compute_reasons(quota, now_ts)

    changed = False
    if reasons:
        if not disabled_before or not managed_before or sync_meta.get("reasons") != reasons:
            auth["disabled"] = True
            changed = True
        auth[SYNC_KEY] = {
            "managed_disabled": True,
            "reasons": reasons,
            "quota_file": quota_path.name,
            "synced_at": datetime.fromtimestamp(now_ts, tz=timezone.utc).isoformat().replace("+00:00", "Z"),
        }
    else:
        if managed_before:
            auth["disabled"] = False
            auth[SYNC_KEY] = {
                "managed_disabled": False,
                "reasons": [],
                "quota_file": quota_path.name,
                "synced_at": datetime.fromtimestamp(now_ts, tz=timezone.utc).isoformat().replace("+00:00", "Z"),
            }
            changed = True

    if changed:
        auth_path.write_text(json.dumps(auth, ensure_ascii=False, indent=2) + "\n")

    return {
        "file": auth_path.name,
        "email": email,
        "status": "updated" if changed else "ok",
        "disabled": bool(auth.get("disabled", False)),
        "managed": bool(auth.get(SYNC_KEY, {}).get("managed_disabled")) if isinstance(auth.get(SYNC_KEY), dict) else False,
        "reasons": reasons,
        "quota_file": quota_path.name,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Disable CLIProxyAPI auth files that are exhausted by local Codex quota snapshots.")
    parser.add_argument("--auth-dir", default=str(Path.home() / ".openclaw" / "runtime" / "cliproxyapi" / "auths"))
    parser.add_argument("--quota-dir", default=str(Path.home() / ".codex" / "accounts"))
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    auth_dir = Path(args.auth_dir).expanduser()
    quota_dir = Path(args.quota_dir).expanduser()
    now_ts = datetime.now(timezone.utc).timestamp()

    results = []
    for auth_path in sorted(auth_dir.glob("*.json")):
        results.append(sync_auth_file(auth_path, quota_dir, now_ts))

    disabled_now = [item for item in results if item.get("disabled")]
    summary = {
        "auth_dir": str(auth_dir),
        "quota_dir": str(quota_dir),
        "processed": len(results),
        "disabled_now": len(disabled_now),
        "results": results,
    }

    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    elif not args.quiet:
        print(f"[quota-sync] processed {summary['processed']} auth file(s), disabled now: {summary['disabled_now']}")
        for item in results:
            reasons = ",".join(item.get("reasons", [])) if item.get("reasons") else "-"
            print(f"  {item.get('email','?'):<40} {item['status']:<7} disabled={str(item.get('disabled', False)).lower():<5} reasons={reasons}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
