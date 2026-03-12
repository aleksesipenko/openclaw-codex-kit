#!/usr/bin/env python3
"""
Manage multiple OpenAI Codex accounts by swapping auth.json.
"""

import json
import os
import sys
import shutil
import base64
import argparse
import uuid
from pathlib import Path

CODEX_DIR = Path.home() / ".codex"
AUTH_FILE = CODEX_DIR / "auth.json"
ACCOUNTS_DIR = CODEX_DIR / "accounts"

def ensure_dirs():
    if not ACCOUNTS_DIR.exists():
        ACCOUNTS_DIR.mkdir(parents=True)

def decode_jwt_payload(token):
    try:
        # JWT is header.payload.signature
        parts = token.split('.')
        if len(parts) != 3:
            return {}
        
        payload = parts[1]
        # Add padding if needed
        payload += '=' * (-len(payload) % 4)
        
        decoded = base64.urlsafe_b64decode(payload)
        return json.loads(decoded)
    except Exception:
        return {}


def _read_tokens(data: dict | None) -> dict:
    tokens = data.get('tokens', {}) if isinstance(data, dict) else {}
    return tokens if isinstance(tokens, dict) else {}


def _read_account_id_from_data(data: dict | None) -> str | None:
    account_id = _read_tokens(data).get("account_id")
    if isinstance(account_id, str) and account_id.strip():
        return account_id.strip()
    return None


def _short_account_id(account_id: str | None) -> str | None:
    if not isinstance(account_id, str):
        return None
    value = account_id.strip().lower()
    if not value:
        return None
    return value.split("-", 1)[0][:8]

def _read_token_exp_seconds(data: dict) -> int | None:
    """Try to extract an expiry timestamp (unix seconds) from known JWT fields.

    Note: access/id tokens are intentionally short-lived (minutes/hours).
    """
    try:
        tokens = data.get('tokens', {}) if isinstance(data, dict) else {}
        if not isinstance(tokens, dict):
            return None

        # Prefer id_token (has email claim), fall back to access_token.
        for key in ("id_token", "access_token"):
            tok = tokens.get(key)
            if isinstance(tok, str) and tok.count('.') == 2:
                payload = decode_jwt_payload(tok)
                exp = payload.get('exp')
                if isinstance(exp, (int, float)) and exp > 0:
                    return int(exp)
        return None
    except Exception:
        return None


def get_account_info(auth_path):
    """Return email and other info from an auth.json file."""
    if not auth_path.exists():
        return None

    try:
        with open(auth_path, 'r') as f:
            data = json.load(f)

        exp = _read_token_exp_seconds(data)
        last_refresh = data.get('last_refresh') if isinstance(data, dict) else None
        account_id = _read_account_id_from_data(data)

        # Look for id_token in tokens object
        tokens = _read_tokens(data)
        id_token = tokens.get('id_token')

        if id_token:
            payload = decode_jwt_payload(id_token)
            plan_type = None
            auth_claims = payload.get("https://api.openai.com/auth")
            if isinstance(auth_claims, dict):
                raw_plan = auth_claims.get("chatgpt_plan_type")
                if isinstance(raw_plan, str) and raw_plan.strip():
                    plan_type = raw_plan.strip()
            return {
                'email': payload.get('email', 'unknown'),
                'name': payload.get('name'),
                'account_id': account_id,
                'plan_type': plan_type,
                'exp': exp,
                'last_refresh': last_refresh,
                'raw': data
            }

        # Fallback if structure is different
        return {
            'email': 'unknown',
            'account_id': account_id,
            'exp': exp,
            'last_refresh': last_refresh,
            'raw': data,
        }
    except Exception as e:
        return {'email': 'error', 'error': str(e)}


def _canonical_email(value: str | None) -> str | None:
    if not isinstance(value, str):
        return None
    email = value.strip().lower()
    if not email or email in ("unknown", "error") or "@" not in email:
        return None
    return email


def _canonical_snapshot_name(email: str | None, account_id: str | None, fallback: str = "account") -> str:
    email = _canonical_email(email)
    short_id = _short_account_id(account_id)

    if email:
        seen_ids: set[str] = set()
        for path in _iter_account_files():
            info = get_account_info(path) or {}
            if _canonical_email(info.get("email")) != email:
                continue
            existing_account_id = info.get("account_id")
            if isinstance(existing_account_id, str) and existing_account_id.strip():
                seen_ids.add(existing_account_id.strip())
        if isinstance(account_id, str) and account_id.strip():
            seen_ids.add(account_id.strip())

        if len(seen_ids) > 1 and short_id:
            return f"{email}--{short_id}"
        return email

    if short_id:
        return f"account--{short_id}"
    return fallback

def is_current(stored_path):
    """Check if the stored file matches the current auth.json."""
    if not AUTH_FILE.exists() or not stored_path.exists():
        return False
    
    # Simple content comparison
    with open(AUTH_FILE, 'rb') as f1, open(stored_path, 'rb') as f2:
        return f1.read() == f2.read()

def resolve_active_profile():
    """Return (name, email) for the currently active auth.json if it matches a saved profile."""
    if not AUTH_FILE.exists():
        return None

    info = get_account_info(AUTH_FILE) or {}
    active_email = _canonical_email(info.get("email"))

    for account in _build_account_catalog():
        if account["active"]:
            return account["name"], account.get("email", "unknown")

    active_account_id = info.get("account_id")
    active_name = _canonical_snapshot_name(active_email, active_account_id, fallback=active_email or "account")
    return active_name, active_email or info.get("email", "unknown")


