import json
import shutil
import unittest
from unittest.mock import patch
import uuid

from codex_video import DEFAULTS, PUBLISH_CHECKS, ROOT, ExecutionBusy, Workspace


class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.root = ROOT / "outputs" / "codex-time-echo" / ("test_" + uuid.uuid4().hex)
        self.root.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, self.root)
        (self.root / "codex-video.json").write_text(json.dumps(DEFAULTS), encoding="utf-8")
        self.work = Workspace(self.root)
        with patch("codex_video.preflight", return_value=["music.mp3"]):
            result = self.work.execute("begin", owner="thread-a")
        self.run_id = result["run_id"]
        self.token = result["execution"]["token"]
        self.folder, self.data = self.work.load(self.run_id)
        self.video = self.folder / "video.mp4"
        self.video.write_bytes(b"isolated-video-fixture")
        self.data.update(status="rendered", video=str(self.video))
        self.work.save(self.folder, self.data)
        review = {"framing": True, "years": True, "order": True, "transitions": True, "observations": "测试成片审核"}
        with patch("codex_video.validate_video", return_value={}):
            self.call("finish", review)
        self.info = {"run_id": self.run_id, "video": str(self.video), "short_title": "旧日难回",
                     "description": "AI 创作场景", "hashtags": ["旧时光"],
                     "settings": {"location": "不显示位置", "collection": "旧时光", "link": None,
                                  "activity": "不参与活动", "schedule": "不定时", "original_requested": True,
                                  "ai_label": "含AI生成内容"}}

    def call(self, action, payload=None, **kwargs):
        path = self.root / "input.json"
        if payload is not None:
            path.write_text(json.dumps(payload), encoding="utf-8")
        return self.work.execute(action, owner="thread-a", token=self.token, run_id=self.run_id,
                                 file=path, **kwargs)

    def step(self, status, **kwargs):
        return self.call("publish-step", {"status": status, "evidence": "测试页面观察", **kwargs})

    def ready(self):
        self.call("publish-init", self.info)
        self.step("uploading")
        return self.step("ready", account="测试视频号", checks={k: True for k in PUBLISH_CHECKS})

    def test_finished_video_pending_publication_resumes_without_new_batch(self):
        before = (self.folder / "manifest.json").read_bytes()
        self.assertEqual(self.call("publish-status")["status"], "awaiting_publish")
        self.call("release", reason="等待浏览器接入")
        with patch("codex_video.preflight", side_effect=AssertionError("不能重新出片")):
            resumed = self.work.execute("begin", owner="thread-b")
        self.assertEqual(resumed["run_id"], self.run_id)
        self.assertEqual((self.folder / "manifest.json").read_bytes(), before)

    def test_prepare_checks_video_batch_and_screenshot_settings(self):
        for key, value in (("run_id", "wrong"), ("video", str(self.root / "other.mp4")),
                           ("settings", {}), ("short_title", "")):
            info = {**self.info, key: value}
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.call("publish-init", info)
        self.assertEqual(self.call("publish-status")["status"], "awaiting_publish")

    def test_other_executor_cannot_change_publication(self):
        before = (self.folder / "publish-progress.json").read_bytes()
        for action in ("publish-init", "publish-step"):
            with self.subTest(action=action), self.assertRaises(ExecutionBusy):
                self.work.execute(action, owner="thread-b", token=self.token, run_id=self.run_id)
        self.assertEqual((self.folder / "publish-progress.json").read_bytes(), before)

    def test_login_wait_and_qr_refresh_do_not_enable_early_submission(self):
        self.call("publish-init", self.info)
        self.step("waiting_login", qr_event="qr:1")
        self.step("waiting_login", qr_event="qr:2")
        with self.assertRaises(ValueError):
            self.step("submitting")
        self.assertEqual(self.call("publish-status")["status"], "waiting_login")

    def test_ready_requires_actual_account_and_all_form_checks(self):
        self.call("publish-init", self.info)
        self.step("uploading")
        for checks in ({}, {k: k != "ai_label" for k in PUBLISH_CHECKS}, "invalid"):
            with self.subTest(checks=checks), self.assertRaises(ValueError):
                self.step("ready", account="测试视频号", checks=checks)
        with self.assertRaises(ValueError):
            self.step("ready", checks={k: True for k in PUBLISH_CHECKS})

    def test_changed_video_cannot_be_submitted(self):
        self.ready()
        self.video.write_bytes(b"different-video")
        with self.assertRaisesRegex(ValueError, "成片已变化"):
            self.step("submitting")
        self.assertEqual(self.call("publish-status")["status"], "ready")

    def test_preparation_is_idempotent_and_cannot_reset_submitting(self):
        self.ready()
        self.step("submitting")
        before = (self.folder / "publish-progress.json").read_bytes()
        self.assertEqual(self.call("publish-init", self.info)["status"], "submitting")
        self.assertEqual((self.folder / "publish-progress.json").read_bytes(), before)
        altered = {**self.info, "short_title": "新标题"}
        with self.assertRaises(ValueError):
            self.call("publish-init", altered)

    def test_uncertain_submission_requires_reconciliation_before_retry(self):
        self.ready()
        self.step("submitting")
        for state in ("submitting", "uploading", "blocked", "ready"):
            with self.subTest(state=state), self.assertRaises(ValueError):
                self.step(state)
        self.step("uncertain")
        checks = {k: True for k in PUBLISH_CHECKS}
        with self.assertRaisesRegex(ValueError, "明确证据"):
            self.step("ready", account="测试视频号", checks=checks)
        self.step("ready", account="测试视频号", checks=checks, confirmed_not_submitted=True)
        self.step("submitting")

    def test_published_requires_receipt_and_is_terminal(self):
        self.ready()
        with self.assertRaises(ValueError):
            self.step("published", receipt="不能跳过提交")
        self.step("submitting")
        with self.assertRaises(ValueError):
            self.step("published")
        self.step("published", receipt="测试平台发表成功提示及匹配作品记录")
        with self.assertRaises(ValueError):
            self.step("submitting")
        self.step("delivered", notification_receipts={k: {"status": "sent", "message_id": i + 1}
                  for i, k in enumerate(("complete", "video", "published"))})
        self.call("release", reason="发布完成")
        with patch("codex_video.preflight", return_value=["music.mp3"]):
            following = self.work.execute("begin", owner="thread-b")
        self.assertNotEqual(following["run_id"], self.run_id)

    def test_blocked_browser_preserves_done_video_and_can_prepare_later(self):
        before = (self.folder / "manifest.json").read_bytes()
        self.step("blocked", reason="Chrome 未接通")
        self.assertEqual((self.folder / "manifest.json").read_bytes(), before)
        self.call("publish-init", self.info)
        self.assertEqual(self.call("publish-status")["status"], "prepared")

    def test_status_is_read_only(self):
        before = (self.folder / "publish-progress.json").read_bytes()
        execution = self.work.execution()
        self.call("publish-status")
        self.assertEqual((self.folder / "publish-progress.json").read_bytes(), before)
        self.assertEqual(self.work.execution(), execution)

    def test_published_but_notification_failed_resumes_without_republishing(self):
        self.ready()
        self.step("submitting")
        self.step("published", receipt="测试发布成功")
        with self.assertRaisesRegex(ValueError, "通知尚未确认"):
            self.step("delivered", notification_receipts={"published": {"status": "failed"}})
        self.call("release", reason="通知失败但已发布")
        resumed = self.work.execute("begin", owner="thread-b")
        self.assertEqual(resumed["run_id"], self.run_id)
        self.assertEqual(self.work.publication(self.folder)["status"], "published")

    def test_title_can_be_shortened_before_submission_and_rechecked(self):
        self.ready()
        amended = {**self.info, "short_title": "旧日"}
        result = self.call("publish-init", amended)
        self.assertEqual(result["status"], "prepared")
        self.assertEqual(result["info"]["short_title"], "旧日")
        with self.assertRaises(ValueError):
            self.step("submitting")

    def test_repeat_finish_does_not_erase_publication_receipt(self):
        self.ready()
        self.step("submitting")
        self.step("published", receipt="测试成功回执")
        before = (self.folder / "publish-progress.json").read_bytes()
        review = {"framing": True, "years": True, "order": True, "transitions": True, "observations": "重复审核"}
        with patch("codex_video.validate_video", return_value={}):
            self.call("finish", review)
        self.assertEqual((self.folder / "publish-progress.json").read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
