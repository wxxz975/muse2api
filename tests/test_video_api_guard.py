"""视频接口回归：参数校验、幂等、准入上限、任务类型隔离。

Run: python tests/test_video_api_guard.py
不需要账号、网络或浏览器；_run_generation 被打桩。
"""
import asyncio
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parents[1]


class Response:
    def __init__(self, status, body):
        self.status_code, self.body = status, body

    def json(self):
        return json.loads(self.body) if self.body else {}


class Client:
    """Minimal ASGI test client using the standard library, no httpx dependency."""

    def __init__(self, app, headers):
        self.app, self.headers = app, headers

    async def request(self, method, path, json_data=None, headers=None):
        hdr = {**self.headers, **(headers or {})}
        body = b""
        if json_data is not None:
            body = json.dumps(json_data).encode()
            hdr["content-type"] = "application/json"
        hdr["content-length"] = str(len(body))
        scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
                 "method": method, "scheme": "http", "path": path,
                 "raw_path": path.encode(), "query_string": b"", "root_path": "",
                 "headers": [(k.lower().encode(), v.encode()) for k, v in hdr.items()],
                 "server": ("test", 80), "client": ("test", 1)}
        messages = []
        sent = False

        async def receive():
            nonlocal sent
            if not sent:
                sent = True
                return {"type": "http.request", "body": body, "more_body": False}
            await asyncio.Event().wait()

        async def send(message):
            messages.append(message)

        await self.app(scope, receive, send)
        status = next(m["status"] for m in messages if m["type"] == "http.response.start")
        return Response(status, b"".join(m.get("body", b"") for m in messages))

    async def post(self, path, json=None, **kwargs):
        return await self.request("POST", path, json_data=json, **kwargs)

    async def get(self, path, **kwargs):
        return await self.request("GET", path, **kwargs)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


async def wait_task(client, tid, timeout=5.0):
    end = time.time() + timeout
    body = {}
    while time.time() < end:
        body = (await client.get("/v1/videos/" + tid)).json()
        if body.get("status") in ("completed", "failed"):
            return body
        await asyncio.sleep(0.01)
    return body


async def main(source, home):
    os.environ.update(MUSE2API_HOME=home, MUSE2API_PROFILE_ROOT=home,
                      MUSE2API_KEY="test-only", MUSE2API_PUBLIC_BASE="")
    sys.path.insert(0, str(source.parent))
    spec = importlib.util.spec_from_file_location("video_guard_target", source)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)

    calls = []

    def fake_generation(prompt, kind, timeout, **kwargs):
        calls.append(kind)
        name = "fixture-video.mp4"
        (Path(module.CFG.media_dir) / name).write_bytes(b"fake-video-bytes")
        return ({"filename": name, "size": 16, "kind": "video",
                 "path": str(Path(module.CFG.media_dir) / name)}, "acc-fixture")

    module._run_generation = fake_generation

    async with Client(module.app, {"Authorization": "Bearer test-only"}) as client:
        # 1) 脏参数必须 422：负时长/超大时长、0 或超长 timeout 都不能进提示词
        for bad in ({"duration": 0}, {"duration": -3}, {"duration": 31},
                    {"duration": 10 ** 6}, {"timeout": 0}, {"timeout": 601},
                    {"timeout": -5}, {"timeout": 10 ** 400}):
            res = await client.post("/v1/videos", json={"prompt": "cat", **bad})
            assert res.status_code == 422, (bad, res.status_code, res.body[:200])
        print("validation=PASS")

        # 2) 正常任务：异步创建 -> 轮询 -> 带 url/video.url 的完成态
        res = await client.post("/v1/videos", json={"prompt": "cat", "duration": 5})
        assert res.status_code == 200, (res.status_code, res.body[:200])
        created = res.json()
        assert created["object"] == "video.task", created
        tid = created["task_id"]
        body = await wait_task(client, tid)
        assert body["status"] == "completed", body
        assert body["url"].endswith(".mp4"), body
        assert body["video"]["url"] == body["url"], body
        assert body["progress"] == 100
        assert module.store.get_task(tid)["duration"] == 5, "duration 应落进任务记录"
        print("completed_task=PASS")

        # 3) 幂等：同一个 key + 同一个请求 -> 同一个任务；不同请求 -> 409
        headers = {"Idempotency-Key": "video-key-1"}
        first = await client.post("/v1/videos", json={"prompt": "same"}, headers=headers)
        second = await client.post("/v1/videos", json={"prompt": "same"}, headers=headers)
        assert first.json()["task_id"] == second.json()["task_id"], (first.body, second.body)
        conflict = await client.post("/v1/videos", json={"prompt": "other"}, headers=headers)
        assert conflict.status_code == 409, (conflict.status_code, conflict.body[:200])
        too_long = await client.post("/v1/videos", json={"prompt": "x"},
                                     headers={"Idempotency-Key": "k" * 300})
        assert too_long.status_code == 400, too_long.status_code
        print("idempotency=PASS")

        # 4) 准入上限：排队中的视频达到上限后 429，不再起新线程烧额度
        old_limit = module.CFG.video_queue
        try:
            module.CFG.video_queue = 1
            module.store.create_task("video", "queued-fixture")
            full = await client.post("/v1/videos", json={"prompt": "over-limit"})
            assert full.status_code == 429, (full.status_code, full.body[:200])
        finally:
            module.CFG.video_queue = old_limit
        print("admission_cap=PASS")

        # 5) 任务类型隔离：图片任务 ID 不能当视频任务查
        image_task = module.store.create_task("image", "image-fixture")
        wrong = await client.get("/v1/videos/" + image_task["id"])
        assert wrong.status_code == 404, wrong.status_code
        missing = await client.get("/v1/videos/task_not_exists")
        assert missing.status_code == 404, missing.status_code
        print("task_kind_isolation=PASS")

        # 6) 失败任务仍然返回 error 文本
        failed = module.store.create_task("video", "fail-fixture")
        module.store.update_task(failed["id"], status="failed", error="等待生成超时")
        body = (await client.get("/v1/videos/" + failed["id"])).json()
        assert body["status"] == "failed" and "超时" in body["error"], body
        print("failed_task=PASS")

    assert calls and set(calls) == {"video"}, calls


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as home:
        asyncio.run(main(ROOT / "app.py", home))
    print("PASS: video API validation, idempotency, admission and task isolation")
