"""Offline behavioral regressions for persistent, redacted engine diagnostics.

Run: python3 -m pytest -q tests/test_diagnostics.py
Only synthetic cookies/media/OTP values and temporary directories are used.
"""
import asyncio
import base64
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import stat
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import diagnostics
from diagnostics import Journal, redact, safe_message
import engine as module


class Clock:
    now = 1000.0

    def monotonic(self):
        return self.now

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


@pytest.fixture
def clock(monkeypatch):
    value = Clock()
    monkeypatch.setattr(module, "time", value)
    return value


def make_engine(tmp_path, journal=None):
    cfg = SimpleNamespace(profile_dir=str(tmp_path / "profile"),
                          data_dir=str(tmp_path / "data"), media_dir=str(tmp_path / "media"))
    journal = journal or Journal(tmp_path / "events.jsonl")
    return module.MuseEngine(cfg, journal=journal)


def entries(journal):
    return journal.read(limit=200)["items"]


def test_string_and_recursive_redaction():
    cases = {
        "Authorization: Bearer auth-synthetic": ["auth-synthetic"],
        "authorization=Bearer auth-synthetic": ["auth-synthetic"],
        'Cookie: hatch_sess=cookie-synthetic; second=cookie-two': ["cookie-synthetic", "cookie-two"],
        '{"Cookie":"hatch_sess=json-synthetic; x=other-synthetic"}': ["json-synthetic", "other-synthetic"],
        "token=token-synthetic password='password synthetic'": ["token-synthetic", "password synthetic"],
        "password is prose-synthetic": ["prose-synthetic"],
        "otp code: 654321": ["654321"],
        "verification_code=verify-synthetic": ["verify-synthetic"],
        "https://user:proxy-synthetic@proxy.example:1080/private-path?q=query-synthetic#fragment-synthetic":
            ["proxy-synthetic", "private-path", "query-synthetic", "fragment-synthetic"],
        "socks5://user:socks-synthetic@proxy.example:1080/": ["socks-synthetic"],
        "<pre>unlabelled-private-page-fragment</pre>": ["unlabelled-private-page-fragment"],
        "模型未生成媒体，仅返回文本: arbitrary-private-model-text": ["arbitrary-private-model-text"],
        "data:image/png;base64,aGVsbG8=": ["aGVsbG8="],
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJmaXh0dXJlIn0.signature": ["signature"],
        "OTP is 765432": ["765432"],
    }
    for text, secrets in cases.items():
        result = safe_message(text)
        for secret in secrets:
            assert secret not in result
        assert result == safe_message(result)
    source = {"Authorization": "private-a", "cookies": {"hatch_sess": "private-b"},
              "otp": "private-c", "proxy": {"password": "private-d"},
              "prompt": "private-e", "media_data": b"private-f",
              "safe": {"count": 3, "send_state": "clicked"}, "code": "private-g"}
    result = redact(source)
    encoded = json.dumps(result)
    assert all(value not in encoded for value in ("private-a", "private-b", "private-c", "private-d",
                                                  "private-e", "private-f", "private-g"))
    assert result["safe"] == source["safe"]
    assert source["cookies"]["hatch_sess"] == "private-b"


def test_error_persists_stack_only_and_reopens(tmp_path):
    path = tmp_path / "journal.jsonl"
    journal = Journal(path)
    private_local = "do-not-log-local-value"

    class UnsafeException(RuntimeError):
        def __str__(self):
            raise AssertionError("exception text must never be inspected")

    try:
        raise UnsafeException(private_local)
    except UnsafeException as exc:
        with journal.context(task_id="task-a", account_id="account-a", attempt=2):
            record = journal.error("task.failed", exc, stage="extract", reason="media_decode_failed", duration_ms=23)
    assert record["error_type"] == "UnsafeException"
    assert record["traceback"] and set(record["traceback"][0]) == {"file", "line", "function"}
    assert private_local not in path.read_text()
    assert "raise UnsafeException" not in path.read_text()
    reopened = Journal(path).read(task_id="task-a", account_id="account-a", level="error")
    assert reopened["total"] == 1 and reopened["items"][0]["attempt"] == 2
    assert reopened["items"][0]["duration_ms"] == 23
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert journal.health()["ok"] is True


