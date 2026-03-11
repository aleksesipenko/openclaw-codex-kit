#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
ALLOWED_ROOT_ENTRIES = {
    ".gitignore",
    "LICENSE",
    "README.md",
    "docs",
    "packages",
    "scripts",
    "templates",
}
FORBIDDEN_PATH_PARTS = {
    "__pycache__",
    "node_modules",
    "dist",
}
TEXT_EXTENSIONS = {
    ".json",
    ".md",
    ".py",
    ".sh",
    ".ts",
    ".js",
    ".txt",
    ".yml",
    ".yaml",
}
def joined(*parts: str) -> str:
    return "".join(parts)


STRING_PATTERNS = {
    "absolute_user_path": joined("/", "Users", "/"),
    "owner_handle": joined("alex", "esip"),
    "owner_id": joined("873", "529051"),
    "private_hostname": joined("tail", "fcc"),
    "private_workspace_path": "/".join(("workspace", "scripts", "ops")),
    "private_switcher_path": "/".join(("workspace", "skills", "codex-account-switcher")),
    "old_repo_hint": "/".join(("Projects", "openclaw-reliability-ops")),
}
REGEX_PATTERNS = {
    "telegram_bot_token": re.compile(r"\b\d{8,12}:[A-Za-z0-9_-]{30,}\b"),
    "openai_api_key": re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"),
    "groq_api_key": re.compile(r"\bgsk_[A-Za-z0-9]{20,}\b"),
    "private_key_block": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
}


def iter_files(root: Path):
    for path in root.rglob("*"):
        if any(part == ".git" for part in path.parts):
            continue
        if path.is_file():
            yield path


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit the public kit tree for private data and banned artifacts.")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    findings: list[dict[str, str]] = []

    for child in ROOT.iterdir():
        if child.name.startswith(".") and child.name != ".gitignore":
            continue
        if child.name not in ALLOWED_ROOT_ENTRIES:
            findings.append({"type": "unexpected_root_entry", "path": str(child.relative_to(ROOT))})

    for path in iter_files(ROOT):
        rel = path.relative_to(ROOT)
        if any(part in FORBIDDEN_PATH_PARTS for part in path.parts):
            findings.append({"type": "forbidden_artifact", "path": str(rel)})
            continue
        if ".bak" in path.name or path.suffix == ".pyc":
            findings.append({"type": "forbidden_artifact", "path": str(rel)})
            continue

        if path.suffix not in TEXT_EXTENSIONS and path.name not in {".gitignore"}:
            continue

        text = path.read_text(encoding="utf-8", errors="ignore")
        for label, needle in STRING_PATTERNS.items():
            if needle in text:
                findings.append({"type": label, "path": str(rel)})
        for label, pattern in REGEX_PATTERNS.items():
            if pattern.search(text):
                findings.append({"type": label, "path": str(rel)})

    payload = {"ok": not findings, "findings": findings}
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        if findings:
            for item in findings:
                print(f"{item['type']}: {item['path']}", file=sys.stderr)
        else:
            print("public safety audit: ok")

    return 0 if not findings else 1


if __name__ == "__main__":
    raise SystemExit(main())
