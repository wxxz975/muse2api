"""地区限制必须换号重试，并把该账号标成异常（线上 11-11/11-13 各 1 次）。

Run: python tests/test_region_rotation.py
"""
import importlib.util
import os
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

with tempfile.TemporaryDirectory() as home:
    os.environ.update(MUSE2API_HOME=home, MUSE2API_PROFILE_ROOT=home,
                      MUSE2API_KEY="test-only")
    spec = importlib.util.spec_from_file_location("region_rotation_candidate", ROOT / "app.py")
    app = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = app
    spec.loader.exec_module(app)
    first = app.store.add_account({"hatch_sess": "one", "hatch_vml": "one",
                                   "hatch_native_auth_device": "one"}, "one")
    app.store.add_account({"hatch_sess": "two", "hatch_vml": "two",
                           "hatch_native_auth_device": "two"}, "two")
    calls = []

    def refuse(cookies, prompt, **kwargs):
        calls.append(cookies["hatch_sess"])
        raise app.MuseRegionRestrictedError(
            "当前账号所在地区不支持该能力: 你这个账号所在的国家/地区目前不支持视频生成")

    with patch.object(app, "_renew_and_persist",
                      side_effect=lambda ident, **_: app.store.get_account(ident)), \
         patch.object(app.engine, "start"), \
         patch.object(app.engine, "generate", side_effect=refuse), \
         patch.object(app.engine, "reset_thread"), \
         patch.object(app.engine, "close_current_page"):
        try:
            app._run_generation_locked("video", "video", 30, account_id=first["id"],
                                       deadline=app.time.monotonic() + 30)
        except app.MuseRegionRestrictedError:
            pass
        else:
            raise AssertionError("地区限制没有被抛出来")

    # 换号重试：两个账号都试过，而不是像内容拒绝那样止步于第一个
    assert calls == ["one", "two"], calls
    # 地区不支持的账号必须被标记异常，否则面板会一直显示"健康"并反复选它
    marked = app.store.get_account(first["id"])
    assert marked.get("ok") is False, marked.get("ok")
    assert "地区" in (marked.get("note") or ""), marked.get("note")
    assert app.MuseRegionRestrictedError.__mro__[1].__name__ == "MuseGenerationError"
print("PASS: region restriction rotates account and marks it unhealthy")