def _format_expiry(exp_seconds: int | None) -> str:
    """Format access/id token expiry (short-lived). Mostly useful for debugging."""
    if not exp_seconds:
        return ""
    try:
        import time
        now = int(time.time())
        delta = exp_seconds - now
        if delta <= 0:
            return "(token expired)"
        if delta < 60:
            return "(token <1m)"
        mins = delta // 60
        if mins < 120:
            return f"(token {mins}m)"
        hours = mins // 60
        rem_m = mins % 60
        if hours < 48:
            return f"(token {hours}h{rem_m:02d}m)"
        days = hours // 24
        return f"(token {days}d)"
    except Exception:
        return ""


def _format_refreshed(last_refresh: str | None, fallback_path: Path | None = None) -> str:
    """More useful than token exp: when this snapshot was last refreshed."""
    try:
        from datetime import datetime, timezone

        ts: datetime | None = None
        if isinstance(last_refresh, str) and last_refresh.strip():
            raw = last_refresh.strip()
            # Python doesn't like trailing Z with fromisoformat.
            if raw.endswith('Z'):
                raw = raw[:-1] + '+00:00'
            ts = datetime.fromisoformat(raw)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
        elif fallback_path is not None and fallback_path.exists():
            ts = datetime.fromtimestamp(fallback_path.stat().st_mtime, tz=timezone.utc)

        if not ts:
            return "refreshed ?"

        now = datetime.now(timezone.utc)
        delta = now - ts
        seconds = int(delta.total_seconds())
        if seconds < 60:
            return "refreshed just now"
        mins = seconds // 60
        if mins < 120:
            return f"refreshed {mins}m ago"
        hours = mins // 60
        rem_m = mins % 60
        if hours < 48:
            return f"refreshed {hours}h{rem_m:02d}m ago"
        days = hours // 24
        return f"refreshed {days}d ago"
    except Exception:
        return "refreshed ?"


def _parse_refresh_dt(last_refresh: str | None, fallback_path: Path | None = None):
    try:
        from datetime import datetime, timezone

        ts: datetime | None = None
        if isinstance(last_refresh, str) and last_refresh.strip():
            raw = last_refresh.strip()
            if raw.endswith('Z'):
                raw = raw[:-1] + '+00:00'
            ts = datetime.fromisoformat(raw)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
        elif fallback_path is not None and fallback_path.exists():
            ts = datetime.fromtimestamp(fallback_path.stat().st_mtime, tz=timezone.utc)
        return ts
    except Exception:
        return None


def _snapshot_refresh_ts(info: dict | None, fallback_path: Path) -> float:
    ts = _parse_refresh_dt((info or {}).get("last_refresh"), fallback_path=fallback_path)
    if ts is not None:
        return ts.timestamp()
    try:
        return fallback_path.stat().st_mtime
    except Exception:
        return 0.0


def _iter_account_files():
    for path in sorted(ACCOUNTS_DIR.glob("*.json")):
        if path.name.startswith('.'):
            continue
        yield path


def _build_account_catalog():
    raw_entries: list[dict] = []
    for path in _iter_account_files():
        info = get_account_info(path) or {}
        email = _canonical_email(info.get("email")) if isinstance(info, dict) else None
        account_id = info.get("account_id") if isinstance(info, dict) else None
        raw_entries.append(
            {
                "email": email,
                "account_id": account_id,
                "slot_name": path.stem,
                "path": path,
                "info": info,
                "active": is_current(path),
                "last_refresh": info.get("last_refresh") if isinstance(info, dict) else None,
                "exp": info.get("exp") if isinstance(info, dict) else None,
                "plan_type": info.get("plan_type") if isinstance(info, dict) else None,
                "sort_ts": _snapshot_refresh_ts(info, path),
            }
        )

    groups: dict[str, list[dict]] = {}
    for entry in raw_entries:
        label = _canonical_snapshot_name(entry["email"], entry.get("account_id"), fallback=entry["slot_name"])
        enriched = dict(entry)
        enriched["name"] = label
        groups.setdefault(label, []).append(enriched)

    catalog = []
    for label, entries in groups.items():
        entries.sort(
            key=lambda item: (item["slot_name"] == label, item["active"], item["sort_ts"]),
            reverse=True,
        )
        primary = entries[0]
        aliases = [item["slot_name"] for item in entries if item["slot_name"] != label]
        active_slot = next((item["slot_name"] for item in entries if item["active"]), None)
        catalog.append(
            {
                "name": label,
                "email": primary["email"],
                "account_id": primary.get("account_id"),
                "plan_type": primary.get("plan_type"),
                "slot_name": primary["slot_name"],
                "path": primary["path"],
                "info": primary["info"],
                "active": any(item["active"] for item in entries),
                "active_slot": active_slot,
                "aliases": aliases,
                "last_refresh": primary["last_refresh"],
                "exp": primary["exp"],
            }
        )

    catalog.sort(key=lambda item: item["name"])
    return catalog


def ensure_canonical_snapshot_files() -> None:
    ensure_dirs()
    for account in _build_account_catalog():
        source = account.get("path")
        name = account.get("name")
        if not name or not isinstance(source, Path) or not source.exists():
            continue

        target = ACCOUNTS_DIR / f"{name}.json"
        if target == source and target.exists():
            continue

        should_copy = not target.exists()
        if not should_copy:
            existing_info = get_account_info(target) or {}
            should_copy = _snapshot_refresh_ts(account.get("info") or {}, source) >= _snapshot_refresh_ts(existing_info, target)

        if should_copy:
            try:
                success, _ = safe_save_token(source, target, force=False)
                if not success and not target.exists():
                    shutil.copy2(source, target)
            except Exception:
                continue


