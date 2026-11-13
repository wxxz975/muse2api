"""静默自动升级不能覆盖本地未提交的改动。

Run: python tests/test_auto_upgrade_guard.py
"""
import importlib.util
import os
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class FakeResponse:
    def __init__(self, payload):
        self.status_code = 200
        self._payload = payload

    def json(self):
        return self._payload


def fake_requests_get(url, **kwargs):
    if "version.json" in url:
        return FakeResponse({"version": "9.9.9", "highlights": ["x"]})
    if "api.github.com" in url:
        return FakeResponse([{
            "sha": "deadbeefcafe1234",
            "commit": {"message": "upstream change",
                       "committer": {"date": "2026-11-13T00:00:00Z"}},
            "html_url": "https://example.invalid/commit",
        }])
    raise AssertionError("unexpected URL: " + url)


def make_git(status_output):
    def _git(args, timeout=30):
        head = args[0]
        if head == "rev-parse":
            return SimpleNamespace(returncode=0, stdout="b60f474\n", stderr="")
        if head == "log":
            return SimpleNamespace(returncode=0, stdout="msg||0\n", stderr="")
        if head == "status":
            return SimpleNamespace(returncode=0, stdout=status_output, stderr="")
        raise AssertionError("unexpected git call: " + " ".join(args))
    return _git


with tempfile.TemporaryDirectory() as home:
    os.environ.update(MUSE2API_HOME=home, MUSE2API_PROFILE_ROOT=home,
                      MUSE2API_KEY="test-only")
    spec = importlib.util.spec_from_file_location("upgrade_guard_target", ROOT / "app.py")
    app = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = app
    spec.loader.exec_module(app)

    started = []

    with patch("requests.get", side_effect=fake_requests_get):
        # 1) 工作区有未提交改动 -> 不升级（否则 git checkout -f 会抹掉热修）
        app._git = make_git(" M app.py\n M engine.py\n")
        app._silent_upgrade_worker = lambda: started.append("dirty")
        app._check_update_sync(force=True)
        time.sleep(0.2)
        assert started == [], started

        # 2) 工作区干净 -> 照常静默升级
        app._git = make_git("")
        app._silent_upgrade_worker = lambda: started.append("clean")
        app._check_update_sync(force=True)
        time.sleep(0.2)
        assert started == ["clean"], started
print("PASS: silent auto-upgrade is skipped while the working tree has local edits")
