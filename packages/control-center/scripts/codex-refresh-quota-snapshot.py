#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import json
import subprocess
import sys
import time
from pathlib import Path


CODEX_DIR = Path.home() / ".codex"
AUTH_FILE = CODEX_DIR / "auth.json"
ACCOUNTS_DIR = CODEX_DIR / "accounts"
SESSIONS_DIR = CODEX_DIR / "sessions"


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
    return email if email and "@" in email else None


def current_identity() -> tuple[str, str]:
    data = json.loads(AUTH_FILE.read_text())
    tokens = data.get("tokens") or {}
    account_id = str(tokens.get("account_id") or "").strip()
    if not account_id:
        raise SystemExit("current ~/.codex/auth.json has no account_id")

    email = None
    for token_name in ("id_token", "access_token"):
        payload = decode_jwt_payload(tokens.get(token_name))
        email = canonical_email(payload.get("email"))
        if email:
            break
        profile = payload.get("https://api.openai.com/profile")
        if isinstance(profile, dict):
            email = canonical_email(profile.get("email"))
            if email:
                break
    if not email:
        raise SystemExit("could not determine email from current ~/.codex/auth.json")
    return email, account_id


def matching_snapshot_stems(email: str, account_id: str) -> list[str]:
    stems: list[str] = []
    short_id = account_id.strip().lower().split("-", 1)[0][:8]
    for path in sorted(ACCOUNTS_DIR.glob("*.json")):
        if path.name.startswith("."):
            continue
        try:
            data = json.loads(path.read_text())
        except Exception:
            continue
        tokens = data.get("tokens") or {}
        if str(tokens.get("account_id") or "").strip() != account_id:
            continue
        payload = decode_jwt_payload(tokens.get("id_token")) or decode_jwt_payload(tokens.get("access_token"))
        candidate_email = canonical_email(payload.get("email"))
        if not candidate_email:
            profile = payload.get("https://api.openai.com/profile")
            if isinstance(profile, dict):
                candidate_email = canonical_email(profile.get("email"))
        if candidate_email != email:
            continue
        stems.append(path.stem)
    if f"{email}--{short_id}" not in stems:
        stems.append(f"{email}--{short_id}")
    if email not in stems:
        stems.append(email)
    return sorted(dict.fromkeys(stems))


def newest_session_after(start_ts: float) -> Path | None:
    candidates = []
    for path in SESSIONS_DIR.rglob("*.jsonl"):
        try:
            mtime = path.stat().st_mtime
        except FileNotFoundError:
            continue
        if mtime >= start_ts - 1:
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


def write_quota_snapshots(stems: list[str], rate_limits: dict, session_path: Path) -> list[str]:
    written = []
    payload = {
        "rate_limits": rate_limits,
        "_meta": {
            "refreshed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "source_session": str(session_path),
            "source": "codex_exec_probe",
        },
    }
    for stem in stems:
        target = ACCOUNTS_DIR / f".{stem}.quota.json"
        target.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
        written.append(str(target))
    return written


def run_probe(prompt: str, timeout_seconds: float) -> tuple[int, str]:
    started = time.time()
    proc = subprocess.run(
        ["codex", "exec", "--skip-git-repo-check", prompt],
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
    stems = matching_snapshot_stems(email, account_id)
    started = time.time()
    returncode, output = run_probe(args.prompt, args.timeout)
    session_path = newest_session_after(started)
    if session_path is None:
        raise SystemExit("probe finished but no fresh codex session file was found")
    rate_limits = extract_rate_limits(session_path)
    if not isinstance(rate_limits, dict):
        raise SystemExit(f"fresh session {session_path} had no token_count rate_limits payload")
    written = write_quota_snapshots(stems, rate_limits, session_path)

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
