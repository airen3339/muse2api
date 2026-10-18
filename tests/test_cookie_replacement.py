"""Offline regression: replacing a login session must discard old session state."""
import copy
import os
from pathlib import Path
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch


class CookieReplacementTests(unittest.TestCase):
    now = 1900000000

    def setUp(self):
        from store import Store
        self.home = tempfile.TemporaryDirectory()
        self.addCleanup(self.home.cleanup)
        self.cfg = SimpleNamespace(
            accounts_file=self.home.name + "/accounts.json",
            tasks_file=self.home.name + "/tasks.json",
        )
        self.store = Store(self.cfg)
        self.enterContext(patch.object(self.app, "store", self.store))
        self.enterContext(patch("time.time", return_value=self.now))
        self.enterContext(patch.object(self.app.engine, "start",
                                      side_effect=AssertionError("unexpected browser")))
        self.acc = self.store.add_account(
            {"hatch_sess": "old-fixture", "hatch_vml": "old-vml"},
            label="synthetic account", cookies_exp={"hatch_vml": self.now - 100})
        self.acc.update(ok=False, note="old authentication failure",
                        checked_at=self.now - 100, last_keepalive=self.now - 200,
                        synced_at=self.now - 300, use_count=9, last_used=self.now - 400,
                        quota={"found": True, "weekly_used_pct": 90})

    def replace(self, payload):
        return self.app.update_cookies(self.acc["id"], payload)

    def test_new_header_discards_old_expiry_and_health(self):
        result = self.replace({"cookie_header": "hatch_sess=new-fixture; hatch_vml=new-vml"})
        self.assertTrue(result["ok"])  # Saving succeeds, validity is still untested.
        self.assertIsNone(self.acc["ok"])
        self.assertEqual(self.acc["cookies_exp"], {})
        self.assertEqual(self.acc["expires_at"], self.now + 2 * 86400)
        self.assertEqual(self.acc["expiry_anchor"], self.now)
        self.assertEqual(self.acc["cookies"]["hatch_sess"], "new-fixture")
        self.assertTrue(self.store.list_accounts()[0]["expires_estimated"])

    def test_explicit_expiry_belongs_to_new_session(self):
        expires = self.now + 3600
        self.replace({"cookies": {"hatch_sess": "new-fixture"},
                      "expires": {"hatch_vml": expires}})
        self.assertEqual(self.acc["expires_at"], expires)
        self.assertEqual(self.acc["cookies_exp"], {"hatch_vml": expires})
        self.assertIsNone(self.acc["expiry_anchor"])
        self.assertFalse(self.store.list_accounts()[0]["expires_estimated"])

    def test_session_results_are_cleared_but_account_settings_survive(self):
        self.acc["enabled"] = False
        before = copy.deepcopy(self.acc)
        self.replace({"cookies": {"hatch_sess": "new-fixture"}})
        for name in ("id", "label", "enabled", "created_at", "last_used", "use_count"):
            self.assertEqual(self.acc[name], before[name], name)
        for name in ("checked_at", "synced_at", "last_keepalive", "quota"):
            self.assertIsNone(self.acc.get(name), name)
        self.assertIsNone(self.acc["ok"])
        self.assertNotEqual(self.acc["note"], before["note"])

    def test_previously_healthy_session_is_not_assumed_valid(self):
        self.acc["ok"] = True
        self.replace({"cookies": {"hatch_sess": "new-fixture"}})
        self.assertIsNone(self.acc["ok"])

    def test_new_session_state_survives_reload(self):
        from store import Store
        self.replace({"cookies": {"hatch_sess": "new-fixture"}, "expires": {}})
        saved = Store(self.cfg).get_account(self.acc["id"])
        self.assertEqual(saved, self.acc)
        self.assertIsNone(saved["ok"])
        self.assertEqual(saved["expires_at"], self.now + 2 * 86400)

    def test_empty_replacement_does_not_modify_existing_account(self):
        before = copy.deepcopy(self.acc)
        with self.assertRaises(self.app.HTTPException) as error:
            self.replace({"cookies": {}})
        self.assertEqual(error.exception.status_code, 400)
        self.assertEqual(self.acc, before)

    def test_unknown_account_does_not_create_one(self):
        with self.assertRaises(self.app.HTTPException) as error:
            self.app.update_cookies("missing-fixture", {"cookies": {"hatch_sess": "new"}})
        self.assertEqual(error.exception.status_code, 404)
        self.assertEqual(len(self.store.accounts), 1)

    def test_routine_cookie_refresh_preserves_health_and_quota(self):
        before = copy.deepcopy(self.acc)
        self.store.update_account(self.acc["id"], cookies={"hatch_sess": "refreshed"})
        for name in ("ok", "note", "quota", "last_keepalive", "cookies_exp"):
            self.assertEqual(self.acc[name], before[name], name)


if __name__ == "__main__":
    root = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory() as home, patch.dict(os.environ, {
        "MUSE2API_HOME": home, "MUSE2API_PROFILE_ROOT": home, "MUSE2API_KEY": "test-only",
    }), patch("requests.sessions.Session.request", side_effect=AssertionError("unexpected network")):
        sys.path.insert(0, str(root))
        import app
        CookieReplacementTests.app = app
        unittest.main(verbosity=2)