def test_contexts_and_concurrent_instances_do_not_cross(tmp_path):
    path = tmp_path / "journal.jsonl"
    journals = [Journal(path, max_bytes=512000), Journal(path, max_bytes=512000)]

    def worker(number):
        journal = journals[number % 2]
        with journal.context(task_id=f"task-{number}", account_id=f"account-{number}", attempt=number):
            journal.emit("outer")
            with journal.context(stage="upload", attempt=99):
                journal.emit("inner")
            journal.emit("restored")
        journal.emit("unbound")

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(worker, range(20)))
    result = entries(journals[0])
    assert len(result) == 80
    assert len({item["id"] for item in result}) == 80
    for number in range(20):
        subset = journals[0].read(task_id=f"task-{number}")["items"]
        assert len(subset) == 3
        assert all(item["account_id"] == f"account-{number}" for item in subset)
        assert {item["attempt"] for item in subset if item["event"] != "inner"} == {number}
        assert next(item for item in subset if item["event"] == "inner")["attempt"] == 99
    assert all(item["task_id"] is None for item in result if item["event"] == "unbound")
    # Every physical line is complete JSON, not interleaved text from threads.
    assert len([json.loads(line) for line in path.read_text().splitlines()]) == 80


def test_async_context_isolation(tmp_path):
    journal = Journal(tmp_path / "events.jsonl")

    async def worker(number):
        with journal.context(task_id=f"async-{number}"):
            await asyncio.sleep(0)
            journal.emit("async.complete", number=number)

    async def run():
        await asyncio.gather(*(worker(number) for number in range(10)))

    asyncio.run(run())
    assert all(item["task_id"] == f"async-{item['number']}" for item in entries(journal))


def test_reverse_pagination_filters_rotation_and_size_bound(tmp_path):
    journal = Journal(tmp_path / "events.jsonl", max_bytes=1024, backup_count=2)
    for number in range(30):
        journal.emit("event", number=number, task_id="odd" if number % 2 else "even",
                     account_id="account", level="error" if number % 3 == 0 else "info")
    files = list(tmp_path.iterdir())
    assert 1 < len(files) <= 3
    assert all(path.stat().st_size <= 1024 for path in files)
    records = entries(journal)
    assert records[0]["number"] == 29
    assert [item["number"] for item in records] == sorted((item["number"] for item in records), reverse=True)
    page = journal.read(limit=2)
    assert page["next_offset"] == 2
    next_page = journal.read(offset=2, limit=2)
    assert page["items"] + next_page["items"] == records[:4]
    filtered = journal.read(task_id="odd", account_id="account", level="error")["items"]
    assert all(item["number"] % 2 and item["number"] % 3 == 0 for item in filtered)
    huge = Journal(tmp_path / "huge.jsonl", max_bytes=1024, backup_count=0)
    huge.emit("界" * 2000, task_id="界" * 2000, account_id="界" * 2000, stage="界" * 2000,
              reason={"value": "界" * 3000}, error_type="界" * 2000, extra=["x" * 2000] * 50)
    assert huge.path.stat().st_size <= 1024
    assert huge.read()["items"][0]["truncated"] is True
    with pytest.raises(ValueError):
        journal.read(limit=201)
    with pytest.raises(ValueError):
        journal.read(offset=-1)
    with pytest.raises(ValueError):
        journal.read(level="everything")


def test_corrupt_line_skipped_and_persistence_failure_visible(tmp_path, monkeypatch):
    journal = Journal(tmp_path / "events.jsonl")
    journal.emit("first")
    with journal.path.open("ab") as stream:
        stream.write(b"not-json\n")
    journal.emit("last")
    assert [item["event"] for item in entries(journal)] == ["last", "first"]
    real_open = diagnostics.os.open

    def denied(path, flags, *args):
        if flags & diagnostics.os.O_WRONLY:
            raise PermissionError("do-not-log-sensitive-disk-path")
        return real_open(path, flags, *args)

    monkeypatch.setattr(diagnostics.os, "open", denied)
    unwritten = journal.emit("cannot.write")
    assert unwritten["persisted"] is False and unwritten["write_error"] == "PermissionError"
    assert all(item["event"] != "cannot.write" for item in entries(journal))
    assert journal.health()["ok"] is False
    assert journal.health()["error_type"] == "PermissionError"


class UploadPage:
    def __init__(self, *, missing=False, swallow_first=False):
        self.missing = missing
        self.swallow_first = swallow_first
        self.accepted = 0
        self.calls = 0

    def send(self, method, params=None, **kwargs):
        if method == "Runtime.evaluate":
            return {"result": {"result": {} if self.missing else {"objectId": "node"}}}
        if method == "DOM.setFileInputFiles":
            self.calls += 1
            if self.calls != 1 or not self.swallow_first:
                self.accepted += 1
            return {}
        raise AssertionError(method)

    def js(self, _script, **kwargs):
        return self.accepted


