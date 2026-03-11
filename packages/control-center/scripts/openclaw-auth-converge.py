#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path


DEFAULT_AGENTS = ["main"]


def load_store(path: Path) -> dict:
    if not path.exists():
        return {"version": 1, "profiles": {}}
    raw = json.loads(path.read_text())
    if not isinstance(raw, dict):
        return {"version": 1, "profiles": {}}
    raw.setdefault("profiles", {})
    return raw


def save_store(path: Path, doc: dict) -> None:
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n")


def as_dict(value) -> dict:
    return value if isinstance(value, dict) else {}


def as_int(value) -> int:
    try:
        return int(value)
    except Exception:
        return 0


def provider_of(profile: dict) -> str:
    provider = profile.get("provider")
    return provider.strip().lower() if isinstance(provider, str) else ""


def is_oauth(profile: dict) -> bool:
    return profile.get("type") == "oauth"


def profile_score(profile: dict) -> tuple[int, int, int]:
    expires = as_int(profile.get("expires"))
    access_len = len(str(profile.get("access") or ""))
    refresh_len = len(str(profile.get("refresh") or ""))
    return (expires, access_len, refresh_len)


def pick_better(current: dict | None, candidate: dict) -> bool:
    if current is None:
        return True
    return profile_score(candidate) >= profile_score(current)


def dedupe(values: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, str):
            continue
        item = value.strip()
        if not item or item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out


def usage_score(stats: dict) -> int:
    return max(as_int(stats.get("lastUsed")), as_int(stats.get("lastFailureAt")))


def sanitize_usage(profile_id: str, stats: dict, profiles: dict[str, dict]) -> dict:
    if not isinstance(stats, dict):
        return {}
    profile = profiles.get(profile_id) or {}
    clean: dict = {}
    if "lastUsed" in stats:
        clean["lastUsed"] = as_int(stats.get("lastUsed"))
    if "lastFailureAt" in stats:
        clean["lastFailureAt"] = as_int(stats.get("lastFailureAt"))
    if not is_oauth(profile):
        for key in ("errorCount", "failureCounts", "cooldownUntil"):
            if key in stats:
                clean[key] = stats[key]
    return {k: v for k, v in clean.items() if v not in ({}, None)}


def build_main_store(docs: dict[str, dict]) -> tuple[dict, set[str], set[str]]:
    merged_profiles: dict[str, dict] = {}
    version = 1
    for agent, doc in docs.items():
        version = max(version, as_int(doc.get("version")) or 1)
        for profile_id, profile in as_dict(doc.get("profiles")).items():
            if not isinstance(profile_id, str) or not isinstance(profile, dict):
                continue
            current = merged_profiles.get(profile_id)
            if pick_better(current, profile):
                merged_profiles[profile_id] = dict(profile)

    oauth_profile_ids = {
        profile_id
        for profile_id, profile in merged_profiles.items()
        if is_oauth(profile)
    }
    shared_oauth_providers = {
        provider_of(profile)
        for profile in merged_profiles.values()
        if is_oauth(profile)
    }

    provider_order: dict[str, list[str]] = {}
    for doc in docs.values():
        for provider, values in as_dict(doc.get("order")).items():
            if not isinstance(values, list):
                continue
            bucket = provider_order.setdefault(str(provider), [])
            bucket.extend(values)

    merged_order: dict[str, list[str]] = {}
    for provider, values in provider_order.items():
        available = [
            profile_id
            for profile_id, profile in merged_profiles.items()
            if provider_of(profile) == provider
        ]
        ordered = [value for value in dedupe(values) if value in available]
        for profile_id in sorted(available):
            if profile_id not in ordered:
                ordered.append(profile_id)
        if ordered:
            merged_order[provider] = ordered

    merged_last_good: dict[str, str] = {}
    providers = set(merged_order)
    providers.update(provider_of(profile) for profile in merged_profiles.values())
    for provider in sorted(p for p in providers if p):
        for doc in docs.values():
            candidate = as_dict(doc.get("lastGood")).get(provider)
            if isinstance(candidate, str) and candidate in merged_profiles:
                merged_last_good[provider] = candidate
                break
        if provider not in merged_last_good and merged_order.get(provider):
            merged_last_good[provider] = merged_order[provider][0]

    merged_usage: dict[str, dict] = {}
    for doc in docs.values():
        for profile_id, stats in as_dict(doc.get("usageStats")).items():
            if profile_id not in merged_profiles or not isinstance(stats, dict):
                continue
            current = merged_usage.get(profile_id)
            if current is None or usage_score(stats) >= usage_score(current):
                merged_usage[profile_id] = dict(stats)
    merged_usage = {
        profile_id: sanitize_usage(profile_id, stats, merged_profiles)
        for profile_id, stats in merged_usage.items()
    }
    merged_usage = {
        profile_id: stats
        for profile_id, stats in merged_usage.items()
        if stats
    }

    main_doc = {
        "version": version,
        "profiles": merged_profiles,
    }
    if merged_order:
        main_doc["order"] = merged_order
    if merged_last_good:
        main_doc["lastGood"] = merged_last_good
    if merged_usage:
        main_doc["usageStats"] = merged_usage
    return main_doc, oauth_profile_ids, shared_oauth_providers


