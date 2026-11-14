"""Offline behavioral tests: python3 tests/test_account_deduplication.py.

Every cookie/identity is synthetic, every store lives in a temporary directory.
No config import, production account file, network request or browser is needed.
"""
import copy
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from muse2api.account_identity import (AccountIdentityConflict, identity_from_session,
                              normalize_identity, verified_email_identity)
from muse2api.store import Store, VML_TTL


def user(uid="user-fixture", email=None):
    value = {"verified": True, "source": "session", "user_id": uid}
    if email:
        value["email"] = email
    return normalize_identity(value)


class IdentityTests(unittest.TestCase):
    def test_real_native_auth_field_and_secret_projection(self):
        result = identity_from_session({"auth_meta_session": {
            "frl_account_id": "native-user-fixture", "frl_access_token": "NEVER-PERSIST"}})
        self.assertEqual(result["user_id"], "native-user-fixture")
        self.assertNotIn("NEVER-PERSIST", json.dumps(result))
        self.assertIsNone(identity_from_session({"auth_meta_session": {
            "frl_account_id": "native-user-fixture"}}, verified=False))

    def test_vm_device_label_generic_ids_and_unverified_email_are_not_identity(self):
        payload = {"status": "assigned", "vm_id": "shared-vm", "id": "generic",
                   "device_id": "shared-device", "label": "same", "email": "fake@example.test",
                   "user_id": "unobserved-schema", "account": {"id": "generic"}}
        self.assertIsNone(identity_from_session(payload))
        for value in ({"verified": False, "email": "x@example.test"},
                      {"verified": True, "vm_id": "vm"}, {"verified": True, "device_id": "device"},
                      {"verified": True, "user_id": True}):
            with self.assertRaises(ValueError):
                normalize_identity(value)

    def test_otp_email_normalizes_case_but_not_aliases(self):
        self.assertEqual(verified_email_identity(" Person+one@Example.TEST ")["email"],
                         "person+one@example.test")
        self.assertNotEqual(verified_email_identity("person+one@example.test"),
                            verified_email_identity("person@example.test"))
        with self.assertRaises(ValueError):
            verified_email_identity("not a mailbox")


