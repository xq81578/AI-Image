import copy
import json
from pathlib import Path
import shutil
import unittest
from unittest.mock import patch
import uuid

from PIL import Image

from codex_video import CHECKS, DEFAULTS, ROOT, Workspace, compose


class CodexVideoTests(unittest.TestCase):
    def setUp(self):
        # Windows 沙箱下 mkdtemp 的 0700 ACL 会阻止当前受限进程写入。
        self.root = ROOT / "outputs" / "codex-time-echo" / ("test_" + uuid.uuid4().hex)
        self.root.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, self.root)
        (self.root / "codex-video.json").write_text(json.dumps(DEFAULTS), encoding="utf-8")
        self.work = Workspace(self.root)
        with patch("codex_video.preflight", return_value=["music.mp3"]):
            result = self.work.begin()
        self.folder, self.data = self.work.load(result["run_id"])
        self.source = self.root / "image.png"
        Image.new("RGB", (800, 900), "#557766").save(self.source)
        self.plan = {"title": "候车廊", "old_life": "等车回家", "loss": "停运后不再候车",
                     "change_route": "改为库房", "caption": "再也等不到那辆车",
                     "anchors": ["廊柱", "台阶"], "changes": ["拆牌", "封窗", "拆凳"]}
        self.work.plan(self.folder, self.data, 1, self.plan)

    def review(self, kind):
        result = {"passed": True, "checks": {k: True for k in CHECKS[kind]},
                  "observations": "实际查看后记录测试证据"}
        if kind == "now":
            result.update(visible_changes=["拆牌留孔", "窗口封堵", "墙面翻修"], loss_evidence="候车活动消失")
        return result

    def accept(self, kind):
        attempt = self.work.attempt(self.folder, self.data, 1, kind, "完整测试提示词")
        return self.work.record(self.folder, self.data, attempt["id"], self.source, self.review(kind))

    def test_resume_preserves_count_years_and_attempt_budget(self):
        attempt = self.work.attempt(self.folder, self.data, 1, "old", "first")
        with patch("codex_video.preflight", side_effect=AssertionError("恢复不重新选音乐")):
            resumed = self.work.begin()
        self.assertTrue(resumed["resumed"])
        self.assertEqual(resumed["run_id"], self.data["run_id"])
        self.assertEqual(resumed["pairs"], self.data["pairs"])
        self.assertEqual(resumed["image_calls"], 1)
        self.work.fail(self.folder, self.data, attempt["id"], "请求中断")
        self.data["config"]["max_image_calls"] = 1
        with self.assertRaisesRegex(ValueError, "上限"):
            self.work.attempt(self.folder, self.data, 1, "old", "retry")

    def test_requires_old_acceptance_and_serial_requests(self):
        with self.assertRaisesRegex(ValueError, "旧照"):
            self.work.attempt(self.folder, self.data, 1, "now", "now")
        self.work.attempt(self.folder, self.data, 1, "old", "old")
        self.work.plan(self.folder, self.data, 2, self.plan)
        with self.assertRaisesRegex(ValueError, "串行"):
            self.work.attempt(self.folder, self.data, 2, "old", "old2")

    def test_quality_failure_does_not_unlock_next_stage(self):
        attempt = self.work.attempt(self.folder, self.data, 1, "old", "old")
        review = self.review("old")
        review["checks"]["anatomy"] = False
        with self.assertRaisesRegex(ValueError, "未通过"):
            self.work.record(self.folder, self.data, attempt["id"], self.source, review)
        review["passed"] = False
        record = self.work.record(self.folder, self.data, attempt["id"], self.source, review)
        self.assertEqual(record["status"], "rejected")
        self.assertTrue(Path(record["path"]).is_file())
        self.assertIsNone(self.data["pairs"][0]["old"])

    def test_now_needs_visible_temporal_evidence(self):
        self.accept("old")
        attempt = self.work.attempt(self.folder, self.data, 1, "now", "now")
        review = self.review("now")
        review["visible_changes"] = ["只是人少了"]
        with self.assertRaisesRegex(ValueError, "三项"):
            self.work.record(self.folder, self.data, attempt["id"], self.source, review)
        review = self.review("now")
        review["checks"]["loss"] = False
        review["passed"] = False
        self.work.record(self.folder, self.data, attempt["id"], self.source, review)
        self.assertIsNone(self.data["pairs"][0]["now"])

    def test_replacement_keeps_audit_and_budget(self):
        self.accept("old")
        old = copy.deepcopy(self.data["pairs"][0])
        self.work.replace(self.folder, self.data, 1, "选题无法产生可见变迁")
        self.assertEqual(self.data["image_calls"], 1)
        self.assertEqual(self.data["events"][0]["pair"], old)
        self.assertEqual(self.data["pairs"][0]["old_year"], old["old_year"])
        self.assertEqual(self.data["pairs"][0]["candidate"], 2)
        self.assertTrue(Path(old["old"]["path"]).exists())

    def test_incomplete_or_modified_assets_cannot_render(self):
        self.accept("old")
        self.accept("now")
        with self.assertRaisesRegex(ValueError, "尚未审核"):
            self.work.ready_frames(self.folder, self.data)
        path = Path(self.data["pairs"][0]["old"]["path"])
        Image.new("RGB", (800, 900), "red").save(path)
        with self.assertRaisesRegex(ValueError, "被修改"):
            self.work.ready_frames(self.folder, self.data)

    def test_composition_preserves_full_wide_source(self):
        source = Image.new("RGB", (1600, 800), "green")
        for x in range(80):
            source.paste("red", (x, 0, x + 1, 800))
            source.paste("blue", (1599 - x, 0, 1600 - x, 800))
        source.save(self.source)
        output = self.root / "stacked.png"
        compose(self.source, self.source, 2010, 2026, output)
        with Image.open(output) as result:
            self.assertEqual(result.size, (1080, 1920))
            self.assertEqual(result.getpixel((10, 480)), (255, 0, 0))
            self.assertEqual(result.getpixel((1070, 1440)), (0, 0, 255))

    def test_cleanup_failure_resumes_without_starting_new_batch(self):
        self.accept("old")
        self.data.update(status="rendered", video=str(self.folder / "video.mp4"))
        review = {"framing": True, "years": True, "order": True, "transitions": True, "observations": "已查看抽帧"}
        with patch("codex_video.validate_video", return_value={}), patch.object(Path, "unlink", side_effect=PermissionError("busy")):
            with self.assertRaises(PermissionError):
                self.work.finish(self.folder, self.data, review)
        resumed = self.work.begin()
        self.assertEqual(resumed["status"], "finishing")
        self.assertEqual(resumed["run_id"], self.data["run_id"])
        with patch("codex_video.validate_video", return_value={}):
            self.work.finish(self.folder, self.data, review)
        self.assertEqual(self.data["status"], "done")
        self.assertEqual(list((self.folder / "assets").glob("*.png")), [])


if __name__ == "__main__":
    unittest.main()