def _resolve_account_path(identifier: str) -> Path | None:
    raw = (identifier or "").strip()
    if not raw:
        return None

    direct = ACCOUNTS_DIR / f"{raw}.json"
    if direct.exists() and not direct.name.startswith('.'):
        return direct

    wanted_email = _canonical_email(raw)
    for account in _build_account_catalog():
        if raw == account["name"] or raw == account["slot_name"]:
            return account["path"]
        if raw in account.get("aliases", []):
            return account["path"]
        if wanted_email and account.get("email") == wanted_email:
            return account["path"]

    return None


def cmd_list(verbose: bool = False, json_mode: bool = False):
    ensure_dirs()

    accounts = _build_account_catalog()
    max_name = max((len(account["name"]) for account in accounts), default=0)

    if not accounts:
        if json_mode:
            print(json.dumps({"accounts": [], "active": None}, indent=2))
        else:
            print("(no accounts saved)")
        return

    if json_mode:
        from datetime import datetime, timezone
        import time

        now = datetime.now(timezone.utc)
        now_epoch = int(time.time())
        payload_accounts = []
        active_name = None
        active_slot = None

        for account in accounts:
            name = account["name"]
            active = account["active"]
            last_refresh = account["last_refresh"]
            exp = account["exp"]
            path = account["path"]
            if active:
                active_name = name
                active_slot = account.get("active_slot") or account["slot_name"]
            ts = _parse_refresh_dt(last_refresh, fallback_path=path)
            age_s = int((now - ts).total_seconds()) if ts else None
            ttl_s = int(exp - now_epoch) if isinstance(exp, int) else None
            token_exp_iso = (
                datetime.fromtimestamp(exp, tz=timezone.utc).isoformat().replace('+00:00', 'Z')
                if isinstance(exp, int)
                else None
            )
            payload_accounts.append(
                {
                    "name": name,
                    "email": account.get("email"),
                    "slot_name": account.get("slot_name"),
                    "aliases": account.get("aliases", []),
                    "active": bool(active),
                    "last_refresh": last_refresh if isinstance(last_refresh, str) else None,
                    "refreshed_age_seconds": age_s,
                    "token_exp": token_exp_iso,
                    "token_ttl_seconds": ttl_s,
                }
            )

        print(
            json.dumps(
                {
                    "generated_at": now.isoformat(),
                    "active": active_name,
                    "active_slot": active_slot,
                    "accounts": payload_accounts,
                },
                indent=2,
            )
        )
        return

    lines = []
    for account in accounts:
        name = account["name"]
        active = account["active"]
        last_refresh = account["last_refresh"]
        exp = account["exp"]
        path = account["path"]
        display = f"**{name}**" if name else name

        if verbose:
            left = f"- {display.ljust(max_name + 4)}  {_format_refreshed(last_refresh, fallback_path=path)}"
            extra = _format_expiry(exp)
            if extra:
                left += f"  {extra}"
        else:
            left = f"- {display.ljust(max_name + 4)}"

        if active:
            left += "  ✅"
        lines.append(left)

    header = "Codex Accounts"
    underline = "—" * len(header)
    print(header + "\n" + underline + "\n" + "\n".join(lines))

def _resolve_matching_account(email: str, account_id: str | None = None) -> Path | None:
    """Find an existing saved account file for the same underlying Codex identity."""
    want = _canonical_email(email)
    if not want:
        return None

    exact_matches: list[Path] = []
    matches: list[Path] = []
    seen_identity_markers: set[str] = set()
    for f in _iter_account_files():
        info = get_account_info(f) or {}
        got = _canonical_email(info.get("email"))
        if got == want:
            matches.append(f)
            got_account_id = info.get("account_id")
            marker = got_account_id if isinstance(got_account_id, str) and got_account_id.strip() else f"slot:{f.stem}"
            seen_identity_markers.add(marker)
            if account_id and got_account_id == account_id:
                exact_matches.append(f)

    if exact_matches:
        exact_matches.sort(key=lambda path: _snapshot_refresh_ts(get_account_info(path) or {}, path), reverse=True)
        return exact_matches[0]

    if not matches:
        return None

    if len(seen_identity_markers) > 1:
        return None

    for f in matches:
        if f.stem.strip().lower() == want:
            return f

    matches.sort(key=lambda path: _snapshot_refresh_ts(get_account_info(path) or {}, path), reverse=True)
    return matches[0]


def _resolve_unique_name_path(base_name: str) -> tuple[str, Path]:
    base = (base_name or "account").strip() or "account"
    target = ACCOUNTS_DIR / f"{base}.json"
    if not target.exists():
        return base, target

    suffix = 2
    while True:
        candidate_name = f"{base}-{suffix}"
        candidate = ACCOUNTS_DIR / f"{candidate_name}.json"
        if not candidate.exists():
            return candidate_name, candidate
        suffix += 1