class StoreIdentityTests(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.TemporaryDirectory(prefix="muse-account-identity-")
        self.addCleanup(self.home.cleanup)
        self.cfg = SimpleNamespace(accounts_file=os.path.join(self.home.name, "accounts.json"),
                                   tasks_file=os.path.join(self.home.name, "tasks.json"))
        self.store = Store(self.cfg)

    def seed(self, values):
        with open(self.cfg.accounts_file, "w", encoding="utf-8") as f:
            json.dump(values, f, separators=(",", ":"))
        return Path(self.cfg.accounts_file).read_bytes()

    def legacy(self, aid, sess, created, **kw):
        return {"id": aid, "cookies": {"hatch_sess": sess}, "cookies_exp": {},
                "label": aid, "enabled": True, "created_at": created,
                "expires_at": created + VML_TTL, "expiry_anchor": created,
                "last_used": None, "last_keepalive": None, "use_count": 0,
                "ok": None, "note": "", "synced_at": None, **kw}

    def test_reimport_matches_accounts_not_tasks_and_preserves_state_and_object(self):
        with patch("muse2api.store.time.time", return_value=1000):
            account = self.store.add_account({"hatch_sess": "same", "hatch_vml": "vml"}, "old",
                                             {"hatch_vml": 9999})
        self.store.update_account(account["id"], enabled=False, use_count=7, last_used=123,
                                  proxy={"server": "http://proxy.test:80", "password": "fixture"})
        task = self.store.create_task("account", "not-an-account")
        self.store.update_task(task["id"], cookies={"hatch_sess": "same"})
        with patch("muse2api.store.time.time", return_value=2000):
            found = self.store.add_account({"hatch_sess": "same"}, "new")
        self.assertIs(found, account)
        self.assertEqual(len(self.store.accounts), 1)
        self.assertFalse(found["enabled"])
        self.assertEqual((found["created_at"], found["last_used"], found["use_count"]), (1000, 123, 7))
        self.assertEqual(found["label"], "new")
        self.assertEqual(found["expires_at"], 9999)
        self.assertEqual(found["proxy"]["password"], "fixture")
        self.assertEqual(self.store.get_task(task["id"])["prompt"], "not-an-account")
        self.assertEqual(Store(self.cfg).get_account(found["id"])["expires_at"], 9999)

    def test_concurrent_exact_import_is_one_account(self):
        with ThreadPoolExecutor(max_workers=12) as pool:
            ids = list(pool.map(lambda _: self.store.add_account({"hatch_sess": "shared-fixture"})["id"],
                                range(48)))
        self.assertEqual(len(set(ids)), 1)
        self.assertEqual(len(Store(self.cfg).accounts), 1)

    def test_labels_devices_vm_and_missing_session_do_not_merge(self):
        for sess in ("one", "two"):
            self.store.add_account({"hatch_sess": sess, "hatch_native_auth_device": "same-device",
                                    "hatch_vml": "same-vm"}, "same-label")
        self.store.add_account({}, "same-label")
        self.store.add_account({}, "same-label")
        self.assertEqual(len(self.store.accounts), 4)

    def test_verified_user_merges_cross_sessions_without_reenabling_or_counting_use(self):
        with patch("muse2api.store.time.time", return_value=1000):
            original = self.store.add_account({"hatch_sess": "old", "hatch_vml": "old-vml"},
                                              cookies_exp={"hatch_vml": 2000}, identity=user())
        self.store.update_account(original["id"], enabled=False, use_count=4, last_used=1200)
        with patch("muse2api.store.time.time", return_value=3000):
            imported = self.store.add_account({"hatch_sess": "new"}, identity=user())
        self.assertIs(imported, original)
        self.assertFalse(imported["enabled"])
        self.assertEqual(imported["cookies"], {"hatch_sess": "new"})
        self.assertEqual(imported["cookies_exp"], {})
        self.assertEqual(imported["created_at"], 1000)
        self.assertEqual(imported["expiry_anchor"], 3000)
        self.assertEqual(imported["expires_at"], 3000 + VML_TTL)
        self.assertEqual((imported["use_count"], imported["last_used"]), (4, 1200))
        self.store.touch_keepalive(imported["id"], True)
        self.assertEqual(imported["expires_at"], 3000 + VML_TTL)
        self.assertEqual(imported["last_used"], 1200)

    def test_verified_email_merges_only_same_mailbox(self):
        email = verified_email_identity("Person@Example.test")
        a = self.store.add_account({"hatch_sess": "a"}, identity=email)
        b = self.store.add_account({"hatch_sess": "b"}, identity=verified_email_identity("person@example.test"))
        self.assertEqual(a["id"], b["id"])
        self.store.add_account({"hatch_sess": "c"}, identity=verified_email_identity("person+tag@example.test"))
        self.assertEqual(len(self.store.accounts), 2)

    def test_same_session_conflicting_identity_is_atomic_and_secret_free(self):
        a = self.store.add_account({"hatch_sess": "cookie-MARKER"}, identity=user("first"))
        before = Path(self.cfg.accounts_file).read_bytes()
        with self.assertRaises(AccountIdentityConflict) as caught:
            self.store.add_account({"hatch_sess": "cookie-MARKER"}, identity=user("second"))
        self.assertNotIn("cookie-MARKER", str(caught.exception))
        self.assertNotIn("first", str(caught.exception))
        self.assertEqual(Path(self.cfg.accounts_file).read_bytes(), before)
        self.assertEqual(a["identity"]["user_id"], "first")

    def test_conflicting_user_ids_with_same_verified_email_do_not_merge(self):
        self.store.add_account({"hatch_sess": "a"}, identity=user("first", "shared@example.test"))
        before = Path(self.cfg.accounts_file).read_bytes()
        with self.assertRaises(AccountIdentityConflict):
            self.store.add_account({"hatch_sess": "b"}, identity=user("second", "shared@example.test"))
        self.assertEqual(Path(self.cfg.accounts_file).read_bytes(), before)

    def test_explicit_proxy_conflict_fails_closed_and_omission_preserves_route(self):
        proxy = {"server": "http://proxy.test:80", "username": "x", "password": "SECRET-MARKER"}
        a = self.store.add_account({"hatch_sess": "a"}, identity=user(), proxy=proxy)
        before = Path(self.cfg.accounts_file).read_bytes()
        for route in (None, {**proxy, "password": "different"}, {"server": "http://other.test:80"}):
            with self.assertRaises(AccountIdentityConflict) as caught:
                self.store.add_account({"hatch_sess": "b"}, identity=user(), proxy=route)
            self.assertNotIn("SECRET-MARKER", str(caught.exception))
            self.assertEqual(Path(self.cfg.accounts_file).read_bytes(), before)
        self.assertEqual(self.store.add_account({"hatch_sess": "b"}, identity=user())["proxy"], proxy)
        self.assertEqual(self.store.update_account(a["id"], proxy=None)["proxy"], None)

    def test_startup_migration_backup_aliases_counts_and_real_expiry(self):
        a = self.legacy("old", "same", 1000, enabled=False, use_count=3, last_used=1400,
                        cookies_exp={"hatch_vml": 9999}, cookies={"hatch_sess": "same", "hatch_vml": "vml"})
        b = self.legacy("duplicate", "same", 1200, use_count=5, last_used=1800, last_keepalive=1900,
                        synced_at=1900, cookies_exp={"hatch_vml": 9999},
                        cookies={"hatch_sess": "same", "hatch_vml": "vml"})
        raw = self.seed([a, b])
        store = Store(self.cfg)
        self.assertEqual(len(store.accounts), 1)
        account = store.get_account("duplicate")
        self.assertIs(account, store.get_account("old"))
        self.assertEqual((account["created_at"], account["last_used"], account["use_count"]), (1000, 1800, 8))
        self.assertFalse(account["enabled"])
        self.assertEqual(account["last_keepalive"], 1900)
        self.assertEqual(account["expires_at"], 9999)
        backup = Path(store.migration_report["backup"])
        self.assertEqual(backup.read_bytes(), raw)
        self.assertEqual(stat.S_IMODE(backup.stat().st_mode), 0o600)
        store.mark("duplicate", True, "checked")
        self.assertTrue(account["ok"])
        self.assertEqual(Store(self.cfg).resolve_account_id("duplicate"), "old")
        self.assertEqual(Store(self.cfg).accounts[0]["use_count"], 8)

    def test_verified_cross_session_migration_keeps_selected_session_anchor(self):
        a = self.legacy("old", "old-session", 1000, identity=user())
        b = self.legacy("new", "new-session", 2000, identity=user(), synced_at=2100,
                        expires_at=2000 + VML_TTL)
        self.seed([a, b])
        store = Store(self.cfg)
        self.assertEqual(store.accounts[0]["cookies"]["hatch_sess"], "new-session")
        with patch("muse2api.store.time.time", return_value=5000):
            store.touch_keepalive("new", True)
        self.assertEqual(store.accounts[0]["expires_at"], 2000 + VML_TTL)

    def test_conflicted_legacy_component_is_not_partially_merged(self):
        a = self.legacy("a", "shared", 1000, identity=user("one"))
        b = self.legacy("b", "shared", 1001)
        c = self.legacy("c", "shared", 1002, identity=user("two"))
        raw = self.seed([a, b, c])
        store = Store(self.cfg)
        self.assertEqual(len(store.accounts), 3)
        self.assertEqual(Path(self.cfg.accounts_file).read_bytes(), raw)
        self.assertEqual(len(store.migration_report["conflicts"]), 1)
        with self.assertRaises(AccountIdentityConflict):
            store.add_account({"hatch_sess": "shared"})

    def test_legacy_proxy_conflict_direct_vs_proxy_not_merged(self):
        a = self.legacy("a", "same", 1000)
        b = self.legacy("b", "same", 1001, proxy={"server": "http://proxy.test:80"})
        raw = self.seed([a, b])
        store = Store(self.cfg)
        self.assertEqual(len(store.accounts), 2)
        self.assertEqual(Path(self.cfg.accounts_file).read_bytes(), raw)

    def test_bind_identity_merges_alias_and_all_operations_resolve_old_id(self):
        a = self.store.add_account({"hatch_sess": "a"})
        b = self.store.add_account({"hatch_sess": "b"})
        original_id, alias = a["id"], b["id"]
        self.store.bind_identity(original_id, user())
        canonical = self.store.bind_identity(alias, user())
        self.assertIs(canonical, a)
        self.assertEqual(len(self.store.accounts), 1)
        self.assertEqual(self.store.resolve_account_id(alias), original_id)
        self.store.update_account(alias, label="renamed")
        self.store.mark(alias, False)
        self.assertFalse(a["ok"])
        self.store.touch_keepalive(alias, True)
        self.assertTrue(a["ok"])
        self.assertEqual(a["label"], "renamed")
        self.assertIsNone(self.store.pick_account(exclude_ids={alias}, record_use=False))
        self.assertIs(self.store.pick_account(preferred_id=alias, rotate=False, record_use=False), a)
        self.assertIsNone(a["last_used"])
        self.assertEqual(a["use_count"], 0)
        self.assertTrue(Store(self.cfg).delete_account(alias))
        self.assertEqual(len(Store(self.cfg).accounts), 0)

    def test_bind_conflict_preserves_memory_disk_and_no_count(self):
        a = self.store.add_account({"hatch_sess": "a"}, identity=user("one"))
        before = copy.deepcopy(a)
        raw = Path(self.cfg.accounts_file).read_bytes()
        with self.assertRaises(AccountIdentityConflict):
            self.store.bind_identity(a["id"], user("two"))
        self.assertEqual(a, before)
        self.assertEqual(Path(self.cfg.accounts_file).read_bytes(), raw)

    def test_manual_session_replacement_clears_identity_but_authenticated_rotation_keeps_it(self):
        a = self.store.add_account({"hatch_sess": "a", "hatch_vml": "same-vml"},
                                   cookies_exp={"hatch_vml": 9999999}, identity=user())
        with patch("muse2api.store.time.time", return_value=1000):
            self.store.update_account(a["id"], cookies={"hatch_sess": "rotated", "hatch_vml": "same-vml"},
                                      cookies_exp={}, verified_session=True)
        self.assertEqual(a["identity"], user())
        self.assertEqual(a["expires_at"], 9999999)
        self.assertNotIn("verified_session", a)
        self.store.update_account(a["id"], cookies={"hatch_sess": "manual"})
        self.assertNotIn("identity", a)
        self.assertNotIn("hatch_vml", a["cookies"])
        self.assertNotEqual(a["expires_at"], 9999999)

    def test_session_only_measured_expiry_survives_verified_rotation_without_extension(self):
        measured_expiry = 1900012345
        with patch("muse2api.store.time.time", return_value=1900000000):
            a = self.store.add_account({"hatch_sess": "session-only"},
                                       cookies_exp={"hatch_vml": measured_expiry}, identity=user())
        self.assertEqual(a["expires_at"], measured_expiry)
        self.assertNotIn("hatch_vml", a["cookies"])
        with patch("muse2api.store.time.time", return_value=1900003600):
            self.store.add_account({"hatch_sess": "session-only"}, cookies_exp={})
            self.store.update_account(a["id"], cookies={"hatch_sess": "rotated"},
                                      cookies_exp={}, verified_session=True)
            self.store.touch_keepalive(a["id"], True)
        self.assertEqual(a["cookies_exp"]["hatch_vml"], measured_expiry)
        self.assertEqual(a["expires_at"], measured_expiry)
        self.assertIsNone(a["expiry_anchor"])
        self.assertEqual(a["identity"], user())
        self.assertIsNone(a["last_used"])
        self.assertEqual(a["use_count"], 0)
        self.assertEqual(Store(self.cfg).get_account(a["id"])["expires_at"], measured_expiry)
        self.store.update_account(a["id"], cookies={"hatch_sess": "manual-new-session"})
        self.assertNotIn("hatch_vml", a["cookies_exp"])
        self.assertNotEqual(a["expires_at"], measured_expiry)

    def test_cookie_update_exact_session_merges_and_conflicting_identity_does_not(self):
        a = self.store.add_account({"hatch_sess": "a"})
        b = self.store.add_account({"hatch_sess": "b"})
        canonical = self.store.update_account(b["id"], cookies={"hatch_sess": "a"})
        self.assertIs(canonical, a)
        self.assertEqual(len(self.store.accounts), 1)
        self.assertEqual(self.store.resolve_account_id(b["id"]), a["id"])
        self.store.bind_identity(a["id"], user("one"))
        c = self.store.add_account({"hatch_sess": "c"}, identity=user("two"))
        before = Path(self.cfg.accounts_file).read_bytes()
        with self.assertRaises(AccountIdentityConflict):
            self.store.update_account(c["id"], cookies={"hatch_sess": "a"}, verified_session=True)
        self.assertEqual(Path(self.cfg.accounts_file).read_bytes(), before)

    def test_keepalive_reimport_and_empty_expiry_never_extend_estimated_lifetime(self):
        with patch("muse2api.store.time.time", return_value=1000):
            a = self.store.add_account({"hatch_sess": "a"})
        with patch("muse2api.store.time.time", return_value=2000):
            self.store.add_account({"hatch_sess": "a"}, cookies_exp={})
            self.store.update_account(a["id"], cookies_exp={})
            self.store.update_account(a["id"], cookies={"hatch_sess": "a"}, expiry_anchor=2000)
            self.store.pick_account(record_use=False)
            self.store.touch_keepalive(a["id"], True)
        self.assertEqual(a["expires_at"], 1000 + VML_TTL)
        self.assertIsNone(a["last_used"])
        self.assertEqual(a["use_count"], 0)
        self.store.pick_account()
        self.assertEqual(a["use_count"], 1)

    def test_public_view_has_no_cookies_proxy_password_or_legacy_logs(self):
        a = self.store.add_account({"hatch_sess": "COOKIE-MARKER"}, identity=user(),
                                   proxy={"server": "http://proxy.test:80", "password": "PASSWORD-MARKER"})
        a["logs"] = [{"msg": "OLD-RAW-SECRET"}]
        public = self.store.list_accounts()[0]
        output = json.dumps(public)
        for secret in ("COOKIE-MARKER", "PASSWORD-MARKER", "OLD-RAW-SECRET"):
            self.assertNotIn(secret, output)
        public["identity"]["user_id"] = "edited-copy"
        self.assertEqual(a["identity"]["user_id"], "user-fixture")
        self.assertFalse(hasattr(self.store, "log"))

    def test_failed_backup_or_write_does_not_mutate_accounts(self):
        a = self.store.add_account({"hatch_sess": "a"}, identity=user())
        b = self.store.add_account({"hatch_sess": "b"})
        before = copy.deepcopy(self.store.accounts)
        with patch.object(self.store, "_backup_accounts", side_effect=OSError("fixture")):
            with self.assertRaises(OSError):
                self.store.bind_identity(b["id"], user())
        self.assertEqual(self.store.accounts, before)
        with patch("muse2api.store._write", side_effect=OSError("fixture")):
            with self.assertRaises(OSError):
                self.store.add_account({"hatch_sess": "a"}, "must-not-change")
        self.assertEqual(a["label"], before[0]["label"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
