from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1] / "scripts"
LIB_PATH = ROOT / "codex_quota_snapshot_lib.py"


def load_module():
    spec = importlib.util.spec_from_file_location("codex_quota_snapshot_lib", LIB_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class QuotaSnapshotLibTests(unittest.TestCase):
    def setUp(self) -> None:
        self.module = load_module()
        self.tmp = tempfile.TemporaryDirectory()
        self.accounts_dir = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def write_account(self, stem: str, email: str, account_id: str) -> None:
        payload = {
            "email": email,
            "tokens": {
                "account_id": account_id,
            },
        }
        (self.accounts_dir / f"{stem}.json").write_text(json.dumps(payload))

    def write_quota(self, stem: str, email: str | None = None, account_id: str | None = None) -> None:
        payload = {
            "rate_limits": {
                "primary": {"used_percent": 10, "resets_at": 9999999999},
                "secondary": {"used_percent": 20, "resets_at": 9999999999},
            },
            "_meta": {},
        }
        if email is not None:
            payload["_meta"]["identity_email"] = email
        if account_id is not None:
            payload["_meta"]["account_id"] = account_id
        (self.accounts_dir / f".{stem}.quota.json").write_text(json.dumps(payload))

    def test_unique_email_keeps_generic_snapshot(self) -> None:
        self.write_account("solo@example.com", "solo@example.com", "solo-1234")

        stems = self.module.matching_snapshot_stems(self.accounts_dir, "solo@example.com", "solo-1234")

        self.assertIn("solo@example.com", stems)
        self.assertIn("solo@example.com--solo", stems)

    def test_multi_identity_email_drops_generic_snapshot(self) -> None:
        self.write_account("alex@example.com--aaaa1111", "alex@example.com", "aaaa1111-1111")
        self.write_account("alex@example.com--bbbb2222", "alex@example.com", "bbbb2222-2222")

        stems = self.module.matching_snapshot_stems(self.accounts_dir, "alex@example.com", "aaaa1111-1111")

        self.assertIn("alex@example.com--aaaa1111", stems)
        self.assertNotIn("alex@example.com", stems)

    def test_ambiguous_email_requires_exact_snapshot_with_account_meta(self) -> None:
        self.write_account("alex@example.com--aaaa1111", "alex@example.com", "aaaa1111-1111")
        self.write_account("alex@example.com--bbbb2222", "alex@example.com", "bbbb2222-2222")
        self.write_quota("alex@example.com--aaaa1111")

        path, reason = self.module.find_best_quota_file(
            self.accounts_dir,
            "alex@example.com",
            "alex@example.com--aaaa1111",
            "aaaa1111-1111",
        )

        self.assertIsNone(path)
        self.assertEqual(reason, "legacy_exact_quota_missing_account_id")

    def test_exact_snapshot_with_matching_account_meta_is_accepted(self) -> None:
        self.write_account("alex@example.com--aaaa1111", "alex@example.com", "aaaa1111-1111")
        self.write_account("alex@example.com--bbbb2222", "alex@example.com", "bbbb2222-2222")
        self.write_quota("alex@example.com--aaaa1111", email="alex@example.com", account_id="aaaa1111-1111")

        path, reason = self.module.find_best_quota_file(
            self.accounts_dir,
            "alex@example.com",
            "alex@example.com--aaaa1111",
            "aaaa1111-1111",
        )

        self.assertEqual(path, self.accounts_dir / ".alex@example.com--aaaa1111.quota.json")
        self.assertIsNone(reason)

    def test_account_mismatch_is_rejected(self) -> None:
        self.write_account("alex@example.com--aaaa1111", "alex@example.com", "aaaa1111-1111")
        self.write_quota("alex@example.com--aaaa1111", email="alex@example.com", account_id="wrong-9999")

        path, reason = self.module.find_best_quota_file(
            self.accounts_dir,
            "alex@example.com",
            "alex@example.com--aaaa1111",
            "aaaa1111-1111",
        )

        self.assertIsNone(path)
        self.assertEqual(reason, "quota_snapshot_account_id_mismatch")


if __name__ == "__main__":
    unittest.main()
