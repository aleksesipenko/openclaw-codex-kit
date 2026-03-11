#!/usr/bin/env python3
import argparse
import base64
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

CODEX_DIR = Path.home() / ".codex"
AUTH_FILE = CODEX_DIR / "auth.json"
ACCOUNTS_DIR = CODEX_DIR / "accounts"
DEFAULT_QUARANTINE_ROOT = Path(
    os.environ.get("OPENCLAW_HOME", str(Path.home() / ".openclaw"))
) / "tooling" / "openclaw-codex-kit" / "quarantine"


def decode_email(data: dict) -> str | None:
    tokens = data.get("tokens") or {}
    token = tokens.get("id_token") or tokens.get("access_token")
    if not isinstance(token, str) or token.count(".") != 2:
      return None
    try:
      payload = token.split(".")[1]
      payload += "=" * (-len(payload) % 4)
      obj = json.loads(base64.urlsafe_b64decode(payload.encode()).decode())
      email = obj.get("email")
      return email.strip().lower() if isinstance(email, str) and email.strip() else None
    except Exception:
      return None


def read_snapshot(path: Path) -> dict:
    raw = path.read_text()
    parse_error = None
    nul_count = raw.count("\x00")
    data = None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        parse_error = f"{exc.msg} at line {exc.lineno} column {exc.colno} (char {exc.pos})"

    tokens = (data or {}).get("tokens") or {}
    account_id = tokens.get("account_id")
    return {
        "name": path.stem,
        "path": path,
        "data": data,
        "email": decode_email(data or {}),
        "account_id": account_id if isinstance(account_id, str) and account_id.strip() else None,
        "parse_error": parse_error,
        "nul_count": nul_count,
    }


def read_quota(name: str) -> dict | None:
    path = ACCOUNTS_DIR / f".{name}.quota.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


def summarize_quota(quota: dict | None) -> dict:
    rate = (quota or {}).get("rate_limits") or {}
    primary = rate.get("primary") or {}
    secondary = rate.get("secondary") or {}
    day_used = primary.get("used_percent")
    week_used = secondary.get("used_percent")
    day_avail = None if day_used is None else max(0.0, 100.0 - float(day_used))
    week_avail = None if week_used is None else max(0.0, 100.0 - float(week_used))
    if day_avail is None and week_avail is None:
        available = None
    elif day_avail is None:
        available = week_avail
    elif week_avail is None:
        available = day_avail
    else:
        available = min(day_avail, week_avail)
    return {
        "available": available,
        "day_used": day_used,
        "week_used": week_used,
        "day_reset": primary.get("resets_at"),
        "week_reset": secondary.get("resets_at"),
    }


