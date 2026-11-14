"""Generation targets, request-to-account evidence and safe task-detail integration."""
import asyncio
from datetime import datetime, timezone
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
HOME=tempfile.TemporaryDirectory(prefix="muse-studio-integration-")
os.environ.update(MUSE2API_HOME=HOME.name,MUSE2API_PROFILE_ROOT=HOME.name,MUSE2API_KEY="test-only")
import app
from generation_specs import validate_geometry
from quota_dates import reset_metadata
from maintenance import operation_due


class StudioBackendTests(unittest.TestCase):
    def setUp(self):
        self.a=app.store.add_account({"hatch_sess":"studio-"+__import__('uuid').uuid4().hex},"Studio account")

    def test_image_and_video_preserve_resolution_and_reference_counts(self):
        for cls,kind in ((app.ImageRequest,"image"),(app.VideoRequest,"video")):
            req=cls(prompt="animate",size="1080x1920",aspect_ratio="9:16",resolution="1080x1920")
            prompt=(app.build_image_prompt(req) if kind=="image" else app.build_video_prompt(req))
            self.assertIn("1080x1920",prompt)
            self.assertNotIn("720x1280",prompt)
            meta=app._generation_metadata(req,["data:image/png;base64,eA==","data:image/png;base64,eQ==","data:audio/wav;base64,eA=="])
            self.assertEqual(meta["reference_counts"],{"image":2,"audio":1})
            self.assertEqual(meta["resolution"],"1080x1920")
            self.assertNotIn("base64",json.dumps(meta))

    def test_geometry_conflicts_rejected_and_rounding_allowed(self):
        for cls in (app.ImageRequest,app.VideoRequest):
            with self.assertRaises(ValueError):cls(prompt="x",size="1024x1024",aspect_ratio="16:9")
            with self.assertRaises(ValueError):cls(prompt="x",size="1x99999",resolution="90000x100")
            with self.assertRaises(ValueError):cls(prompt="x",size="1920x1080",resolution="1024x1024")
            cls(prompt="x",size="1024x576",aspect_ratio="16:9",resolution="1024x576")
            cls(prompt="x",size="1024x577",aspect_ratio="16:9")

    def test_failed_request_maps_account_before_session_renewal(self):
        tid=app.store.create_task("video","private prompt")["id"]
        def generate():
            return app._run_generation("prompt","video",30,account_id=self.a["id"])
        with patch.object(app,"_renew_and_persist",side_effect=app.MuseAuthError("session denied")), \
             patch.object(app.engine,"close_current_page"),patch.object(app.engine,"reset_thread"):
            app._task_runner(tid,generate)
        saved=app.store.get_task(tid)
        self.assertEqual(saved["status"],"failed")
        self.assertEqual(saved["account_id"],saved["attempts"][-1]["account_id"])
        self.assertTrue(saved["account_label"])
        logs=app.get_task_logs(tid)["entries"]
        assigned=[x for x in logs if x["event"]=="generation.assigned"]
        self.assertTrue(assigned)
        self.assertEqual(assigned[-1]["account_id"],self.a["id"])
        failed=next(x for x in logs if x["event"]=="task.failed")
        self.assertEqual(failed["account_id"],saved["account_id"])
        self.assertTrue(failed["account_label"])
        self.assertFalse(app.ACCOUNT_POOL.busy_accounts)
        self.assertFalse(app.GEN_LOCK.locked())

    def test_task_detail_available_then_deleted_without_rerunning(self):
        name="1234567890abcdef1234567890abcdef.mp4"
        path=Path(app.CFG.media_dir)/name;path.write_bytes(b"fake-video")
        tid=app.store.create_task("video","preview fixture")["id"]
        app.store.update_task(tid,status="completed",account=self.a["id"],result={"filename":name,"url":app.media_url(name),"bytes":10,"kind":"video"},generation={"aspect_ratio":"16:9","resolution":"1920x1080"})
        detail=app.admin_task_detail(tid)["task"]
        self.assertEqual(detail["account_label"],"Studio account")
        self.assertTrue(detail["media"]["exists"])
        self.assertEqual(detail["media"]["kind"],"video")
        path.unlink()
        detail=app.admin_task_detail(tid)["task"]
        self.assertEqual(detail["status"],"completed")
        self.assertFalse(detail["media"]["exists"])
        self.assertEqual(detail["media"]["availability"],"deleted")

    def test_reset_date_is_not_fabricated_exact_time(self):
        checked=int(datetime(2026,11,14,2,tzinfo=timezone.utc).timestamp())
        metadata=reset_metadata("Nov 15",checked)
        self.assertEqual(metadata["weekly_reset_date"],"2026-11-15")
        self.assertNotIn("weekly_reset_at",metadata)
        self.assertEqual(reset_metadata("unknown",checked),{})
        self.assertEqual(reset_metadata("Sep 20",checked),{})
        exact=reset_metadata("2026-11-15T02:00:00+08:00",checked)
        self.assertIn("weekly_reset_at",exact)
        self.assertEqual(reset_metadata("2026-11-15T02:00:00",checked),{})

    def test_expiring_session_maintenance_preempts_long_interval_but_not_backoff(self):
        now=1900000000
        acc={"last_keepalive":now-1200,"keepalive_checked_at":now-1200,"expires_at":now+300}
        self.assertTrue(operation_due(acc,"keepalive",360,now))
        acc["keepalive_checked_at"]=now-10
        self.assertFalse(operation_due(acc,"keepalive",360,now))

    def test_sync_image_task_keeps_result_without_duplicate_task_on_poll(self):
        req=app.ImageRequest(prompt="sync image",size="1024x1024",resolution="1024x1024")
        name="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.png"
        result={"filename":name,"size":3,"kind":"image","w":1024,"h":1024}
        before=len(app.store.tasks)
        with patch.object(app,"_run_generation",return_value=(result,self.a["id"])):
            returned=app._run_sync_image_task(req,app.build_image_prompt(req),[],30)
        self.assertEqual(returned[0],result)
        self.assertEqual(len(app.store.tasks),before+1)
        task=next(t for t in app.store.tasks.values() if t.get("prompt")=="sync image")
        self.assertEqual(task["status"],"completed")
        self.assertEqual(task["generation"]["resolution"],"1024x1024")
        self.assertEqual(task["result"]["w"],1024)

    def test_legacy_media_index_is_once_and_does_not_fabricate_original_account(self):
        from media_history import index_legacy_media
        with tempfile.TemporaryDirectory() as home:
            from config import Config
            from store import Store
            cfg=Config(base_dir=home,profile_root=home);cfg.ensure_dirs();store=Store(cfg)
            media=Path(cfg.media_dir); marker=Path(cfg.data_dir)/".media-history-index-v1.json"
            name="abababababababababababababababab.webp";(media/name).write_bytes(b"legacy")
            (media/"not-generated.txt").write_text("not media")
            first=index_legacy_media(store,media,marker,app.media_url)
            self.assertEqual(first["indexed"],1)
            task=next(iter(store.tasks.values()))
            self.assertTrue(task["historical_media"])
            self.assertEqual(task["generation"],{})
            self.assertNotIn("account_id",task)
            self.assertEqual(index_legacy_media(store,media,marker,app.media_url)["indexed"],0)
            self.assertEqual(len(store.tasks),1)
            self.assertEqual(task["status"],"completed")

    def test_global_refresh_overrides_toggles_without_reenabling_disabled_accounts(self):
        app.maintenance_settings.update({"keepalive_enabled":False,"quota_enabled":False,"include_disabled":False})
        calls=[]
        def keep(aid,check_quota=False):calls.append(("keepalive",aid));return {"ok":True,"id":aid}
        def quota(aid,background=False):calls.append(("quota",aid));return {"found":True,"checked_at":1900000000}
        try:
            with patch.object(app,"_probe_account_sync",side_effect=keep),patch.object(app,"_query_quota_sync",side_effect=quota):
                asyncio.run(app.run_account_maintenance(force=True,refresh_all=True))
            self.assertIn(("keepalive",self.a["id"]),calls)
            self.assertIn(("quota",self.a["id"]),calls)
        finally:
            app.maintenance_settings.update({"keepalive_enabled":True,"quota_enabled":True})


if __name__=="__main__":
    try:unittest.main()
    finally:app.login_service.close();HOME.cleanup()
