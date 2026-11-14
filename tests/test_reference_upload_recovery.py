"""参考素材上传的容错回归（线上 11-13 东京节点 3/4、2/4、找不到上传控件）。

Run: python tests/test_reference_upload_recovery.py
用假页面模拟 muse composer：输入框时有时无、会吞掉个别文件；时钟是假的，
所以 240 秒预算的用例也能毫秒级跑完。
"""
import importlib.util
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


class Clock:
    """只实现被 engine 用到的单调时钟 + 睡眠。"""

    def __init__(self):
        self.now = 1000.0

    def monotonic(self):
        return self.now

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.now += max(0.0, seconds)


class FakePage:
    """muse composer 的最小行为模型。

    missing_lookups: 前 N 次查控件返回 null（React 重建 input 的真实时序）
    drop_calls:      第 N 次 setFileInputFiles 被静默吞掉（线上 3/4 的真凶）
    """

    def __init__(self, missing_lookups=0, drop_calls=()):
        self.missing_lookups = missing_lookups
        self.drop_calls = set(drop_calls)
        self.lookups = 0
        self.sets = []
        self.accepted = []

    def send(self, method, params=None, timeout=None):
        if method == "Runtime.evaluate":
            self.lookups += 1
            if self.lookups <= self.missing_lookups:
                return {"result": {"result": {}}}
            return {"result": {"result": {"objectId": "fixture-node"}}}
        if method == "DOM.setFileInputFiles":
            self.sets.append(params["files"][0])
            if len(self.sets) not in self.drop_calls:
                self.accepted.append(params["files"][0])
            return {"result": {}}
        raise AssertionError(method)

    def js(self, script, **kwargs):
        return str(len(self.accepted))


def make_engine(module):
    borrow = module.MuseEngine(SimpleNamespace(profile_dir=tempfile.gettempdir()))
    return borrow


def main():
    sys.path.insert(0, str(ROOT))
    spec = importlib.util.spec_from_file_location("upload_recovery_target", ROOT / "muse2api" / "engine.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)

    engine = make_engine(module)
    clock = Clock()

    with patch.object(module, "time", clock):
        # 1) 控件晚出现：轮询到为止，不再一次查不到就判死
        page = FakePage(missing_lookups=3)
        engine.page = page
        engine._set_input_files(["/tmp/1.png"])
        assert page.lookups == 4, page.lookups
        assert page.sets == ["/tmp/1.png"], page.sets
        print("late_input=PASS")

        # 2) 正常 4 张：每张都要「确认进了输入框」才投下一张
        page = FakePage()
        engine.page = page
        paths = ["/tmp/1.png", "/tmp/2.png", "/tmp/3.png", "/tmp/4.png"]
        engine._upload_references(paths)
        assert page.accepted == paths, page.accepted
        assert len(page.sets) == 4, page.sets
        print("four_uploads_in_order=PASS")

        # 3) 第 4 张被吞掉（线上 3/4）：补投同一张，最终仍是 4/4
        page = FakePage(drop_calls={4})
        engine.page = page
        engine._upload_references(paths)
        assert page.accepted == paths, page.accepted
        assert page.sets == paths + [paths[3]], page.sets
        print("swallowed_file_retried=PASS")

        # 4) 全部被吞：报明确进度，而不是静默成功
        page = FakePage(drop_calls=set(range(1, 40)))
        engine.page = page
        try:
            engine._upload_references(paths)
        except module.MuseGenerationError as exc:
            assert "0/4" in str(exc), str(exc)
        else:
            raise AssertionError("全部素材被吞时必须失败")
        print("upload_incomplete_reported=PASS")

        # 5) 总预算受任务 deadline 约束（不能无限补投）
        page = FakePage(drop_calls=set(range(1, 40)))
        engine.page = page
        started = clock.now
        try:
            engine._upload_references(paths, deadline=clock.monotonic() + 60)
        except module.MuseGenerationError:
            pass
        else:
            raise AssertionError("预算耗尽后必须停止补投")
        # 60 秒预算 + 一次轮询粒度，不能滚成几分钟
        assert clock.now - started <= 75, clock.now - started
        print("upload_budget_bounded=PASS")

    # 6) 进行时播报不能被当成「模型拒绝」
    pending = ("正在生成视频，请稍候…", "视频正在渲染，大约需要 2 分钟",
               "正在为你渲染视频，请耐心等待",
               "Rendering your video, this may take a couple of minutes",
               "Generating your video now...", "This may take a few minutes.")
    for text in pending:
        assert module.MuseEngine._media_status_pending(text) is True, text
    refusals = ("视频没生成成功：你这个账号所在的国家/地区目前不支持视频生成。",
                "视频没有生成成功，请重新尝试。",
                "The video could not be generated.")
    for text in refusals:
        assert module.MuseEngine._media_status_pending(text) is False, text
    print("progress_text_not_refusal=PASS")

    # 7) 地区限制要能识别出来（账号级 -> 换号，而不是任务终态）
    assert module.MuseEngine._is_region_refusal(
        "视频没生成成功：你这个账号所在的国家/地区目前不支持视频生成。") is True
    assert module.MuseEngine._is_region_refusal(
        "Your video is not available in your region or country.") is True
    assert module.MuseEngine._is_region_refusal("视频生成失败，请重试。") is False
    print("region_refusal_classified=PASS")

    print("PASS: upload recovery, budget, progress text and region classification")


if __name__ == "__main__":
    main()