def probe_snapshot(snapshot: dict, timeout_seconds: float) -> dict:
    shutil.copy2(snapshot["path"], AUTH_FILE)
    try:
        proc = subprocess.run(
            ["codex", "exec", "--skip-git-repo-check", "reply OK"],
            cwd=str(CODEX_DIR),
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
        out = "\n".join(part for part in [proc.stdout, proc.stderr] if part).strip()
        lower = out.lower()
        if "refresh_token_reused" in lower or "oauth token refresh failed" in lower or "authentication token has been invalidated" in lower:
            verdict = "auth_dead"
        elif "usage limit" in lower or "rate limit" in lower or "429" in lower:
            verdict = "quota_or_temporary"
        elif proc.returncode == 0:
            verdict = "ok"
        else:
            verdict = "unknown_error"
        return {
            "verdict": verdict,
            "returncode": proc.returncode,
            "excerpt": " ".join(out.split())[:220],
        }
    except subprocess.TimeoutExpired:
        return {
            "verdict": "timeout",
            "returncode": 124,
            "excerpt": f"timed out after {int(timeout_seconds)}s",
        }


def quarantine_snapshot(snapshot: dict, quarantine_dir: Path) -> list[str]:
    moved: list[str] = []
    quarantine_dir.mkdir(parents=True, exist_ok=True)
    target = quarantine_dir / snapshot["path"].name
    if snapshot["path"].exists():
        shutil.move(str(snapshot["path"]), str(target))
        moved.append(str(target))
    quota_file = ACCOUNTS_DIR / f".{snapshot['name']}.quota.json"
    if quota_file.exists():
        quota_target = quarantine_dir / quota_file.name
        shutil.move(str(quota_file), str(quota_target))
        moved.append(str(quota_target))
    return moved


def main() -> int:
    parser = argparse.ArgumentParser(description="Classify Codex account snapshots and quarantine auth-dead ones.")
    parser.add_argument("--apply", action="store_true", help="Move auth-dead snapshots into quarantine.")
    parser.add_argument("--probe-all", action="store_true", help="Probe every saved snapshot, not only zero-availability candidates.")
    parser.add_argument("--timeout", type=float, default=8.0, help="Seconds for a single Codex auth probe.")
    parser.add_argument("--json", action="store_true", help="Output JSON only.")
    args = parser.parse_args()

    accounts = [read_snapshot(path) for path in sorted(ACCOUNTS_DIR.glob("*.json")) if not path.name.startswith(".")]
    original_auth = AUTH_FILE.read_bytes() if AUTH_FILE.exists() else None
    run_stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    quarantine_dir = DEFAULT_QUARANTINE_ROOT / f"{run_stamp}-codex-auth-dead"
    manifest: list[dict] = []

    try:
        for snapshot in accounts:
            quota = summarize_quota(read_quota(snapshot["name"]))
            snapshot["quota"] = quota
            if snapshot["parse_error"]:
                excerpt = snapshot["parse_error"]
                if snapshot["nul_count"]:
                    excerpt += f"; nul_bytes={snapshot['nul_count']}"
                snapshot["probe"] = {
                    "verdict": "corrupt_auth_store",
                    "returncode": None,
                    "excerpt": excerpt,
                }
            else:
                should_probe = args.probe_all or quota["available"] in (None, 0.0)
                if should_probe:
                    snapshot["probe"] = probe_snapshot(snapshot, args.timeout)
                else:
                    snapshot["probe"] = {"verdict": "skipped", "excerpt": ""}

            probe_verdict = snapshot["probe"]["verdict"]
            available = quota["available"]
            if probe_verdict == "corrupt_auth_store":
                status = "corrupt_auth_store"
            elif probe_verdict == "auth_dead":
                status = "auth_dead"
            elif isinstance(available, (int, float)) and available <= 0:
                status = "quota_dead_recoverable"
            elif probe_verdict == "ok":
                status = "live"
            elif probe_verdict in {"quota_or_temporary", "timeout"}:
                status = "degraded_recoverable"
            else:
                status = "live_or_unknown"
            snapshot["status"] = status

            moved = []
            if args.apply and status in {"auth_dead", "corrupt_auth_store"}:
                moved = quarantine_snapshot(snapshot, quarantine_dir)
            snapshot["moved"] = moved
            manifest.append(
                {
                    "name": snapshot["name"],
                    "email": snapshot["email"],
                    "account_id": snapshot["account_id"],
                    "status": snapshot["status"],
                    "quota": quota,
                    "probe": snapshot["probe"],
                    "parse_error": snapshot["parse_error"],
                    "nul_count": snapshot["nul_count"],
                    "moved": moved,
                }
            )
    finally:
        if original_auth is not None:
            AUTH_FILE.write_bytes(original_auth)

    result = {
        "generated_at": run_stamp,
        "apply": args.apply,
        "probe_all": args.probe_all,
        "quarantine_dir": str(quarantine_dir) if args.apply else None,
        "accounts": manifest,
    }

    if args.apply:
        quarantine_dir.mkdir(parents=True, exist_ok=True)
        (quarantine_dir / "manifest.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    for item in manifest:
        quota = item["quota"]["available"]
        quota_text = "?" if quota is None else f"{int(quota)}%"
        print(f"{item['status']:>22}  {item['name']}  available={quota_text}")
        if item["probe"]["excerpt"]:
            print(f"  probe: {item['probe']['excerpt']}")
        for moved in item["moved"]:
            print(f"  moved: {moved}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
