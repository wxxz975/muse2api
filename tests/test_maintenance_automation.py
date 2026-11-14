"""Integration regressions for actual maintenance, settings, login routes and task logs.
Run python3 tests/test_maintenance_automation.py (no upstream/browser/account needed).
"""
import asyncio
import copy
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
HOME = tempfile.TemporaryDirectory(prefix="muse-maintenance-tests-")
os.environ.update(MUSE2API_HOME=HOME.name, MUSE2API_PROFILE_ROOT=HOME.name, MUSE2API_KEY="test-only")
import app
from muse2api.config import Config
from muse2api.maintenance import MaintenanceSettings, MaintenanceSettingsStore, operation_due
from muse2api.diagnostics import Journal
from muse2api.cleanup import CleanupManager
from muse2api.store import Store


class MaintenanceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="muse-maint-")
        self.original = {key: getattr(app, key) for key in
                         ("store", "maintenance_settings", "journal", "cleanup_manager", "GEN_LOCK", "ACCOUNT_POOL", "KEEPALIVE_LOCK")}
        cfg = Config(base_dir=self.temp.name, profile_root=self.temp.name)
        cfg.ensure_dirs()
        app.store = Store(cfg)
        app.maintenance_settings = MaintenanceSettingsStore(str(Path(self.temp.name) / "maintenance.json"))
        app.journal = Journal(Path(self.temp.name) / "diagnostics.jsonl")
        app.cleanup_manager = CleanupManager(cfg.media_dir, Path(self.temp.name)/"cleanup.json",
                                             Path(self.temp.name)/"cleanup-state.json", app.journal)
        app.GEN_LOCK = app.ConcurrencyLock(1)
        app.ACCOUNT_POOL = app.AccountPoolManager(app.store)
        app.KEEPALIVE_LOCK = asyncio.Lock()
        self.a = app.store.add_account({"hatch_sess": "fixture-session"}, "fixture",
                                       cookies_exp={"hatch_vml": int(time.time()) + 3600})
        app.store.update_account(self.a["id"], last_used=1234, use_count=7)

    def tearDown(self):
        for key, value in self.original.items():
            setattr(app, key, value)
        self.temp.cleanup()

    def reply(self):
        return {"ok": True, "cookies": {"hatch_sess": "fixture-renewed"},
                "cookies_exp": {"hatch_vml": int(time.time()) + 7200},
                "vm_state": "RUNNING", "wake_ok": True}

    async def test_real_quota_method_and_real_expiry_not_business_use(self):
        old_keepalive = self.a.get("last_keepalive")
        with patch.object(app.engine, "renew_session_http", return_value=self.reply()), \
             patch.object(app.engine, "start"), \
             patch.object(app.engine, "quota", return_value={"found": True, "weekly_used_pct": 23}) as quota, \
             patch.object(app.engine, "close_current_page"), \
             patch.object(app.engine.__class__, "last_quota_cookies", new_callable=lambda: property(lambda _: {})):
            result = await asyncio.to_thread(app._query_quota_sync, self.a["id"], True)
        self.assertEqual(result["weekly_used_pct"], 23)
        quota.assert_called_once()
        saved = app.store.get_account(self.a["id"])
        self.assertEqual(saved["last_used"], 1234)
        self.assertEqual(saved["use_count"], 7)
        self.assertEqual(saved.get("last_keepalive"), old_keepalive)
        self.assertGreater(saved["expires_at"], int(time.time()) + 7100)
        self.assertEqual(saved["vm_state"], "RUNNING")
        self.assertIn("checked_at", saved["quota"])
        self.assertFalse(app.ACCOUNT_POOL.busy_accounts)
        self.assertFalse(app.GEN_LOCK.locked())

    async def test_quota_failure_preserves_valid_snapshot_and_diagnostic(self):
        old = {"found": True, "weekly_used_pct": 41, "checked_at": 12345}
        app.store.update_account(self.a["id"], quota=old)
        with patch.object(app.engine, "renew_session_http", return_value=self.reply()), \
             patch.object(app.engine, "start"), \
             patch.object(app.engine, "quota", side_effect=app.MuseGenerationError("额度面板未读到有效数据")), \
             patch.object(app.engine, "close_current_page"):
            with self.assertRaises(app.MuseGenerationError):
                await asyncio.to_thread(app._query_quota_sync, self.a["id"], True)
        saved = app.store.get_account(self.a["id"])
        self.assertEqual(saved["quota"], old)
        self.assertIn("额度面板", saved["quota_error"])
        events = app.journal.read(account_id=self.a["id"])["items"]
        self.assertTrue(any(x["event"] == "quota.failed" and x["error_type"] for x in events))

    async def test_quota_ui_failure_retries_only_one_read_in_fresh_context(self):
        with patch.object(app.engine, "renew_session_http", return_value=self.reply()), \
             patch.object(app.engine, "start"), \
             patch.object(app.engine, "quota", side_effect=[app.MuseGenerationError("menu hydration"), {"found":True,"weekly_used_pct":10}]) as query, \
             patch.object(app.engine, "close_current_page"):
            result = await asyncio.to_thread(app._query_quota_sync, self.a["id"], True)
        self.assertEqual(result["weekly_used_pct"], 10)
        self.assertEqual(query.call_count, 2)
        self.assertTrue(any(x["event"] == "quota.retry" for x in app.journal.read()["items"]))

    async def test_busy_is_skip_and_not_false_refresh(self):
        before = copy.deepcopy(self.a)
        app.GEN_LOCK.acquire()
        try:
            with self.assertRaises(app.HTTPException) as raised:
                app._query_quota_sync(self.a["id"], True)
            self.assertEqual(raised.exception.status_code, 409)
        finally:
            app.GEN_LOCK.release()
        self.assertEqual(self.a, before)
        app.ACCOUNT_POOL.busy_accounts.add(self.a["id"])
        try:
            result = app._probe_account_sync(self.a["id"])
        finally:
            app.ACCOUNT_POOL.busy_accounts.clear()
        self.assertTrue(result["busy"])
        self.assertEqual(self.a, before)

    async def test_due_scheduler_calls_quota_and_keepalive_not_task_records(self):
        app.store.create_task("video", "unrelated business task")
        disabled = app.store.add_account({"hatch_sess": "disabled-session"})
        app.store.update_account(disabled["id"], enabled=False)
        def probe(aid, check_quota=False):
            app.store.update_account(aid, last_keepalive=int(time.time()), keepalive_checked_at=int(time.time()))
            return {"ok": True, "id": aid}
        def quota(aid, background=False):
            self.assertTrue(background)
            q = {"found": True, "checked_at": int(time.time())}
            app.store.update_account(aid, quota=q)
            return q
        with patch.object(app, "_probe_account_sync", side_effect=probe) as keep, \
             patch.object(app, "_query_quota_sync", side_effect=quota) as query:
            first = await app.run_account_maintenance()
            await app.run_account_maintenance()
        self.assertEqual(first["refreshed_count"], 2)
        keep.assert_called_once_with(self.a["id"], False)
        query.assert_called_once_with(self.a["id"], True)
        self.assertEqual(app.store.get_account(self.a["id"])["last_used"], 1234)

    async def test_saved_include_disabled_overrides_legacy_environment_default(self):
        """The saved UI switch wins even if the deployment env originally enabled it."""
        disabled = app.store.add_account({"hatch_sess": "disabled-cadence-fixture"})
        app.store.update_account(disabled["id"], enabled=False)
        app.maintenance_settings.update({"include_disabled": False, "quota_enabled": False})
        def probe(aid, check_quota=False):
            return {"ok": True, "id": aid}
        with patch.object(app.CFG, "keepalive_disabled_accounts", True), \
             patch.object(app, "_probe_account_sync", side_effect=probe) as keep:
            await app.run_account_maintenance(force=True)
            self.assertEqual([x.args[0] for x in keep.call_args_list], [self.a["id"]])
            keep.reset_mock()
            app.maintenance_settings.update({"include_disabled": True})
            await app.run_account_maintenance(force=True)
            self.assertEqual({x.args[0] for x in keep.call_args_list}, {self.a["id"], disabled["id"]})

    async def test_settings_persist_validate_and_are_hot(self):
        result = app.update_maintenance_settings(app.MaintenancePatch(quota_interval_minutes=7,
                         keepalive_interval_minutes=11, quota_enabled=False))
        self.assertEqual(result["settings"]["quota_interval_minutes"], 7)
        loaded = MaintenanceSettingsStore(app.maintenance_settings.path).get()
        self.assertEqual(loaded.keepalive_interval_minutes, 11)
        self.assertFalse(loaded.quota_enabled)
        with self.assertRaises(ValueError):
            app.MaintenancePatch(quota_interval_minutes=0)
        with self.assertRaises(ValueError):
            app.MaintenancePatch(include_disabled="false")
        with patch.object(app, "_query_quota_sync") as quota:
            app.maintenance_settings.update({"keepalive_enabled": False})
            await app.run_account_maintenance(force=True)
            quota.assert_not_called()

    async def test_task_failure_is_durable_correlated_and_redacted(self):
        task = app.store.create_task("video", "private prompt not in log")
        with app.journal.context(task_id=task["id"]):
            try:
                raise RuntimeError("upload confirmation 3/4; Authorization: Bearer should-not-leak")
            except Exception as exc:
                app._record_task_failure(task["id"], exc, 12.3)
        saved = app.store.get_task(task["id"])
        self.assertEqual(saved["status"], "failed")
        self.assertEqual(saved["error_detail"]["error_type"], "RuntimeError")
        readback = Journal(app.journal.path).read(task_id=task["id"])["items"]
        self.assertTrue(readback and readback[0]["traceback"])
        serialized = json.dumps(readback) + saved["error"]
        self.assertNotIn("should-not-leak", serialized)
        self.assertNotIn("private prompt", serialized)
        self.assertIn("3/4", saved["error"])
        app.store.delete_task(task["id"])
        self.assertTrue(app.get_task_logs(task["id"])["entries"])

    async def request(self, method, path, body=None, authorized=True, query=""):
        raw = json.dumps(body).encode() if body is not None else b""
        headers = [(b"content-type", b"application/json")]
        if authorized:
            headers.append((b"authorization", b"Bearer test-only"))
        scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
                 "method": method, "scheme": "http", "path": path, "raw_path": path.encode(),
                 "query_string": query.encode(), "root_path": "", "headers": headers,
                 "server": ("test", 80), "client": ("test", 1)}
        sent, messages = False, []
        async def receive():
            nonlocal sent
            if not sent:
                sent = True
                return {"type": "http.request", "body": raw, "more_body": False}
            await asyncio.Event().wait()
        async def send(value):
            messages.append(value)
        await app.app(scope, receive, send)
        status = next(x["status"] for x in messages if x["type"] == "http.response.start")
        return status, json.loads(b"".join(x.get("body", b"") for x in messages))

    async def test_authenticated_settings_login_and_logs_routes(self):
        for path in ("/admin/maintenance/settings", "/admin/logs", "/admin/login/batches"):
            status, _ = await self.request("GET", path, authorized=False)
            self.assertEqual(status, 401)
        status, data = await self.request("PUT", "/admin/maintenance/settings", {"quota_interval_minutes": 9})
        self.assertEqual(status, 200)
        self.assertEqual(data["settings"]["quota_interval_minutes"], 9)
        status, _ = await self.request("PUT", "/admin/maintenance/settings", {"quota_interval_minutes": 0})
        self.assertEqual(status, 422)
        status, _ = await self.request("POST", "/admin/login/batches", {"accounts": [{"account": "u@example.com", "password": "never-echo-this"}]})
        self.assertEqual(status, 422)
        self.assertNotIn("never-echo-this", json.dumps(_))
        with patch.object(app.login_service, "submit", return_value={"id": "login-fixture", "status": "queued", "items": []}) as submit:
            status, data = await self.request("POST", "/admin/login/batches", {"accounts": [{"account": "u@example.com"}]})
        self.assertEqual(status, 202)
        submit.assert_called_once_with([{"account": "u@example.com", "label": ""}])
        status, _ = await self.request("POST", "/admin/login/batches/missing/otp", {"index": 0, "code": "bad"})
        self.assertEqual(status, 400)
        app.journal.emit("fixture.failed", level="error", task_id="task_fixture", account_id=self.a["id"], reason="3/4 uploads confirmed")
        status, page = await self.request("GET", "/admin/tasks/task_fixture/logs", query="level=error&limit=1")
        self.assertEqual(status, 200)
        self.assertEqual(page["entries"][0]["event"], "fixture.failed")
        self.assertTrue(page["health"]["ok"])

    async def test_cleanup_settings_are_authenticated_strict_and_non_destructive(self):
        media = Path(app.store.cfg.media_dir) / "protected-fixture.png"
        media.write_bytes(b"keep this fixture")
        original = media.read_bytes()
        status, _ = await self.request("GET", "/admin/cleanup/settings", authorized=False)
        self.assertEqual(status, 401)
        status, result = await self.request("GET", "/admin/cleanup/settings")
        self.assertEqual(status, 200)
        self.assertFalse(result["settings"]["enabled"])
        self.assertIsNone(result["state"]["last_checked_date"])
        changed = {**result["settings"], "check_time": "04:15", "media_max_mb": 4,
                   "media_target_mb": 2, "logs_max_mb": 3, "logs_target_mb": 1}
        status, saved = await self.request("PUT", "/admin/cleanup/settings", changed)
        self.assertEqual(status, 200)
        self.assertEqual(saved["settings"], changed)
        self.assertIsNone(saved["state"]["last_checked_date"])
        self.assertEqual(media.read_bytes(), original)
        self.assertTrue(Path(app.cleanup_manager.settings_path).is_file())
        for bad in ({**changed, "check_time": "25:00"}, {**changed, "timezone": "Invalid/Moon"},
                    {**changed, "media_target_mb": 4}, {**changed, "enabled": "true"}):
            status, _ = await self.request("PUT", "/admin/cleanup/settings", bad)
            self.assertEqual(status, 422)
        self.assertEqual(app.cleanup_manager.get_settings(), changed)

    async def test_releases_are_read_only_and_online_mutation_endpoints_retired(self):
        with patch.object(app.release_checker, "check", return_value={"local_version":"1.6.12", "status":"no_releases"}) as check:
            status, _ = await self.request("GET", "/admin/update/check", authorized=False)
            self.assertEqual(status, 401)
            check.assert_not_called()
            status, result = await self.request("GET", "/admin/update/check")
            self.assertEqual(status, 200)
            self.assertEqual(result["status"], "no_releases")
            self.assertEqual(check.call_count, 1)
        for path in ("/admin/update/upgrade", "/admin/repo/pull", "/admin/repo/push"):
            status, _ = await self.request("POST", path)
            self.assertEqual(status, 410)

    async def test_invalid_settings_file_preserves_service_and_reports_error(self):
        target = Path(self.temp.name) / "invalid-settings.json"
        target.write_text("{broken")
        settings = MaintenanceSettingsStore(str(target))
        self.assertEqual(settings.load_error, "JSONDecodeError")
        self.assertEqual(target.read_text(), "{broken")
        settings.update({"quota_interval_minutes": 5})
        self.assertIsNone(settings.load_error)
        self.assertEqual(MaintenanceSettingsStore(str(target)).get().quota_interval_minutes, 5)

    def test_cadence_and_failed_backoff(self):
        now = 1900000000
        account = {"last_keepalive": now - 60, "keepalive_checked_at": now - 60,
                   "last_used": 13, "quota": {"checked_at": now - 60}}
        self.assertFalse(operation_due(account, "keepalive", 10, now))
        self.assertTrue(operation_due(account, "keepalive", 1, now))
        account.update(quota_checked_at=now - 2, quota={"checked_at": now - 7200})
        self.assertFalse(operation_due(account, "quota", 60, now))
        self.assertTrue(operation_due(account, "quota", 60, now, force=True))


if __name__ == "__main__":
    try:
        unittest.main()
    finally:
        app.login_service.close()
        HOME.cleanup()
