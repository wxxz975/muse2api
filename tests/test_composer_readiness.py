"""Composer readiness: 上传附件后 React 重挂载导致的偶发"找不到输入框"。"""
import json
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from muse2api.engine import (MuseAccountDisconnectedError, MuseAuthError,
                            MuseEngine, MuseGenerationError)


class Page:
    def __init__(self, rects, state="{}"):
        self.rects = list(rects)
        self.state = state
        self.sent = []

    def js(self, expr, **kwargs):
        if "hatch-composer-placeholder-overlay" in expr and "getBoundingClientRect" not in expr:
            return self.state
        if "getBoundingClientRect" in expr:
            return self.rects.pop(0) if self.rects else None
        if "dispatchEvent" in expr:
            return 12
        return ""

    def send(self, method, params=None, timeout=None):
        self.sent.append(method)


def engine(rects, state="{}", timeout=0.4):
    e = MuseEngine(SimpleNamespace(profile_dir=tempfile.mkdtemp()))
    e.page = Page(rects, state)
    e.composer_timeout = timeout
    e._click_send = lambda: "clicked"
    return e


def test_composer_appearing_late_still_sends():
    e = engine([None, None, None, json.dumps({"x": 10, "y": 20})])
    assert e._send("hello") == "clicked"
    assert "Input.dispatchMouseEvent" in e.page.sent


def test_slow_but_recoverable_page_is_not_reported_as_broken():
    e = engine([None] * 3 + [json.dumps({"x": 1, "y": 2})], timeout=5)
    assert e._send("hello") == "clicked"


def test_timeout_reports_structure_change_without_clicking():
    e = engine([], state=json.dumps({"ready": "complete", "textarea": False, "composer": False, "signin": False}))
    try:
        e._send("hello")
    except MuseGenerationError as exc:
        assert "页面未在超时内就绪" in str(exc)
    else:
        raise AssertionError("missing composer must fail fast after the wait budget")
    assert "Input.dispatchMouseEvent" not in e.page.sent


def test_redirect_to_signin_is_an_auth_failure_not_a_generation_error():
    e = engine([], state=json.dumps({"ready": "complete", "textarea": False, "composer": False, "signin": True}))
    try:
        e._send("hello")
    except MuseAuthError as exc:
        assert "登录页" in str(exc)
    else:
        raise AssertionError("sign-in redirect must not be retried as a generic generation failure")


def test_disconnected_page_is_account_scoped_not_generic():
    e = engine([], state=json.dumps({"ready": "complete", "textarea": False, "composer": False,
                                     "signin": False, "disconnected": True}))
    try:
        e._send("hello")
    except MuseAccountDisconnectedError as exc:
        assert "Muse is disconnected" in str(exc)
    else:
        raise AssertionError("disconnected account must raise the account-scoped error")


def test_disconnected_marker_aborts_hydration_wait_immediately():
    class SlowPage:
        def __init__(self):
            self.calls = 0
        def js(self, expr, **kwargs):
            self.calls += 1
            return "disconnected"
    e = MuseEngine(SimpleNamespace(profile_dir=tempfile.mkdtemp()))
    page = SlowPage()
    started = time.monotonic()
    try:
        e._wait_ws_ready(page, timeout=5)
    except MuseAccountDisconnectedError:
        pass
    else:
        raise AssertionError("hydration wait must surface disconnection")
    assert time.monotonic() - started < 1.5 and page.calls == 1


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("PASS", name)
