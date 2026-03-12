#!/usr/bin/env python3
from __future__ import annotations

import argparse
import subprocess
import sys
import time
import uuid
from pathlib import Path

import json

from codex_quota_snapshot_lib import (
    build_quota_snapshot_payload,
    matching_snapshot_stems,
    read_identity_from_auth_data,
)


CODEX_DIR = Path.home() / ".codex"
AUTH_FILE = CODEX_DIR / "auth.json"
ACCOUNTS_DIR = CODEX_DIR / "accounts"
SESSIONS_DIR = CODEX_DIR / "sessions"
def current_identity() -> tuple[str, str]:
    email, account_id = read_identity_from_auth_data(json.loads(AUTH_FILE.read_text()))
    if not email or not account_id:
        raise SystemExit("current ~/.codex/auth.json has no account_id")
    return email, account_id


def session_contains_marker(session_path: Path, marker: str) -> bool:
    try:
        return marker in session_path.read_text(errors="ignore")
    except Exception:
        return False


def newest_session_after(start_ts: float, marker: str) -> Path | None:
    candidates = []
    for path in SESSIONS_DIR.rglob("*.jsonl"):
        try:
            mtime = path.stat().st_mtime
        except FileNotFoundError:
            continue
        if mtime >= start_ts - 1:
            if session_contains_marker(path, marker):
                candidates.append((mtime, path))
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[0][1]


def extract_rate_limits(session_path: Path) -> dict | None:
    latest = None
    for line in session_path.read_text(errors="ignore").splitlines():
        try:
            record = json.loads(line)
        except Exception:
            continue
        payload = record.get("payload")
        if not isinstance(payload, dict):
            continue
        if payload.get("type") != "token_count":
            continue
        rate_limits = payload.get("rate_limits")
        if isinstance(rate_limits, dict):
            latest = rate_limits
    return latest


def write_quota_snapshots(
    stems: list[str],
    rate_limits: dict,
    session_path: Path,
    *,
    email: str,
    account_id: str,
    marker: str,
) -> list[str]:
    written = []
    payload = build_quota_snapshot_payload(
        rate_limits=rate_limits,
        session_path=session_path,
        source="codex_exec_probe",
        email=email,
        account_id=account_id,
        probe_marker=marker,
    )
    payload["_meta"]["refreshed_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    for stem in stems:
        target = ACCOUNTS_DIR / f".{stem}.quota.json"
        target.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
        written.append(str(target))
    return written


def run_probe(prompt: str, timeout_seconds: float, marker: str) -> tuple[int, str]:
    started = time.time()
    proc = subprocess.run(
        ["codex", "exec", "--skip-git-repo-check", f"{prompt}\n\nquota-probe-marker: {marker}"],
        cwd=str(CODEX_DIR),
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
        check=False,
    )
    combined = "\n".join(part for part in (proc.stdout, proc.stderr) if part).strip()
    return proc.returncode, combined


def main() -> int:
    parser = argparse.ArgumentParser(description="Refresh local Codex quota snapshots from a fresh codex exec probe.")
    parser.add_argument("--prompt", default="Reply with exactly OK.")
    parser.add_argument("--timeout", type=float, default=40.0)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    email, account_id = current_identity()
    stems = matching_snapshot_stems(ACCOUNTS_DIR, email, account_id)
    started = time.time()
    marker = f"quota-probe::{uuid.uuid4().hex}"
    returncode, output = run_probe(args.prompt, args.timeout, marker)
    session_path = newest_session_after(started, marker)
    if session_path is None:
        raise SystemExit("probe finished but no fresh codex session file was found")
    rate_limits = extract_rate_limits(session_path)
    if not isinstance(rate_limits, dict):
        raise SystemExit(f"fresh session {session_path} had no token_count rate_limits payload")
    written = write_quota_snapshots(
        stems,
        rate_limits,
        session_path,
        email=email,
        account_id=account_id,
        marker=marker,
    )

    result = {
        "email": email,
        "account_id": account_id,
        "probe_returncode": returncode,
        "probe_output_excerpt": " ".join(output.split())[:220],
        "session": str(session_path),
        "written": written,
        "rate_limits": rate_limits,
    }
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"[quota-refresh] {email} -> {session_path.name}")
        for path in written:
            print(f"  wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
