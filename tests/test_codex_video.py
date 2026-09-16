import copy
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import unittest
from unittest.mock import patch
import uuid

from PIL import Image

from codex_video import CHECKS, DEFAULTS, ROOT, ExecutionBusy, Workspace, compose, exclusive


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

    def test_plan_can_choose_scene_appropriate_old_year(self):
        pair = self.data["pairs"][1]
        plan = dict(self.plan, old_year=1993)
        self.work.plan(self.folder, self.data, 2, plan)
        self.assertEqual(pair["old_year"], 1993)

        compatible_year = self.data["pairs"][2]["old_year"]
        self.work.plan(self.folder, self.data, 3, self.plan)
        self.assertEqual(self.data["pairs"][2]["old_year"], compatible_year)

        invalid = dict(self.plan, old_year=1979)
        with self.assertRaisesRegex(ValueError, "1980"):
            self.work.plan(self.folder, self.data, 4, invalid)

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


class ExecutionOwnershipTests(unittest.TestCase):
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

    def call_a(self, action, **kwargs):
        return self.work.execute(action, owner="thread-a", token=self.token, run_id=self.run_id, **kwargs)

    def snapshot(self):
        return {str(p.relative_to(self.work.output)): p.read_bytes()
                for p in self.work.output.rglob("*") if p.is_file()}

    def proof(self, **changes):
        current = self.work.execution()
        proof = {k: current[k] for k in ("owner_thread_id", "token", "revision")}
        proof.update(observed_status="interrupted", source="wait_threads",
                     checked_at=datetime.now(timezone.utc).isoformat(), evidence="测试状态工具报告原任务已中断")
        proof.update(changes)
        path = self.root / "stop-check.json"
        path.write_text(json.dumps(proof), encoding="utf-8")
        return path

    def add_pending(self):
        plan = {"title": "候车廊", "old_life": "等车", "loss": "停运", "change_route": "库房",
                "caption": "再也等不到", "anchors": ["廊柱", "台阶"], "changes": ["拆牌", "封窗", "拆凳"]}
        path = self.root / "plan.json"
        path.write_text(json.dumps(plan), encoding="utf-8")
        self.call_a("plan", pair=1, file=path)
        path = self.root / "prompt.txt"
        path.write_text("完整提示词", encoding="utf-8")
        return self.call_a("attempt", pair=1, kind="old", file=path)

    def test_other_thread_cannot_begin_or_mutate_any_step(self):
        self.add_pending()
        before = self.snapshot()
        for action in ("begin", "plan", "attempt", "record", "fail", "replace", "render", "finish", "block", "release"):
            with self.subTest(action=action), self.assertRaises(ExecutionBusy):
                self.work.execute(action, owner="thread-b", token=self.token, run_id=self.run_id,
                                  file=self.root / "missing.json", reason="不能误标失败")
            self.assertEqual(self.snapshot(), before)

    def test_missing_or_old_token_is_rejected_even_for_same_owner(self):
        before = self.snapshot()
        for token in (None, "old-token"):
            for action in ("begin", "block"):
                with self.subTest(token=token, action=action), self.assertRaises(ExecutionBusy):
                    self.work.execute(action, owner="thread-a", token=token, run_id=self.run_id, reason="失败")
        self.assertEqual(self.snapshot(), before)

    def test_read_only_status_does_not_refresh_or_change_execution(self):
        before = self.snapshot()
        for action in ("ownership", "status", "history"):
            self.work.execute(action, run_id=self.run_id)
        self.assertEqual(self.snapshot(), before)

    def test_owner_resume_uses_same_batch_without_refreshing_choices(self):
        self.add_pending()
        before = (self.folder / "manifest.json").read_bytes()
        revision = self.work.execution()["revision"]
        resumed = self.call_a("begin")
        self.assertTrue(resumed["resumed"])
        self.assertEqual(resumed["run_id"], self.run_id)
        self.assertEqual(resumed["image_calls"], 1)
        self.assertEqual((self.folder / "manifest.json").read_bytes(), before)
        self.assertGreater(resumed["execution"]["revision"], revision)

    def test_release_refuses_unresolved_generation(self):
        attempt = self.add_pending()
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, "未登记"):
            self.call_a("release", reason="停止")
        self.assertEqual(self.snapshot(), before)
        self.call_a("fail", attempt=attempt["id"], reason="已确认调用中断")
        manifest = (self.folder / "manifest.json").read_bytes()
        self.call_a("release", reason="正常保存后停止")
        resumed = self.work.execute("begin", owner="thread-b")
        self.assertEqual(resumed["run_id"], self.run_id)
        self.assertEqual(resumed["image_calls"], 1)
        self.assertEqual((self.folder / "manifest.json").read_bytes(), manifest)
        with self.assertRaises(ExecutionBusy):
            self.call_a("block", reason="旧执行者恢复")

    def test_done_remains_owned_until_explicit_release(self):
        self.data["status"] = "done"
        self.work.save(self.folder, self.data)
        before = self.snapshot()
        with self.assertRaises(ExecutionBusy):
            self.work.execute("begin", owner="thread-b")
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.call_a("begin")["run_id"], self.run_id)
        self.call_a("release", reason="完成交付")
        with patch("codex_video.preflight", return_value=["music.mp3"]):
            new = self.work.execute("begin", owner="thread-b")
        self.assertNotEqual(new["run_id"], self.run_id)
        with self.assertRaises(ExecutionBusy):
            self.call_a("finish")

    def test_ownership_never_expires_based_on_elapsed_time(self):
        execution = self.work.execution()
        execution["updated_at"] = "2000-01-01T00:00:00+00:00"
        (self.work.output / "execution.json").write_text(json.dumps(execution), encoding="utf-8")
        before = self.snapshot()
        with self.assertRaises(ExecutionBusy):
            self.work.execute("begin", owner="thread-b")
        self.assertEqual(self.snapshot(), before)

    def test_takeover_refuses_active_unknown_or_invalid_stop_checks(self):
        cases = [{"observed_status": state} for state in ("running", "waiting_for_tool", "unknown")]
        cases += [{"source": "guess"}, {"evidence": ""}, {"owner_thread_id": "wrong"},
                  {"checked_at": "bad"}, {"checked_at": datetime.now().isoformat()},
                  {"checked_at": (datetime.now(timezone.utc) - timedelta(seconds=61)).isoformat()}]
        before = self.snapshot()
        for changes in cases:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.work.execute("takeover", owner="thread-b", run_id=self.run_id, file=self.proof(**changes))
            self.assertEqual(self.snapshot(), before)

    def test_verified_takeover_preserves_pending_and_fences_previous_owner(self):
        pending = self.add_pending()
        before = (self.folder / "manifest.json").read_bytes()
        result = self.work.execute("takeover", owner="thread-b", run_id=self.run_id, file=self.proof())
        self.assertEqual(result["run_id"], self.run_id)
        self.assertEqual(result["pairs"][0]["attempts"][0]["status"], "pending")
        self.assertEqual((self.folder / "manifest.json").read_bytes(), before)
        self.assertNotEqual(result["execution"]["token"], self.token)
        for action in ("begin", "record", "fail", "release", "block"):
            with self.subTest(action=action), self.assertRaises(ExecutionBusy):
                self.call_a(action, attempt=pending["id"], reason="迟到的旧调用")
        self.work.execute("fail", owner="thread-b", token=result["execution"]["token"],
                          run_id=self.run_id, attempt=pending["id"], reason="接管后确认原调用中断")

    def test_owner_activity_invalidates_prior_stop_check(self):
        proof = self.proof()
        self.call_a("block", reason="原任务仍在写入")
        before = self.snapshot()
        with self.assertRaises(ExecutionBusy):
            self.work.execute("takeover", owner="thread-b", run_id=self.run_id, file=proof)
        self.assertEqual(self.snapshot(), before)

    def test_owner_resuming_invalidates_prior_stop_check(self):
        proof = self.proof()
        self.call_a("begin")
        before = self.snapshot()
        with self.assertRaises(ExecutionBusy):
            self.work.execute("takeover", owner="thread-b", run_id=self.run_id, file=proof)
        self.assertEqual(self.snapshot(), before)

    def test_two_takeovers_cannot_reuse_same_stop_check(self):
        proof = self.proof()
        self.work.execute("takeover", owner="thread-b", run_id=self.run_id, file=proof)
        before = self.snapshot()
        with self.assertRaises(ExecutionBusy):
            self.work.execute("takeover", owner="thread-c", run_id=self.run_id, file=proof)
        self.assertEqual(self.snapshot(), before)

    def test_file_lock_refuses_entry_without_changing_batch(self):
        before = self.snapshot()
        with exclusive(self.work.output / ".lock"), self.assertRaises(ExecutionBusy):
            self.work.execute("begin", owner="thread-b")
        self.assertEqual(self.snapshot(), before)

    def test_two_processes_racing_to_resume_have_one_owner(self):
        self.call_a("release", reason="交给下一轮")
        script = '''
import json, sys
from codex_video import Workspace, ExecutionBusy
try:
    result = Workspace(sys.argv[1]).execute("begin", owner=sys.argv[2])
    print(json.dumps({"status": "owned", "owner": result["execution"]["owner_thread_id"]}))
except ExecutionBusy:
    print(json.dumps({"status": "busy"}))
'''
        processes = []
        try:
            for owner in ("thread-b", "thread-c"):
                processes.append(subprocess.Popen([sys.executable, "-c", script, str(self.root), owner],
                    cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)))
            results = []
            for process in processes:
                stdout, stderr = process.communicate(timeout=15)
                self.assertEqual(process.returncode, 0, stderr.decode(errors="replace"))
                results.append(json.loads(stdout))
            self.assertCountEqual([r["status"] for r in results], ["owned", "busy"])
            owner = next(r["owner"] for r in results if r["status"] == "owned")
            self.assertEqual(self.work.execution()["owner_thread_id"], owner)
            self.assertEqual(self.work.execution()["run_id"], self.run_id)
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
                    process.communicate()

    def test_cli_uses_current_thread_and_rejects_owner_spoofing(self):
        from codex_video import main
        with patch.dict(os.environ, {"CODEX_THREAD_ID": "thread-b"}), patch("codex_video.Workspace", return_value=self.work):
            with patch.object(sys, "argv", ["codex_video.py", "begin", "--owner", "thread-a"]):
                with self.assertRaisesRegex(ValueError, "不得冒用"):
                    main()
            with patch.object(sys, "argv", ["codex_video.py", "begin"]):
                with self.assertRaises(ExecutionBusy):
                    main()


if __name__ == "__main__":
    unittest.main()