def cmd_add(name_override: str | None = None):
    """Add accounts by ALWAYS running a fresh login flow.

    Behavior:
    - Always triggers a new browser login.
    - After login, detects the email from ~/.codex/auth.json.
    - If we already have a saved account with that SAME email+account_id: update that slot.
    - Otherwise: save a new canonical snapshot from email (and account_id when needed).

    Interactive (TTY): can repeat.
    Non-interactive (Clawdbot): single-shot.
    """
    ensure_dirs()

    interactive = bool(sys.stdin.isatty() and sys.stdout.isatty())

    while True:
        do_browser_login()

        if not AUTH_FILE.exists():
            print("❌ Login did not produce ~/.codex/auth.json.")
            if not interactive:
                return
            retry = input("Retry login? [Y/n] ").strip().lower()
            if retry == 'n':
                return
            continue

        info = get_account_info(AUTH_FILE) or {}
        email = info.get('email', 'unknown')
        current_email = (email or '').strip().lower() if isinstance(email, str) else ''
        current_account_id = info.get("account_id") if isinstance(info, dict) else None
        print(f"Found active session for: {email}")

        suggested = _canonical_snapshot_name(current_email, current_account_id, fallback="account")

        # If --name is provided, treat it as an explicit snapshot slot.
        # This allows keeping multiple workspace/quota snapshots even for the same email.
        if name_override:
            explicit = (name_override or "").strip()
            target = ACCOUNTS_DIR / f"{explicit}.json"
            if target.exists():
                existing = get_account_info(target) or {}
                existing_email = (existing.get("email") or "").strip().lower()
                if existing_email and existing_email not in ("unknown", "error") and existing_email != current_email:
                    print(
                        f"❌ Refusing to overwrite '{explicit}': has {existing_email}, new login is {current_email}"
                    )
                    return
            shutil.copy2(AUTH_FILE, target)
            print(f"✅ Saved '{explicit}' ({email})")
        else:
            # 1) If we already have this identity stored under ANY name, update that file.
            match = _resolve_matching_account(current_email, current_account_id)
            canonical_name = _canonical_snapshot_name(current_email, current_account_id, fallback=suggested)
            canonical_target = ACCOUNTS_DIR / f"{canonical_name}.json"
            if match is not None:
                target = canonical_target
                if is_current(match) and target.exists() and is_current(target):
                    print(f"ℹ️  '{canonical_name}' already up to date for {current_email}")
                else:
                    print(f"ℹ️  Updating existing account '{canonical_name}' ({current_email})")
                    shutil.copy2(AUTH_FILE, target)
                print(f"✅ Saved '{canonical_name}' ({email})")
            else:
                # 2) Otherwise, create a new snapshot with default name.
                base_name = suggested
                name, target = _resolve_unique_name_path(base_name)
                shutil.copy2(AUTH_FILE, target)
                print(f"✅ Saved '{name}' ({email})")

        if not interactive:
            return

        more = input("\nAdd another account? [y/N] ").strip().lower()
        if more != 'y':
            return

