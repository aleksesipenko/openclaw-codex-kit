#!/usr/bin/env bash
set -euo pipefail

# Import current Codex CLI login (~/.codex/auth.json) into OpenClaw auth-profiles
# for selected agents.
#
# Usage:
#   codex-import-from-cli.sh [profile-id] [agent|all]
# Examples:
#   codex-import-from-cli.sh
#   codex-import-from-cli.sh openai-codex:acc-abcdef all
#   codex-import-from-cli.sh openai-codex:work-main main

PROFILE_ID="${1:-}"
TARGET="${2:-main}"

OPENCLAW_ROOT="${OPENCLAW_ROOT:-$HOME/.openclaw}"
CODEX_AUTH="${CODEX_AUTH:-$HOME/.codex/auth.json}"

if [[ ! -f "$CODEX_AUTH" ]]; then
  echo "codex auth file not found: $CODEX_AUTH" >&2
  exit 1
fi

if [[ "$TARGET" == "all" ]]; then
  mapfile -t AGENTS < <(find "$OPENCLAW_ROOT/agents" -mindepth 1 -maxdepth 1 -type d -exec basename {} \; 2>/dev/null | sort)
elif [[ "$TARGET" == *" "* ]]; then
  read -r -a AGENTS <<<"$TARGET"
else
  AGENTS=("$TARGET")
fi

python3 - "$PROFILE_ID" "$CODEX_AUTH" "$OPENCLAW_ROOT" "${AGENTS[@]}" <<'PY'
import base64
import json
import sys
import time
from pathlib import Path

profile_id = sys.argv[1]
codex_auth_path = Path(sys.argv[2])
openclaw_root = Path(sys.argv[3])
agents = sys.argv[4:]


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


def extract_email_from_token(token: str | None) -> str | None:
    payload = decode_jwt_payload(token)
    email = canonical_email(payload.get("email"))
    if email:
        return email
    profile = payload.get("https://api.openai.com/profile")
    if isinstance(profile, dict):
        return canonical_email(profile.get("email"))
    return None


def extract_email_from_profile(profile: dict) -> str | None:
    if not isinstance(profile, dict):
        return None
    direct = canonical_email(profile.get("email"))
    if direct:
        return direct
    return extract_email_from_token(profile.get("access"))


def short_account_id(account_id: str | None) -> str | None:
    if not isinstance(account_id, str):
        return None
    value = account_id.strip().lower()
    if not value:
        return None
    return value.split("-", 1)[0][:8]


def extract_account_id_from_profile(profile: dict) -> str | None:
    if not isinstance(profile, dict):
        return None
    account_id = profile.get("accountId")
    if isinstance(account_id, str) and account_id.strip():
        return account_id.strip()
    return None


def identity_key(email: str | None, account_id: str | None) -> str | None:
    canonical = canonical_email(email)
    if not canonical:
        return None
    return f"{canonical}|{(account_id or '').strip()}"


def canonical_profile_id(email: str, account_id: str | None) -> str:
    short_id = short_account_id(account_id)
    if short_id:
        return f"openai-codex:{email}--{short_id}"
    return f"openai-codex:{email}"


raw = json.loads(codex_auth_path.read_text())
tokens = raw.get("tokens") or {}
access = tokens.get("access_token")
refresh = tokens.get("refresh_token")
account_id = tokens.get("account_id")
id_token = tokens.get("id_token")

if not access or not refresh or not account_id:
    raise SystemExit("codex auth tokens are incomplete (need access/refresh/account_id)")

email = extract_email_from_token(id_token) or extract_email_from_token(access)
if not email:
    raise SystemExit("could not derive email from current codex auth")

expires_ms = None
payload = decode_jwt_payload(id_token) if id_token else {}
exp = payload.get("exp")
if isinstance(exp, (int, float)):
    expires_ms = int(exp * 1000)
if expires_ms is None:
    expires_ms = int((time.time() + 1800) * 1000)

