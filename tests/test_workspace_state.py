"""工作区未连接的账号必须快速失败并换号，而不是反复重试同一账号。"""
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
HOME = tempfile.TemporaryDirectory(prefix="muse-vm-")
os.environ.update(MUSE2API_HOME=HOME.name, MUSE2API_PROFILE_ROOT=HOME.name, MUSE2API_KEY="test-only")
import app
from muse2api.engine import MuseWorkspaceUnavailableError


class WorkspaceTests(unittest.TestCase):
    def test_disabled_workspace_fails_fast_and_marks_account(self):
        acc = app.store.add_account({"hatch_sess": "vm-fixture"}, "vm-disabled")
        app.store.update_account(acc["id"], vm_state="DISABLED", ok=True)
        with patch.object(app.engine, "start") as start, \
             patch.object(app.engine, "generate") as generate, \
             patch.object(app, "_renew_and_persist",
                          side_effect=lambda i, **k: app.store.get_account(i)), \
             patch.object(app.engine, "close_current_page"):
            with self.assertRaises(MuseWorkspaceUnavailableError):
                app._run_generation_locked("prompt", "image", 30, account_id=acc["id"],
                                           deadline=app.time.monotonic() + 30)
        start.assert_not_called()
        generate.assert_not_called()
        saved = app.store.get_account(acc["id"])
        self.assertFalse(saved.get("ok"))
        self.assertIn("工作区未连接", saved.get("note") or "")
        self.assertFalse(app.ACCOUNT_POOL.busy_accounts)
        self.assertFalse(app.GEN_LOCK.locked())

    def test_running_workspace_is_not_blocked(self):
        acc = app.store.add_account({"hatch_sess": "vm-ok"}, "vm-running")
        app.store.update_account(acc["id"], vm_state="RUNNING", ok=True)
        with patch.object(app.engine, "start"), \
             patch.object(app.engine, "close_current_page"), \
             patch.object(app.engine, "reset_thread"), \
             patch.object(app, "_renew_and_persist", side_effect=lambda i, **k: app.store.get_account(i)), \
             patch.object(app.engine, "generate", return_value={"filename": "x.png", "size": 1, "kind": "image"}) as generate:
            res, aid = app._run_generation_locked("prompt", "image", 30, account_id=acc["id"],
                                                  deadline=app.time.monotonic() + 30)
        self.assertEqual(aid, acc["id"])
        generate.assert_called_once()


if __name__ == "__main__":
    try:
        unittest.main()
    finally:
        app.login_service.close()
        HOME.cleanup()