def do_browser_login():
    import subprocess
    import time

    print("\n🚀 Starting browser login (codex logout && codex login)...")

    before_mtime = AUTH_FILE.stat().st_mtime if AUTH_FILE.exists() else 0

    subprocess.run(["codex", "logout"], capture_output=True)

    # This typically opens the system browser and completes via localhost callback.
    # Prevent auto-opening the default browser. This avoids instantly re-logging
    # into whatever account is already signed into your primary browser profile.
    # You'll open the printed URL in the browser/profile you want.
    env = dict(os.environ)
    env["BROWSER"] = "/usr/bin/false"

    process = subprocess.Popen(
        ["codex", "login"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        env=env,
    )

    # Stream output (so you can see errors) and watch auth.json for changes.
    start = time.time()
    timeout_s = 15 * 60
    
    while True:
        # Non-blocking-ish read: poll process and attempt readline
        if process.stdout:
            line = process.stdout.readline()
            if line:
                print(line.rstrip())
                # Common device-auth policy message; helpful to surface
                if "device code" in line.lower() and "admin" in line.lower():
                    pass

        if AUTH_FILE.exists():
            mtime = AUTH_FILE.stat().st_mtime
            if mtime > before_mtime:
                # auth.json updated; likely success
                break

        if process.poll() is not None:
            # Process ended; if auth didn't change, it's likely failure
            break

        if time.time() - start > timeout_s:
            process.kill()
            print("\n❌ Login timed out after 15 minutes.")
            return

        time.sleep(0.2)

    process.wait(timeout=5)

    if AUTH_FILE.exists() and AUTH_FILE.stat().st_mtime > before_mtime:
        print("\n✅ Login successful (auth.json updated).")
    else:
        print("\n❌ Login did not update auth.json (may have failed).")


def do_device_login():
    import subprocess
    import re
    
    print("\n🚀 Starting Device Flow Login...")
    
    # 1. Logout first to be safe
    subprocess.run(["codex", "logout"], capture_output=True)
    
    # 2. Start login process
    process = subprocess.Popen(
        ["codex", "login", "--device-auth"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1
    )
    
    url = None
    code = None
    
    print("Waiting for code...")
    
    # Read output line by line to find URL and code
    while True:
        line = process.stdout.readline()
        if not line and process.poll() is not None:
            break
        if not line:
            continue
            
        # print(f"DEBUG: {line.strip()}")
        
        # Capture URL
        if "https://auth.openai.com" in line:
            url = line.strip()
            # Remove ANSI color codes
            url = re.sub(r'\x1b\[[0-9;]*m', '', url)
        
        # Capture Code (usually 8 chars like ABCD-1234)
        # Regex for code: 4 chars - 5 chars (actually usually 4-4 or 4-5)
        # The output says: "Enter this one-time code"
        # Then next line has the code
        if "Enter this one-time code" in line:
            # The next line should be the code
            code_line = process.stdout.readline()
            code = code_line.strip()
            code = re.sub(r'\x1b\[[0-9;]*m', '', code)
            
            if url and code:
                print("\n" + "="*50)
                print(f"👉 OPEN THIS: {url}")
                print(f"🔑 ENTER CODE: {code}")
                print("="*50 + "\n")
                print("Waiting for you to complete login in browser...")
                break
    
    # Wait for process to finish (it exits after successful login)
    process.wait()
    
    if process.returncode == 0:
        print("\n✅ Login successful!")
    else:
        print("\n❌ Login failed or timed out.")

def _get_quota_cache_file(name):
    """Get path to quota cache file for an account."""
    return ACCOUNTS_DIR / f".{name}.quota.json"

def _save_quota_cache(name, limits, meta=None):
    """Save quota to cache file."""
    import time
    cache_file = _get_quota_cache_file(name)
    try:
        payload = {
            'rate_limits': limits,
            'cached_at': time.time()
        }
        if isinstance(meta, dict) and meta:
            payload['_meta'] = {key: value for key, value in meta.items() if value is not None}
        with open(cache_file, 'w') as f:
            json.dump(payload, f)
    except:
        pass

def _load_quota_cache(name, max_age_hours=24):
    """Load quota from cache if fresh enough.

    Supports both legacy formats:
    - { rate_limits: ..., cached_at: <epoch> }
    - { rate_limits: ..., collected_at: <epoch> }

    If neither timestamp exists, we fall back to the file mtime.
    """
    import time
    cache_file = _get_quota_cache_file(name)
    if not cache_file.exists():
        return None
    try:
        with open(cache_file, 'r') as f:
            data = json.load(f)

        cached_at = data.get('cached_at') or data.get('collected_at')
        if not isinstance(cached_at, (int, float)):
            cached_at = cache_file.stat().st_mtime

        if time.time() - float(cached_at) < max_age_hours * 3600:
            return data.get('rate_limits')
    except Exception:
        pass
    return None


def _read_account_id(path: Path) -> str | None:
    try:
        data = json.loads(path.read_text())
        tokens = data.get("tokens") if isinstance(data, dict) else None
        if isinstance(tokens, dict):
            account_id = tokens.get("account_id")
            if isinstance(account_id, str) and account_id.strip():
                return account_id
    except Exception:
        return None
    return None


def _sync_auth_back_to_snapshot(source: Path) -> None:
    """Persist refreshed ~/.codex/auth.json back into the currently tested snapshot.

    During `auto`, we copy each account snapshot to AUTH_FILE and invoke Codex.
    If Codex rotates refresh/access tokens, we must write that new state back
    to the same snapshot slot, otherwise future runs keep stale refresh tokens.
    """
    try:
        if not AUTH_FILE.exists() or not source.exists():
            return

        src_account = _read_account_id(source)
        cur_account = _read_account_id(AUTH_FILE)
        if src_account and cur_account and src_account != cur_account:
            # Safety guard: never overwrite a different account slot.
            return

        if AUTH_FILE.read_bytes() != source.read_bytes():
            shutil.copy2(AUTH_FILE, source)
    except Exception:
        # Never fail the quota flow because of snapshot sync.
        return

def _get_quota_for_account(name, source: Path):
    """Get quota info for an account by switching to it and pinging Codex."""
    import subprocess
    import time
    from datetime import datetime

    try:
        ping_timeout_seconds = float(os.environ.get("CODEX_QUOTA_PING_TIMEOUT_SECONDS", "6"))
    except Exception:
        ping_timeout_seconds = 6.0
    ping_timeout_seconds = max(1.0, ping_timeout_seconds)

    try:
        session_scan_delay_seconds = float(
            os.environ.get("CODEX_QUOTA_SESSION_SCAN_DELAY_SECONDS", "0.25")
        )
    except Exception:
        session_scan_delay_seconds = 0.25
    session_scan_delay_seconds = max(0.0, session_scan_delay_seconds)
    
    if not source.exists():
        return None

    probe_marker = f"quota-probe::{name}::{uuid.uuid4().hex}"
    source_info = get_account_info(source) or {}
    source_email = _canonical_email(source_info.get("email"))
    source_account_id = source_info.get("account_id")

    # Switch to account
    shutil.copy(source, AUTH_FILE)
    
    # Record time before ping to only look at sessions created after
    before_ping = time.time()
    
    # Ping codex to get a fresh session (for rate limit info)
    # Note: `-p` is *profile*, not prompt. Use `codex exec` like codex-quota.
    try:
        subprocess.run(
            ["codex", "exec", "--skip-git-repo-check", f"Reply with exactly OK.\n\nquota-probe-marker: {probe_marker}"],
            cwd=str(CODEX_DIR),
            capture_output=True,
            timeout=ping_timeout_seconds,
        )
    except Exception:
        pass

    # Keep this account snapshot fresh after any token rotation.
    _sync_auth_back_to_snapshot(source)
    
    time.sleep(session_scan_delay_seconds)
    
    # Find sessions created AFTER our ping and extract rate limits
    sessions_dir = CODEX_DIR / "sessions"
    now = datetime.now()
    
    for day_offset in range(2):
        date = datetime.fromordinal(now.toordinal() - day_offset)
        day_dir = sessions_dir / f"{date.year:04d}" / f"{date.month:02d}" / f"{date.day:02d}"
        
        if not day_dir.exists():
            continue
        
        # Only consider sessions created after our ping
        jsonl_files = [f for f in day_dir.glob("*.jsonl") if f.stat().st_mtime > before_ping]
        if not jsonl_files:
            continue
            
        # Check each new session for rate_limits and only trust the one
        # that contains our unique probe marker.
        for session_file in sorted(jsonl_files, key=lambda f: f.stat().st_mtime, reverse=True):
            with open(session_file, 'r') as f:
                session_text = f.read()

            if probe_marker not in session_text:
                continue

            lines = session_text.splitlines()

            for line in reversed(lines):
                if not line.strip():
                    continue
                try:
                    event = json.loads(line)
                    if (event.get('payload', {}).get('type') == 'token_count' and
                        event.get('payload', {}).get('rate_limits')):
                        limits = event['payload']['rate_limits']
                        _save_quota_cache(name, limits, meta={
                            'source': 'codex_accounts_quota_probe',
                            'source_session': str(session_file),
                            'source_account_snapshot': str(source),
                            'identity_email': source_email,
                            'account_id': source_account_id,
                            'probe_marker': probe_marker,
                        })
                        return limits
                except json.JSONDecodeError:
                    continue
    
    # No fresh rate_limits - try cached data
    cached = _load_quota_cache(name)
    if cached:
        return cached
    
    return None


def _normalize_rate_limits(limits):
    """Normalize Codex rate limits across one-window and two-window accounts.

    Some accounts expose both:
    - primary: short window (for example 300 minutes)
    - secondary: long window (for example 10080 minutes)

    But others expose only one long window and set `secondary = None`.
    For account-picking we care about the longest available window, because it
    is the more durable quota rail. This helper returns:
    - `primary`: shortest available window, or None
    - `secondary`: longest available window, or None
    """
    if not isinstance(limits, dict):
        return None

    windows = []
    for slot in ("primary", "secondary"):
        entry = limits.get(slot)
        if not isinstance(entry, dict):
            continue
        used_percent = entry.get("used_percent")
        if not isinstance(used_percent, (int, float)):
            continue
        window_minutes = entry.get("window_minutes")
        if not isinstance(window_minutes, (int, float)):
            window_minutes = float("inf")
        windows.append(
            {
                "slot": slot,
                "used_percent": float(used_percent),
                "window_minutes": float(window_minutes),
                "resets_at": entry.get("resets_at", 0),
                "raw": entry,
            }
        )

    if not windows:
        return None

    shortest = min(windows, key=lambda x: x["window_minutes"])
    longest = max(windows, key=lambda x: x["window_minutes"])

    primary = shortest["raw"]
    secondary = longest["raw"]
    if len(windows) == 1:
        primary = None

    return {
        "primary": primary,
        "secondary": secondary,
    }


def _collect_quota_results(accounts, json_mode=False):
    """Collect quota data for accounts without deciding whether to switch."""
    import time

    now = int(time.time())
    results = {}

    for account in accounts:
        name = account["name"]
        if not json_mode:
            print(f"  → {name}...", end=" ", flush=True)

        limits = _get_quota_for_account(name, account["path"])
        normalized = _normalize_rate_limits(limits)

        if normalized and normalized['secondary']:
            weekly = normalized['secondary']
            daily = normalized['primary'] or {}
            weekly_pct = float(weekly.get('used_percent', 100.0))
            daily_pct = daily.get('used_percent')
            weekly_resets_at = weekly.get('resets_at', 0)
            daily_resets_at = daily.get('resets_at', 0)

            effective_weekly_pct = 0 if now >= weekly_resets_at else weekly_pct
            effective_daily_pct = None
            daily_available = 100.0
            if isinstance(daily_pct, (int, float)):
                effective_daily_pct = 0 if now >= daily_resets_at else float(daily_pct)
                daily_available = max(0.0, 100.0 - effective_daily_pct)
            weekly_available = max(0.0, 100.0 - effective_weekly_pct)
            overall_available = min(weekly_available, daily_available)

            results[name] = {
                'weekly_used': weekly_pct,
                'weekly_resets_at': weekly_resets_at,
                'effective_weekly_used': effective_weekly_pct,
                'daily_used': daily_pct,
                'daily_resets_at': daily_resets_at,
                'effective_daily_used': effective_daily_pct,
                'weekly_available': weekly_available,
                'daily_available': daily_available,
                'available': overall_available,
            }
            if not json_mode:
                if effective_daily_pct is not None and effective_daily_pct >= 100.0:
                    print(f"weekly {weekly_pct:.0f}% used, daily LIMIT HIT")
                elif effective_weekly_pct < weekly_pct:
                    print(f"weekly {weekly_pct:.0f}% used → RESET (now 0%)")
                else:
                    print(f"weekly {weekly_pct:.0f}% used")
        else:
            results[name] = {'error': 'could not get quota'}
            if not json_mode:
                print("❌ failed")

    valid = {k: v for k, v in results.items() if 'available' in v}
    return results, valid

def cmd_auto(json_mode=False):
    """Switch to the account with the most quota available."""
    ensure_dirs()
    
    accounts = _build_account_catalog()
    if not accounts:
        if json_mode:
            print('{"error": "No accounts found"}')
        else:
            print("❌ No accounts found")
        return
    
    # Save current account to restore if needed
    active = resolve_active_profile()
    original_account = active[0] if active else None
    
    if not json_mode:
        print(f"🔄 Checking quota for {len(accounts)} account(s)...\n")
    
    results, valid = _collect_quota_results(accounts, json_mode=json_mode)
    
    if not valid:
        if original_account:
            shutil.copy(ACCOUNTS_DIR / f"{original_account}.json", AUTH_FILE)
        if json_mode:
            print(json.dumps({"error": "No valid quota data", "results": results}))
        else:
            print("\n❌ Could not get quota for any account")
        return
    
    # Sort by: 1) lowest effective usage, 2) earliest reset time (if both at 100%)
    def sort_key(k):
        v = valid[k]
        resets_at = min(
            v.get('daily_resets_at', float('inf')) or float('inf'),
            v.get('weekly_resets_at', float('inf')) or float('inf'),
        )
        return (
            -float(v.get('available', 0.0)),
            float(v.get('effective_daily_used', 0.0) or 0.0),
            float(v.get('effective_weekly_used', 0.0) or 0.0),
            resets_at,
        )
    
    best = min(valid.keys(), key=sort_key)
    best_path = next((account["path"] for account in accounts if account["name"] == best), None)
    
    # Check if already on best account
    already_active = (original_account == best)
    
    # Quota collection temporarily swaps AUTH_FILE through multiple snapshots.
    # Always restore the selected best account at the end, even if it was
    # already the logical winner before the scan; otherwise AUTH_FILE can stay
    # on the last tested slot and silently poison downstream runtime.
    if best_path is not None:
        shutil.copy(best_path, AUTH_FILE)
    
    if json_mode:
        print(json.dumps({
            "switched_to": best,
            "already_active": already_active,
            "weekly_used": valid[best]['weekly_used'],
            "effective_weekly_used": valid[best]['effective_weekly_used'],
            "weekly_resets_at": valid[best].get('weekly_resets_at'),
            "daily_used": valid[best].get('daily_used'),
            "effective_daily_used": valid[best].get('effective_daily_used'),
            "daily_resets_at": valid[best].get('daily_resets_at'),
            "available": valid[best]['available'],
            "all_accounts": results
        }, indent=2))
    else:
        from datetime import datetime
        if already_active:
            print(f"\n✅ Already on best account: {best}")
        else:
            print(f"\n✅ Switched to: {best}")
        
        effective = valid[best]['effective_weekly_used']
        actual = valid[best]['weekly_used']
        if effective < actual:
            print(f"   Weekly quota: RESET (was {actual:.0f}%, now fresh)")
        else:
            print(f"   Weekly quota: {actual:.0f}% used ({valid[best]['available']:.0f}% available)")
        daily_used = valid[best].get('effective_daily_used')
        if isinstance(daily_used, (int, float)):
            print(f"   Daily quota: {daily_used:.0f}% used")
        
        # Show comparison
        print(f"\nAll accounts:")
        for name, data in sorted(results.items(), key=lambda x: (x[1].get('effective_weekly_used', 999), x[1].get('weekly_resets_at', float('inf')))):
            if 'error' in data:
                print(f"   {name}: {data['error']}")
            else:
                marker = " ←" if name == best else ""
                eff = data['effective_weekly_used']
                act = data['weekly_used']
                resets_at = data.get('weekly_resets_at', 0)
                
                if eff < act:
                    reset_dt = datetime.fromtimestamp(resets_at).strftime("%b %d %H:%M")
                    print(f"   {name}: RESET (was {act:.0f}%, reset at {reset_dt}){marker}")
                else:
                    reset_dt = datetime.fromtimestamp(resets_at).strftime("%b %d %H:%M")
                    print(f"   {name}: {act:.0f}% used (resets {reset_dt}){marker}")


def cmd_quota(json_mode=False):
    """Collect quota snapshot without changing the active account."""
    ensure_dirs()

    accounts = _build_account_catalog()
    if not accounts:
        if json_mode:
            print('{"error": "No accounts found"}')
        else:
            print("❌ No accounts found")
        return

    original_bytes = AUTH_FILE.read_bytes() if AUTH_FILE.exists() else None

    if not json_mode:
        print(f"🔄 Checking quota for {len(accounts)} account(s)...\n")

    try:
        results, valid = _collect_quota_results(accounts, json_mode=json_mode)
    finally:
        if original_bytes is not None:
            AUTH_FILE.write_bytes(original_bytes)

    if json_mode:
        best = None
        if valid:
            def quota_sort_key(k):
                v = valid[k]
                resets_at = min(
                    v.get('daily_resets_at', float('inf')) or float('inf'),
                    v.get('weekly_resets_at', float('inf')) or float('inf'),
                )
                return (
                    -float(v.get('available', 0.0)),
                    float(v.get('effective_daily_used', 0.0) or 0.0),
                    float(v.get('effective_weekly_used', 0.0) or 0.0),
                    resets_at,
                )
            best = min(valid.keys(), key=quota_sort_key)
        print(json.dumps({
            "active_account_preserved": True,
            "best_account": best,
            "all_accounts": results
        }, indent=2))
        return

    if not valid:
        print("\n❌ Could not get quota for any account")
        return

    print("\n✅ Quota snapshot collected without switching active account")

def cmd_use(name):
    ensure_dirs()
    source = _resolve_account_path(name)
    
    if source is None or not source.exists():
        print(f"❌ Account '{name}' not found.")
        print("Available accounts:")
        for account in _build_account_catalog():
            print(f" - {account['name']}")
        return
    
    # Backup current if it's not saved? 
    # Maybe risky to overwrite silently, but that's what a switcher does.
    
    shutil.copy2(source, AUTH_FILE)
    info = get_account_info(source)
    print(f"✅ Switched to account: {name} ({info.get('email')})")

def get_token_email(auth_path) -> str:
    """Extract email from a token file."""
    info = get_account_info(auth_path) or {}
    return _canonical_email(info.get("email")) or ""


def get_token_account_id(auth_path) -> str:
    info = get_account_info(auth_path) or {}
    account_id = info.get("account_id")
    return account_id.strip() if isinstance(account_id, str) else ""


def safe_save_token(source_path: Path, target_path: Path, force: bool = False) -> tuple[bool, str]:
    """Safely save a token file, preventing overwrites with different users.
    
    Returns (success, message).
    """
    if not source_path.exists():
        return False, "Source token file does not exist"
    
    source_email = get_token_email(source_path)
    if not source_email or source_email in ("unknown", "error"):
        return False, "Could not determine email from source token"
    source_account_id = get_token_account_id(source_path)
    
    # If target exists, verify emails match
    if target_path.exists():
        target_email = get_token_email(target_path)
        if target_email and target_email not in ("unknown", "error"):
            if source_email != target_email:
                if not force:
                    return False, f"Refusing to overwrite: target has {target_email}, source has {source_email}"
                # Force mode: warn but proceed
                print(f"⚠️  Warning: overwriting {target_email} with {source_email} (--force)")
        target_account_id = get_token_account_id(target_path)
        if source_account_id and target_account_id and source_account_id != target_account_id:
            if not force:
                return (
                    False,
                    "Refusing to overwrite: "
                    f"target account_id={_short_account_id(target_account_id)} "
                    f"source account_id={_short_account_id(source_account_id)}",
                )
            print(
                "⚠️  Warning: overwriting "
                f"account_id={_short_account_id(target_account_id)} with "
                f"account_id={_short_account_id(source_account_id)} (--force)"
            )
    
    shutil.copy2(source_path, target_path)
    return True, f"Saved token for {source_email}"


def cmd_save(name: str, force: bool = False):
    """Save the current auth.json to a named account, with safety check."""
    ensure_dirs()
    
    if not AUTH_FILE.exists():
        print("❌ No current auth.json to save")
        return
    
    target = ACCOUNTS_DIR / f"{name}.json"
    success, message = safe_save_token(AUTH_FILE, target, force=force)
    
    if success:
        print(f"✅ {message} as '{name}'")
    else:
        print(f"❌ {message}")


def sync_current_login_to_snapshot() -> None:
    """Persist the CURRENT ~/.codex/auth.json back into the matching named snapshot.

    This makes snapshots behave like "last known good refreshed token state".

    Rules:
    - If the current login matches an existing email+account_id snapshot, refresh that slot.
    - If the same email has multiple underlying account_ids, use `email--shortid` naming.
    - NEVER overwrite a snapshot with a different user's token or different account_id.

    This runs silently (no prints) because it's executed on every invocation.
    """
    try:
        ensure_dirs()
        if not AUTH_FILE.exists():
            return

        info = get_account_info(AUTH_FILE) or {}
        email = _canonical_email(info.get("email"))
        account_id = info.get("account_id") if isinstance(info, dict) else None
        if not email:
            return

        target_name = _canonical_snapshot_name(email, account_id, fallback=email)
        target = ACCOUNTS_DIR / f"{target_name}.json"
        if target.exists():
            if not is_current(target):
                safe_save_token(AUTH_FILE, target, force=False)
            return

        match = _resolve_matching_account(email, account_id)
        if match is not None and match.exists():
            if not is_current(match):
                safe_save_token(AUTH_FILE, match, force=False)
            if match != target:
                safe_save_token(AUTH_FILE, target, force=False)
            return

        shutil.copy2(AUTH_FILE, target)
    except Exception:
        # Never fail the command because of sync.
        return


def main():
    parser = argparse.ArgumentParser(description="Codex Account Switcher")
    subparsers = parser.add_subparsers(dest="command")

    list_parser = subparsers.add_parser("list", help="List saved accounts")
    list_parser.add_argument(
        "--verbose",
        action="store_true",
        help="Show extra diagnostics (refresh age + token TTL)",
    )
    list_parser.add_argument(
        "--json",
        action="store_true",
        help="Output verbose information as JSON",
    )

    add_parser = subparsers.add_parser("add", help="Run a fresh login and save as an account")
    add_parser.add_argument(
        "--name",
        help="Optional explicit snapshot slot. If omitted, uses canonical email/account_id naming.",
    )

    use_parser = subparsers.add_parser("use", help="Switch to an account")
    use_parser.add_argument("name", help="Name of the account to switch to")

    save_parser = subparsers.add_parser("save", help="Save current token to a named account")
    save_parser.add_argument("name", help="Name to save the account as")
    save_parser.add_argument("--force", action="store_true", help="Force overwrite even if emails don't match")

    auto_parser = subparsers.add_parser("auto", help="Switch to the account with most quota available")
    auto_parser.add_argument("--json", action="store_true", help="Output as JSON")

    quota_parser = subparsers.add_parser("quota", help="Collect quota snapshot without switching active account")
    quota_parser.add_argument("--json", action="store_true", help="Output as JSON")

    args = parser.parse_args()

    # Always persist the currently active login back into its named snapshot.
    sync_current_login_to_snapshot()
    ensure_canonical_snapshot_files()

    if args.command == "add":
        cmd_add(name_override=getattr(args, "name", None))
    elif args.command == "use":
        cmd_use(args.name)
    elif args.command == "save":
        cmd_save(args.name, force=bool(getattr(args, "force", False)))
    elif args.command == "auto":
        cmd_auto(json_mode=bool(getattr(args, "json", False)))
    elif args.command == "quota":
        cmd_quota(json_mode=bool(getattr(args, "json", False)))
    else:
        cmd_list(
            verbose=bool(getattr(args, "verbose", False)),
            json_mode=bool(getattr(args, "json", False)),
        )

if __name__ == "__main__":
    main()