def strip_secondary_store(doc: dict, oauth_profile_ids: set[str], shared_oauth_providers: set[str]) -> dict:
    cleaned = {
        "version": as_int(doc.get("version")) or 1,
        "profiles": {},
    }

    for profile_id, profile in as_dict(doc.get("profiles")).items():
        if not isinstance(profile_id, str) or not isinstance(profile, dict):
            continue
        if profile_id in oauth_profile_ids:
            continue
        cleaned["profiles"][profile_id] = profile

    order = {}
    for provider, values in as_dict(doc.get("order")).items():
        provider_key = str(provider).strip().lower()
        if provider_key in shared_oauth_providers:
            continue
        if isinstance(values, list):
            kept = [value for value in dedupe(values) if value in cleaned["profiles"]]
            if kept:
                order[provider_key] = kept
    if order:
        cleaned["order"] = order

    last_good = {}
    for provider, profile_id in as_dict(doc.get("lastGood")).items():
        provider_key = str(provider).strip().lower()
        if provider_key in shared_oauth_providers:
            continue
        if isinstance(profile_id, str) and profile_id in cleaned["profiles"]:
            last_good[provider_key] = profile_id
    if last_good:
        cleaned["lastGood"] = last_good

    usage = {}
    for profile_id, stats in as_dict(doc.get("usageStats")).items():
        if profile_id not in cleaned["profiles"]:
            continue
        clean = sanitize_usage(profile_id, stats, cleaned["profiles"])
        if clean:
            usage[profile_id] = clean
    if usage:
        cleaned["usageStats"] = usage

    return cleaned


def main() -> int:
    agents = sys.argv[1:] or DEFAULT_AGENTS
    root = Path(os.environ.get("OPENCLAW_HOME", str(Path.home() / ".openclaw"))) / "agents"
    docs: dict[str, dict] = {}
    paths: dict[str, Path] = {}

    for agent in agents:
        path = root / agent / "agent" / "auth-profiles.json"
        paths[agent] = path
        docs[agent] = load_store(path)

    if "main" not in docs:
        print("missing main agent store", file=sys.stderr)
        return 1

    main_doc, oauth_profile_ids, shared_oauth_providers = build_main_store(docs)
    save_store(paths["main"], main_doc)
    print(
        f"[main] converged profiles={len(main_doc['profiles'])} "
        f"shared_oauth={len(oauth_profile_ids)}"
    )

    for agent in agents:
        if agent == "main":
            continue
        cleaned = strip_secondary_store(docs[agent], oauth_profile_ids, shared_oauth_providers)
        save_store(paths[agent], cleaned)
        print(
            f"[{agent}] inherited shared oauth from main; "
            f"local_profiles={len(cleaned['profiles'])}"
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
