from __future__ import annotations

import base64
import glob
import json
from pathlib import Path


def load_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


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


def short_account_id(account_id: str | None) -> str | None:
    if not isinstance(account_id, str):
        return None
    value = account_id.strip().lower()
    if not value:
        return None
    return value.split("-", 1)[0][:8]


def read_identity_from_auth_data(data: dict | None) -> tuple[str | None, str | None]:
    tokens = data.get("tokens") if isinstance(data, dict) else None
    if not isinstance(tokens, dict):
        tokens = {}

    account_id = str(tokens.get("account_id") or "").strip() or None
    email = canonical_email(data.get("email")) if isinstance(data, dict) else None

    for token_name in ("id_token", "access_token"):
        payload = decode_jwt_payload(tokens.get(token_name))
        candidate = canonical_email(payload.get("email"))
        if not candidate:
            profile = payload.get("https://api.openai.com/profile")
            if isinstance(profile, dict):
                candidate = canonical_email(profile.get("email"))
        if candidate:
            email = candidate
            break

    return email, account_id


def identity_stem(email: str | None, account_id: str | None) -> str | None:
    canonical = canonical_email(email)
    short_id = short_account_id(account_id)
    if canonical and short_id:
        return f"{canonical}--{short_id}"
    return canonical or (f"account--{short_id}" if short_id else None)


def email_identity_ids(accounts_dir: Path, email: str) -> set[str]:
    identities: set[str] = set()
    canonical = canonical_email(email)
    if not canonical:
        return identities

    for path in sorted(accounts_dir.glob("*.json")):
        if path.name.startswith("."):
            continue
        data = load_json(path)
        candidate_email, account_id = read_identity_from_auth_data(data)
        if candidate_email != canonical:
            continue
        identities.add(account_id or path.stem)
    return identities


def matching_snapshot_stems(accounts_dir: Path, email: str, account_id: str) -> list[str]:
    canonical = canonical_email(email)
    normalized_account_id = str(account_id or "").strip()
    if not canonical or not normalized_account_id:
        return []

    stems: list[str] = []
    identities = email_identity_ids(accounts_dir, canonical)
    identities.add(normalized_account_id)

    for path in sorted(accounts_dir.glob("*.json")):
        if path.name.startswith("."):
            continue
        data = load_json(path)
        candidate_email, candidate_account_id = read_identity_from_auth_data(data)
        if candidate_email != canonical or candidate_account_id != normalized_account_id:
            continue
        stems.append(path.stem)

    exact_stem = identity_stem(canonical, normalized_account_id)
    if exact_stem and exact_stem not in stems:
        stems.append(exact_stem)

    if len(identities) <= 1 and canonical not in stems:
        stems.append(canonical)

    return sorted(dict.fromkeys(stems))


def build_quota_snapshot_payload(
    *,
    rate_limits: dict,
    session_path: Path,
    source: str,
    email: str,
    account_id: str,
    probe_marker: str | None = None,
) -> dict:
    meta = {
        "source": source,
        "source_session": str(session_path),
        "identity_email": canonical_email(email),
        "account_id": str(account_id or "").strip() or None,
    }
    if probe_marker:
        meta["probe_marker"] = probe_marker
    return {
        "rate_limits": rate_limits,
        "_meta": meta,
    }


def quota_snapshot_matches_identity(quota: dict | None, email: str, account_id: str | None) -> tuple[bool, str | None]:
    if not isinstance(quota, dict):
        return False, "invalid_quota_json"

    meta = quota.get("_meta")
    if not isinstance(meta, dict):
        return True, None

    expected_email = canonical_email(email)
    actual_email = canonical_email(meta.get("identity_email"))
    if actual_email and expected_email and actual_email != expected_email:
        return False, "quota_snapshot_email_mismatch"

    expected_account_id = str(account_id or "").strip() or None
    actual_account_id = str(meta.get("account_id") or "").strip() or None
    if actual_account_id and expected_account_id and actual_account_id != expected_account_id:
        return False, "quota_snapshot_account_id_mismatch"

    return True, None


def find_best_quota_file(
    quota_dir: Path,
    email: str,
    auth_stem: str,
    account_id: str | None = None,
) -> tuple[Path | None, str | None]:
    canonical = canonical_email(email)
    if not canonical:
        return None, "missing_email"

    exact = quota_dir / f".{auth_stem}.quota.json"
    ambiguous_email = len(email_identity_ids(quota_dir, canonical)) > 1

    def validate(path: Path, *, exact_match: bool) -> tuple[Path | None, str | None]:
        quota = load_json(path)
        valid, reason = quota_snapshot_matches_identity(quota, canonical, account_id)
        if not valid:
            return None, reason

        meta = quota.get("_meta") if isinstance(quota, dict) else None
        has_account_meta = isinstance(meta, dict) and bool(str(meta.get("account_id") or "").strip())

        if ambiguous_email and not exact_match:
            return None, "ambiguous_email_requires_exact_quota"
        if ambiguous_email and exact_match and not has_account_meta:
            return None, "legacy_exact_quota_missing_account_id"

        return path, None

    if exact.exists():
        return validate(exact, exact_match=True)

    if ambiguous_email:
        return None, "ambiguous_email_without_exact_quota"

    generic = quota_dir / f".{canonical}.quota.json"
    if generic.exists():
        return validate(generic, exact_match=False)

    pattern = str(quota_dir / f".{canonical}*.quota.json")
    matches = sorted(glob.glob(pattern), key=lambda candidate: Path(candidate).stat().st_mtime, reverse=True)
    for match in matches:
        path = Path(match)
        validated, reason = validate(path, exact_match=False)
        if validated is not None:
            return validated, None
        if reason not in {"invalid_quota_json", "quota_snapshot_email_mismatch", "quota_snapshot_account_id_mismatch"}:
            return None, reason

    return None, "quota_not_found"