def test_upload_counts_retry_and_failure_persist_without_file_names(tmp_path, clock):
    engine = make_engine(tmp_path)
    engine.page = UploadPage(swallow_first=True)
    engine._UPLOAD_PER_FILE_TIMEOUT = 0.6
    paths = ["/never-read/private-upload-one.png", "/never-read/private-upload-two.png"]
    with engine.journal.context(task_id="upload-task", account_id="account", attempt=3):
        engine._upload_references(paths)
    assert engine.page.accepted == 2
    events = entries(engine.journal)
    assert any(item["event"] == "engine.upload.retry" and item["retry"] == 1 for item in events)
    assert any(item["event"] == "engine.upload.confirmed" and item["confirmed_count"] == 2 for item in events)
    assert all(item["attempt"] == 3 for item in events)
    assert "private-upload" not in engine.journal.path.read_text()
    engine.page = UploadPage(missing=True)
    with pytest.raises(module.MuseGenerationError, match="找不到上传控件"):
        engine._set_input_files(paths, timeout=0.1)
    failure = Journal(engine.journal.path).read(level="error")["items"][0]
    assert failure["stage"] == "upload_input" and failure["error_type"] == "MuseGenerationError"


class SendPage:
    def js(self, script, **kwargs):
        if "getBoundingClientRect" in script:
            return '{"x":1,"y":2}'
        if "Object.getOwnPropertyDescriptor" in script:
            return 12
        if script == module.MuseEngine._CLICK_SEND_JS:
            return "clicked"
        return None

    def send(self, *args, **kwargs):
        return {}


def test_send_records_input_and_click_not_delivery(tmp_path, clock):
    engine = make_engine(tmp_path)
    engine.page = SendPage()
    assert engine._send("private-prompt-that-must-not-appear") == "clicked"
    events = entries(engine.journal)
    assert {item["stage"] for item in events} == {"input", "send"}
    clicked = next(item for item in events if item["event"] == "engine.send.state")
    assert clicked["send_state"] == "clicked" and clicked["reason"] == "click_not_delivery_confirmation"
    assert "private-prompt" not in engine.journal.path.read_text()


def test_extract_attempts_bytes_not_logged_and_error_code_kept(tmp_path, clock):
    engine = make_engine(tmp_path)
    private_bytes = b"private-binary-media-fixture"
    responses = iter([{"ok": False, "err": "media-http-503"},
                      {"ok": True, "b64": base64.b64encode(private_bytes).decode(),
                       "mime": "image/png", "url": "https://media.example/private?token=private-media-url"}])
    engine.page = SimpleNamespace(js=lambda *args, **kwargs: next(responses))
    result = engine.extract_bytes("https://media.example/private?token=private-media-url", retries=2)
    assert result[0] == private_bytes
    log = engine.journal.path.read_text()
    assert "private-binary-media-fixture" not in log
    assert base64.b64encode(private_bytes).decode() not in log and "private-media-url" not in log
    events = entries(engine.journal)
    retry = next(item for item in events if item["event"] == "engine.extract.retry")
    assert retry["reason"] == "media-http-503" and retry["retry"] == 1
    engine.page = SimpleNamespace(js=lambda *args, **kwargs: {"ok": False, "err": "private-page-error"})
    with pytest.raises(module.MuseGenerationError, match="media_fetch_failed"):
        engine.extract_bytes("blob:synthetic", retries=1)
    assert "private-page-error" not in engine.journal.path.read_text()


def test_wait_refusal_timeout_and_cancel_without_page_text(tmp_path, clock):
    engine = make_engine(tmp_path)
    engine._scroll_bottom = lambda: None
    engine.attachments = lambda: []
    refusal = "图片没生成成功，请稍后重试 private-model-response"
    engine.page = SimpleNamespace(js=lambda *args, **kwargs: json.dumps({
        "cnt": 1, "txt": refusal, "stop": False, "tail": "private-page-tail", "path": "/thread/synthetic"}))
    with engine.journal.context(task_id="refusal-task", account_id="account", attempt=1):
        with pytest.raises(module.MuseModelRefusalError):
            engine._wait_attachment("", 30, "image")
    errors = engine.journal.read(task_id="refusal-task", level="error")["items"]
    assert errors[0]["stage"] == "refusal" and errors[0]["reason"] == "model_refused"
    assert errors[0]["task_id"] == "refusal-task" and errors[0]["account_id"] == "account"
    assert "private-model-response" not in engine.journal.path.read_text()
    assert "private-page-tail" not in engine.journal.path.read_text()
    engine.page = SimpleNamespace(js=lambda *args, **kwargs: '{}')
    assert engine._wait_attachment("", 1, "image") is None
    assert any(item["event"] == "engine.wait.timeout" for item in entries(engine.journal))
    with pytest.raises(module.MuseGenerationError, match="断开连接"):
        engine._wait_attachment("", 30, "image", stop_event=SimpleNamespace(is_set=lambda: True))