plan_type = None
auth_claims = payload.get("https://api.openai.com/auth")
if isinstance(auth_claims, dict):
    raw_plan = auth_claims.get("chatgpt_plan_type")
    if isinstance(raw_plan, str) and raw_plan.strip():
        plan_type = raw_plan.strip()

profile_id = canonical_profile_id(email, account_id)
profile_obj = {
    "type": "oauth",
    "provider": "openai-codex",
    "access": access,
    "refresh": refresh,
    "expires": expires_ms,
    "accountId": account_id,
    "email": email,
}
if plan_type:
    profile_obj["planType"] = plan_type

for agent in agents:
    p = openclaw_root / "agents" / agent / "agent" / "auth-profiles.json"
    if not p.exists():
        print(f"[{agent}] skip: missing {p}")
        continue

    doc = json.loads(p.read_text())
    profiles = doc.setdefault("profiles", {})
    profiles[profile_id] = profile_obj

    legacy_to_canonical: dict[str, str] = {}
    canonical_codex: dict[str, dict] = {}
    canonical_by_identity: dict[str, str] = {}

    for pid, profile in list(profiles.items()):
        if not isinstance(pid, str) or not pid.startswith("openai-codex:"):
            continue
        if not isinstance(profile, dict):
            continue

        candidate_email = extract_email_from_profile(profile)
        if not candidate_email:
            continue
        candidate_account_id = extract_account_id_from_profile(profile)

        canonical_id = canonical_profile_id(candidate_email, candidate_account_id)
        legacy_to_canonical[pid] = canonical_id
        merged = dict(profile)
        merged["email"] = candidate_email

        key = identity_key(candidate_email, candidate_account_id)
        if key:
            previous_id = canonical_by_identity.get(key)
            if previous_id and previous_id != canonical_id:
                legacy_to_canonical[previous_id] = canonical_id
                canonical_codex.pop(previous_id, None)
            canonical_by_identity[key] = canonical_id

        existing = canonical_codex.get(canonical_id)
        candidate_expires = int(merged.get("expires") or 0)
        existing_expires = int((existing or {}).get("expires") or 0)
        keep_candidate = existing is None or candidate_expires >= existing_expires or pid == profile_id
        if keep_candidate:
            canonical_codex[canonical_id] = merged

    cleaned_profiles = {
        pid: profile
        for pid, profile in profiles.items()
        if not (isinstance(pid, str) and pid.startswith("openai-codex:"))
    }
    cleaned_profiles.update(canonical_codex)
    doc["profiles"] = cleaned_profiles

    order = doc.setdefault("order", {})
    old_order = list(order.get("openai-codex", []) or [])
    new_order: list[str] = []

    if profile_id in cleaned_profiles:
        new_order.append(profile_id)

    for pid in old_order:
        if not isinstance(pid, str):
            continue
        canonical_id = legacy_to_canonical.get(pid, pid)
        if canonical_id not in cleaned_profiles:
            continue
        if canonical_id in new_order:
            continue
        new_order.append(canonical_id)

    for pid in sorted(cleaned_profiles):
        if not pid.startswith("openai-codex:"):
            continue
        if pid not in new_order:
            new_order.append(pid)

    order["openai-codex"] = new_order

    last_good = doc.get("lastGood")
    if isinstance(last_good, dict):
        current_last_good = last_good.get("openai-codex")
        mapped_last_good = legacy_to_canonical.get(current_last_good, current_last_good)
        if mapped_last_good in cleaned_profiles:
            last_good["openai-codex"] = mapped_last_good
        else:
            last_good["openai-codex"] = profile_id

    p.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n")
    print(f"[{agent}] imported -> {profile_id} (deduped by email+account_id, order first)")

print(f"profile_id={profile_id}")
print(f"email={email}")
print(f"account_id={account_id}")
print(f"expires_ms={expires_ms}")
PY
