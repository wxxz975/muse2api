"""Offline task projection tests; standard library only.

Run: python3 -B tests/test_task_views.py
Also collectable by: python3 -m pytest -q tests/test_task_views.py
All filesystem fixtures live in TemporaryDirectory, never application data.
"""
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import task_views
from task_views import project_task


IMAGE = "a" * 32 + ".png"
VIDEO = "b" * 32 + ".mp4"
AUDIO = "01234567-89ab-cdef-0123-456789abcdef.mp3"
PUBLIC = "https://media.example.test/service"


def local_url(name):
    return "/v1/media/" + name


def public_url(name):
    return PUBLIC + local_url(name)


class TaskViewsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.media = self.root / "media"
        self.media.mkdir()
        self.accounts = [{"id": "account-current", "aliases": ["account-old"],
                          "label": "当前账号", "cookies": {"hatch_sess": "must-not-copy"}}]

    def view(self, task, *, accounts=None, media=None, url_fn=local_url):
        return project_task(task, self.accounts if accounts is None else accounts,
                            self.media if media is None else media, url_fn)

    def put(self, name=IMAGE, payload=b"not actual media; never read"):
        path = self.media / name
        path.write_bytes(payload)
        return path

    def task(self, name=IMAGE, **fields):
        return {"id": "task_fixture", "object": "task", "kind": "image",
                "status": "completed", "prompt": "请画一只猫 [图1]",
                "api_prompt": "Create a cat, ratio 16:9",
                "created_at": 1700000000, "updated_at": 1700000001,
                "result": {"filename": name, "size": 9999, "kind": "image"},
                **fields}

    def test_complete_schema_generation_and_result_size_are_separate(self):
        file = self.put()
        task = self.task(account="account-old", size="1920x1080", aspect_ratio="16:9",
                         resolution="2K", duration=6, response_format="url", progress=100,
                         generation={"size": "1280x720", "quality": "high",
                                     "reference_counts": {"images": 2, "audios": 1, "videos": 0}},
                         metadata={"elapsed": 6.2, "stage": "extract"}, elapsed=6.2)
        task["result"].update(w=1280, h=720, path=str(file))
        before, accounts_before = copy.deepcopy(task), copy.deepcopy(self.accounts)
        view = self.view(task)
        self.assertEqual(view["prompt"], task["prompt"])
        self.assertEqual(view["api_prompt"], task["api_prompt"])
        self.assertEqual(view["account_id"], "account-current")
        self.assertEqual(view["account_label"], "当前账号")
        self.assertEqual(view["generation"], {
            "size": "1280x720", "quality": "high", "reference_counts": {"images": 2, "audios": 1, "videos": 0},
            "aspect_ratio": "16:9", "resolution": "2K", "duration": 6, "response_format": "url"})
        self.assertEqual(view["media"], {"url": local_url(IMAGE), "filename": IMAGE,
            "kind": "image", "exists": True, "bytes": file.stat().st_size,
            "width": 1280, "height": 720, "availability": "available"})
        self.assertEqual(view["result"]["size"], 9999)
        self.assertEqual(view["size"], "1920x1080")
        self.assertEqual(view["metadata"], task["metadata"])
        self.assertNotIn("path", view["result"])
        self.assertEqual(task, before)
        self.assertEqual(self.accounts, accounts_before)
        view["metadata"]["stage"] = "changed"
        view["generation"]["reference_counts"]["images"] = 0
        self.assertEqual(task, before)

    def test_request_parameters_fallback_does_not_infer_missing_generation(self):
        view = self.view(self.task(request={"size": "1024x1024", "aspect_ratio": "1:1"},
                                   parameters={"resolution": "1K", "duration": 3}))
        self.assertEqual(view["generation"], {"size": "1024x1024", "aspect_ratio": "1:1",
                                               "resolution": "1K", "duration": 3})
        self.assertEqual(self.view(self.task())["generation"], {})
        self.assertNotIn("width", self.view(self.task())["media"])
        self.assertNotIn("height", self.view(self.task())["media"])

    def test_missing_media_preserves_completed_status_and_historical_bytes(self):
        task = self.task(size="1024x1024")
        view = self.view(task)
        self.assertEqual(view["status"], "completed")
        self.assertEqual(view["media"]["availability"], "deleted")
        self.assertFalse(view["media"]["exists"])
        self.assertEqual(view["media"]["filename"], IMAGE)
        self.assertEqual(view["media"]["bytes"], 9999)
        self.assertEqual(view["media"]["url"], local_url(IMAGE))
        self.assertEqual(view["generation"]["size"], "1024x1024")
        self.assertEqual(list(self.media.iterdir()), [])

    def test_empty_incomplete_and_text_tasks_have_no_media(self):
        for status in ("queued", "processing", "failed", "cancelled"):
            with self.subTest(status=status):
                task = {"id": "task_fixture", "kind": "image", "status": status,
                        "prompt": "hello", "result": None, "error": "failure" if status == "failed" else None}
                self.assertIsNone(self.view(task)["media"])
        self.assertIsNone(self.view({"id": "task_text", "kind": "chat", "status": "completed"})["media"])
        self.assertEqual(self.view({"kind": "video", "status": "completed"})["media"]["availability"], "unknown")

    def test_account_mapping_and_historical_attempt_labels_on_failure(self):
        task = {"id": "task_failed", "kind": "video", "status": "failed", "account": "account-old",
                "account_label": "stale-label", "error": "generation timeout", "elapsed": 9,
                "error_detail": {"error_type": "TimeoutError", "reason": "timed out",
                                 "logs_url": "/admin/tasks/task_failed/logs"},
                "attempts": [{"account_id": "account-old", "account_label": "历史标签", "attempt": 1,
                              "stage": "wait", "status": "failed", "error": "timed out"},
                             {"account": {"id": "deleted-account", "cookies": {"x": "secret"}},
                              "account_label": "旧账号", "attempt": 2},
                             {"account_id": "account-current", "attempt": 3},
                             {"attempt": 4, "stage": "queue"}]}
        before = copy.deepcopy(task)
        view = self.view(task, accounts={"account-current": {"label": "新标签", "aliases": ["account-old"]}})
        self.assertEqual(view["account_id"], "account-current")
        self.assertEqual(view["account_label"], "新标签")
        self.assertEqual(view["attempts"][0]["account_label"], "历史标签")
        self.assertEqual(view["attempts"][1]["account_id"], "deleted-account")
        self.assertEqual(view["attempts"][1]["account"], "deleted-account")
        self.assertEqual(view["attempts"][2]["account_label"], "新标签")
        self.assertIsNone(view["attempts"][3]["account_id"])
        self.assertIsNone(view["attempts"][3]["account_label"])
        self.assertEqual(view["error_detail"], task["error_detail"])
        self.assertEqual(view["status"], "failed")
        self.assertEqual(task, before)
        self.assertIsNone(self.view({"account_id": "unmapped"})["account_label"])
        self.assertIsNone(self.view({})["account_id"])

    def test_recursive_credentials_data_urls_local_paths_and_hashes_removed(self):
        task = self.task(
            image_request_hash="request-hash-secret", video_request_key="request-key-secret",
            token="token-secret", _internal="internal-secret",
            prompt="Keep cat. data:image/png;base64,PAYLOADSECRET",
            api_prompt="Cookie: hatch_sess=COOKIESECRET; other=OTHERSECRET",
            metadata={"Authorization": "Bearer HEADERSECRET", "apiKey": "KEYSECRET",
                      "reference_images": ["raw-reference-secret"],
                      "rawReferenceDataURLs": ["raw-reference-secret"],
                      "nested": [{"password": "PASSWORDSECRET", "keep": "visible"}],
                      "trace": "/Users/private/location/image.png",
                      "windows": r"C:\private\secret.png",
                      "download": "file:///tmp/private/path.png",
                      "source": "https://u:PROXYPASS@example.test/mailbox/URLPATHSECRET?token=URLTOKENSECRET#frag",
                      "headers": "Authorization: Bearer HEADERSECRET"},
            request={"images": ["https://secret.test/OTPURLSECRET"], "otp_url": "OTPTOKEN"},
            error="Failed token=ERRORTOKEN password='ERRORPASSWORD' Cookie: abc=ERRORCOOKIE")
        before = copy.deepcopy(task)
        task["result"].update(path="/private/media/path", b64_json="B64SECRET",
                              upstream_token="RESULTSECRET")
        before = copy.deepcopy(task)
        view = self.view(task)
        text = json.dumps(view, ensure_ascii=False)
        for secret in ("request-hash-secret", "request-key-secret", "token-secret", "internal-secret",
                       "PAYLOADSECRET", "COOKIESECRET", "OTHERSECRET", "HEADERSECRET", "KEYSECRET",
                       "raw-reference-secret", "PASSWORDSECRET", "/Users/private", r"C:\private",
                       "/tmp/private", "PROXYPASS", "URLPATHSECRET", "URLTOKENSECRET", "OTPURLSECRET",
                       "OTPTOKEN", "ERRORTOKEN", "ERRORPASSWORD", "ERRORCOOKIE", "B64SECRET", "RESULTSECRET"):
            with self.subTest(secret=secret):
                self.assertNotIn(secret, text)
        self.assertIn("Keep cat.", view["prompt"])
        self.assertEqual(view["metadata"]["nested"], [{"keep": "visible"}])
        self.assertEqual(task, before)

    def test_filename_authoritative_over_conflicting_urls(self):
        self.put(IMAGE)
        self.put(VIDEO)
        task = self.task(url=local_url(VIDEO))
        task["result"]["url"] = "https://evil.test/" + VIDEO
        view = self.view(task)
        self.assertEqual(view["media"]["filename"], IMAGE)
        self.assertEqual(view["url"], local_url(IMAGE))
        self.assertEqual(view["result"]["url"], local_url(IMAGE))

    def test_unsafe_filenames_cannot_fallback_to_valid_url(self):
        self.put()
        for name in ("../" + IMAGE, "/tmp/" + IMAGE, "sub/" + IMAGE, "sub\\" + IMAGE,
                     "%2e%2e%2f" + IMAGE, IMAGE + ".html", IMAGE + "?x=.mp4", IMAGE + "#x",
                     IMAGE + "\x00", " " + IMAGE, "." + IMAGE, "plain.svg", "a" * 32 + ".html"):
            with self.subTest(name=name):
                view = self.view(self.task(name, url=local_url(IMAGE)))
                self.assertEqual(view["media"]["availability"], "unknown")
                self.assertIsNone(view["media"]["filename"])
                self.assertIsNone(view["media"]["url"])
                self.assertNotIn("filename", view["result"])
                self.assertNotIn("url", view)

    def test_trusted_local_and_configured_public_url_only_legacy(self):
        self.put()
        for url in (local_url(IMAGE), public_url(IMAGE), public_url(IMAGE) + "?token=QUERYSECRET#frag"):
            with self.subTest(url=url):
                view = self.view({"id": "old", "kind": "image", "status": "completed", "url": url}, url_fn=public_url)
                self.assertTrue(view["media"]["exists"])
                self.assertEqual(view["url"], public_url(IMAGE))
                self.assertNotIn("QUERYSECRET", json.dumps(view))
        view = self.view({"kind": "video", "status": "completed", "result": {"url": local_url(IMAGE)}})
        self.assertEqual(view["media"]["kind"], "image")

    def test_untrusted_and_ambiguous_urls_cannot_be_local_files(self):
        self.put()
        urls = ("https://evil.test" + local_url(IMAGE), "https://media.example.test.evil" + local_url(IMAGE),
                "http://media.example.test/service" + local_url(IMAGE), "https://media.example.test:444/service" + local_url(IMAGE),
                PUBLIC + "/other/" + IMAGE, "//media.example.test/service" + local_url(IMAGE),
                "/other/" + IMAGE, "/v1/media/../" + IMAGE, "/v1/media/%2e%2e/" + IMAGE,
                local_url(IMAGE) + "/suffix", local_url(IMAGE) + ".html", "file:///tmp/" + IMAGE,
                "data:image/png;base64,SECRET", "javascript:alert(1)", PUBLIC + "/%76%31/media/" + IMAGE,
                "https://user:password@media.example.test/service" + local_url(IMAGE),
                local_url(IMAGE) + "\n", "https://[invalid/" + IMAGE)
        for url in urls:
            with self.subTest(url=url):
                view = self.view({"kind": "image", "status": "completed", "url": url}, url_fn=public_url)
                self.assertEqual(view["media"]["availability"], "unknown")
                self.assertIsNone(view["media"]["url"])
                self.assertNotIn("url", view)

    def test_legacy_image_response_only_first_item_and_trusted_fields(self):
        self.put()
        legacy = {"created": 123, "data": [{"url": local_url(IMAGE), "bytes": 18,
                    "size": "1024x1024", "width": 640, "height": 480,
                    "revised_prompt": "cat", "b64_json": "BASE64SECRET", "token": "TOKEN"},
                    {"url": "https://evil.test/SECONDSECRET", "b64_json": "MORESECRET"}]}
        for task in ({"image_response": legacy}, {"result": {"image_response": legacy}}):
            with self.subTest(task=task):
                view = self.view({"kind": "image", "status": "completed", **task})
                self.assertTrue(view["media"]["exists"])
                self.assertEqual(view["media"]["width"], 640)
                response = view.get("image_response") or view["result"]["image_response"]
                self.assertEqual(response["created"], 123)
                self.assertEqual(len(response["data"]), 1)
                self.assertEqual(response["data"][0]["url"], local_url(IMAGE))
                for secret in ("BASE64SECRET", "TOKEN", "SECONDSECRET", "MORESECRET"):
                    self.assertNotIn(secret, json.dumps(view))

    def test_kind_comes_from_media_extension_not_requested_kind(self):
        for name, kind in ((IMAGE, "image"), (VIDEO, "video"), (AUDIO, "audio")):
            with self.subTest(kind=kind):
                self.put(name)
                task = self.task(name, kind="video")
                task["result"]["kind"] = "video"
                task["result"]["mime"] = "video/mp4"
                view = self.view(task)
                self.assertEqual(view["media"]["kind"], kind)
                self.assertEqual(view["result"]["kind"], kind)

    def test_generic_extension_needs_actual_result_mime_or_extension(self):
        for meta in ({"mime": "audio/mpeg"}, {"ext": ".mp3"}):
            name = "c" * 32 + ".bin"
            self.put(name)
            task = self.task(name)
            task["result"].update(meta)
            self.assertEqual(self.view(task)["media"]["kind"], "audio")
            self.assertTrue(self.view(task)["media"]["exists"])
        name = "c" * 32 + ".dat"
        self.put(name)
        view = self.view(self.task(name, kind="image"))
        self.assertIsNone(view["media"]["kind"])
        self.assertEqual(view["media"]["availability"], "unknown")
        self.assertFalse(view["media"]["exists"])

    def test_no_dimensions_or_byte_sizes_inferred_from_request(self):
        task = self.task(size="8192x8192", width=8192, height=8192)
        task["result"] = {"filename": IMAGE, "size": "8192x8192", "w": 0, "h": -1}
        view = self.view(task)
        self.assertNotIn("width", view["media"])
        self.assertNotIn("height", view["media"])
        self.assertIsNone(view["media"]["bytes"])
        task["result"]["metadata"] = {"width": 1024, "height": 512}
        view = self.view(task)
        self.assertEqual(view["media"]["width"], 1024)
        self.assertEqual(view["media"]["height"], 512)

    def test_symlink_hardlink_directory_and_fifo_are_not_media(self):
        outside = self.root / "outside.png"
        outside.write_bytes(b"OUTSIDE SECRET")
        link = self.media / IMAGE
        link.symlink_to(outside)
        self.assertEqual(self.view(self.task())["media"]["availability"], "unknown")
        self.assertIsNone(self.view(self.task())["media"]["url"])
        self.assertEqual(outside.read_bytes(), b"OUTSIDE SECRET")
        link.unlink()
        os.link(outside, link)
        self.assertEqual(self.view(self.task())["media"]["availability"], "unknown")
        link.unlink()
        link.mkdir()
        self.assertEqual(self.view(self.task())["media"]["availability"], "unknown")
        link.rmdir()
        os.mkfifo(link)
        self.assertEqual(self.view(self.task())["media"]["availability"], "unknown")

    def test_symlink_media_directory_is_not_followed(self):
        self.put()
        linked = self.root / "linked"
        linked.symlink_to(self.media, target_is_directory=True)
        view = self.view(self.task(), media=linked)
        self.assertEqual(view["media"]["availability"], "unknown")
        self.assertFalse(view["media"]["exists"])
        self.assertIsNone(view["media"]["url"])

    def test_permission_or_directory_replacement_failure_is_unknown(self):
        self.put()
        with patch.object(task_views.os, "open", side_effect=PermissionError("sensitive path")):
            self.assertEqual(self.view(self.task())["media"]["availability"], "unknown")
        real_stat = os.stat
        calls = []
        def stat_race(path, *args, **kwargs):
            calls.append(path)
            if len(calls) == 3:
                return real_stat(self.root, follow_symlinks=False)
            return real_stat(path, *args, **kwargs)
        with patch.object(task_views.os, "stat", side_effect=stat_race):
            self.assertEqual(self.view(self.task())["media"]["availability"], "unknown")

    def test_projection_never_reads_media_or_scans_or_uses_network(self):
        self.put()
        with patch("builtins.open", side_effect=AssertionError("file content read")), \
             patch.object(Path, "read_bytes", side_effect=AssertionError("file content read")), \
             patch.object(os, "listdir", side_effect=AssertionError("directory scan")), \
             patch.object(os, "scandir", side_effect=AssertionError("directory scan")), \
             patch("urllib.request.urlopen", side_effect=AssertionError("remote fetch")), \
             patch("socket.socket", side_effect=AssertionError("network socket")):
            self.assertTrue(self.view(self.task())["media"]["exists"])

    def test_bad_public_callback_falls_back_without_exposing_details(self):
        self.put()
        def broken(name):
            raise ValueError("SENSITIVE PATH COOKIE")
        self.assertEqual(self.view(self.task(), url_fn=broken)["url"], local_url(IMAGE))
        for value in ("javascript:evil", "//evil.test/x", "https://u:secret@host/v1/media/" + IMAGE,
                      public_url(IMAGE) + "?token=secret", "/wrong/" + IMAGE, None):
            with self.subTest(value=value):
                self.assertEqual(self.view(self.task(), url_fn=lambda name: value)["url"], local_url(IMAGE))

    def test_json_safe_copy_drops_objects_handles_cycles_and_nonfinite(self):
        class NoString:
            def __str__(self):
                raise AssertionError("must not stringify arbitrary object")
        metadata = {"object": NoString(), "nan": float("nan"), "infinity": float("inf"), "keep": [1, 2]}
        metadata["cycle"] = metadata
        view = self.view({"id": "task_json", "metadata": metadata})
        self.assertNotIn("object", view["metadata"])
        self.assertNotIn("cycle", view["metadata"])
        self.assertIsNone(view["metadata"]["nan"])
        self.assertIsNone(view["metadata"]["infinity"])
        json.dumps(view, allow_nan=False)


if __name__ == "__main__":
    unittest.main(verbosity=2)