def test_debug_dump_saves_only_typed_allowlisted_state(tmp_path):
    engine = make_engine(tmp_path)
    engine.page = SimpleNamespace(js=lambda script: json.dumps({
        "attachment_count": 4, "send_state": "disabled", "ready_state": "complete",
        "composer_present": True, "composer_has_text": True,
        "composer_disabled": False, "stop_visible": True,
        "tail": "private-page-tail", "prompt": "private-prompt", "cookie": "private-cookie",
        "src": "https://media.example/private?token=private-url", "buttons": ["private-label"]}))
    state = engine._debug_dump("../../escape-private")
    assert state["attachment_count"] == 4 and state["send_state"] == "disabled"
    saved = Path(engine.cfg.data_dir) / "debug-state.json"
    assert json.loads(saved.read_text()) == state
    assert stat.S_IMODE(saved.stat().st_mode) == 0o600
    assert "private-" not in saved.read_text() + engine.journal.path.read_text()
    assert "document.body.innerText" not in engine._SAFE_DEBUG_JS and "location.href" not in engine._SAFE_DEBUG_JS
    engine.page = SimpleNamespace(js=lambda script: {"attachment_count": "private-count", "send_state": "private-state"})
    assert engine._debug_dump("extract-fail") == {}


def test_generation_real_methods_record_stages_and_original_failure(tmp_path, clock):
    engine = make_engine(tmp_path)
    engine.page = SendPage()
    engine.ensure_page = lambda *args, **kwargs: None
    engine.reset_thread = lambda **kwargs: None
    engine._scroll_bottom = lambda: None
    engine._clear_attachments = lambda: None
    engine._agent_count = lambda: 0
    engine.attachments = lambda: []
    engine._wait_attachment = lambda *args, **kwargs: {"src": "blob:synthetic", "w": 1, "h": 1}
    old_js = engine.page.js
    engine.page.js = lambda script, **kwargs: (
        {"ok": True, "b64": base64.b64encode(b"fixture-result").decode(), "mime": "image/png"}
        if "async function(src, expect)" in script else old_js(script, **kwargs))
    with engine.journal.context(task_id="generation-task", account_id="account", attempt=4):
        result = engine.generate({"hatch_sess": "private-cookie"}, "private-prompt", account_id="account")
    assert Path(result["path"]).read_bytes() == b"fixture-result"
    events = entries(engine.journal)
    assert {"generation", "send", "input", "extract"}.issubset({item["stage"] for item in events})
    assert all(item["task_id"] == "generation-task" and item["account_id"] == "account" for item in events)
    assert "private-cookie" not in engine.journal.path.read_text() and "private-prompt" not in engine.journal.path.read_text()
    failure = RuntimeError("private-untrusted-page-body")
    engine.ensure_page = lambda *args, **kwargs: (_ for _ in ()).throw(failure)
    with pytest.raises(RuntimeError) as caught:
        engine.generate({}, "private-prompt")
    assert caught.value is failure
    error = entries(engine.journal)[0]
    assert error["event"] == "engine.generation.failed" and error["reason"] == "generation_failed"
    assert "private-untrusted-page-body" not in engine.journal.path.read_text()


def test_quota_reads_real_cookie_metadata_before_close_without_response_leak(tmp_path, clock):
    engine = make_engine(tmp_path)
    engine._close_page = lambda: setattr(engine, "page", None)
    engine.ensure_page = lambda *args, **kwargs: setattr(engine, "page", SimpleNamespace(js=lambda script: "usage"))
    engine._quota_click = lambda *args: None
    engine._parse_quota = lambda text: {"found": True, "weekly_used_pct": 2}
    cookies = {"hatch_sess": {"value": "private-cookie", "expires": 12345}}
    engine.read_cookies = lambda: cookies
    assert engine.quota({}, account_id="account") == {"found": True, "weekly_used_pct": 2}
    assert engine.page is None and engine.last_quota_cookies == cookies
    assert "private-cookie" not in engine.journal.path.read_text()
    engine.read_cookies = lambda: (_ for _ in ()).throw(RuntimeError("private-page"))
    assert engine.quota({}, account_id="account")["found"] is True
    assert engine.last_quota_cookies == {}
