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
                     "first_glance_contrast": "缩小对照仍能看出从正常候车变为长期失管",
                     "long_term_neglect": "多年封闭造成渗水、锈蚀和植物侵入",
                     "anchors": ["廊柱", "台阶"],
                     "changes": ["[功能] 候车设施拆除后不再使用",
                                 "[结构/维护] 窗口封堵并留下长期渗水痕迹",
                                 "[环境/设施/用途] 排水沟淤积且植物侵入"]}
        self.work.plan(self.folder, self.data, 1, self.plan)

    def review(self, kind):
        result = {"passed": True, "checks": {k: True for k in CHECKS[kind]},
                  "observations": "实际查看后记录测试证据"}
        if kind == "now":
            result.update(visible_changes=["[功能] 拆牌留孔", "[结构/维护] 窗口封堵",
                                          "[环境/设施/用途] 墙面翻修"],
                          loss_evidence="候车活动消失",
                          first_glance_evidence="缩小对照仍能看出候车功能退出",
                          long_term_neglect_evidence="封闭窗口、渗水和沟渠淤积共同证明长期失管")
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

    def test_plan_requires_contrast_fields_and_change_categories(self):
        for index, key in ((2, "first_glance_contrast"), (3, "long_term_neglect")):
            invalid = dict(self.plan, **{key: ""})
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, f"缺少 {key}"):
                self.work.plan(self.folder, self.data, index, invalid)
        invalid = dict(self.plan, changes=["[功能] 功能退出", "[结构/维护] 窗口封堵", "只写了变化"])
        with self.assertRaisesRegex(ValueError, "changes 必须覆盖"):
            self.work.plan(self.folder, self.data, 4, invalid)

    def test_now_attempt_rechecks_legacy_plan_requirements(self):
        self.accept("old")
        missing_field = dict(self.plan)
        missing_field.pop("first_glance_contrast")
        missing_category = dict(self.plan,
                                changes=["[功能] 功能退出", "[结构/维护] 窗口封堵", "只有普通描述"])
        for invalid, message in ((missing_field, "缺少 first_glance_contrast"),
                                 (missing_category, "changes 必须覆盖")):
            self.data["pairs"][0]["plan"] = invalid
            with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                self.work.attempt(self.folder, self.data, 1, "now", "now")
        self.data["pairs"][0]["plan"] = self.plan

    def test_now_review_requires_evidence_and_rejects_false_pass(self):
        self.accept("old")
        attempt = self.work.attempt(self.folder, self.data, 1, "now", "now")
        for key in ("first_glance_evidence", "long_term_neglect_evidence"):
            review = self.review("now")
            review.pop(key)
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, f"需填写 {key}"):
                self.work.record(self.folder, self.data, attempt["id"], self.source, review)

        review = self.review("now")
        review["visible_changes"] = ["[功能] 候车设施拆除", "[结构/维护] 窗口封堵", "只有普通描述"]
        with self.assertRaisesRegex(ValueError, "visible_changes 必须覆盖"):
            self.work.record(self.folder, self.data, attempt["id"], self.source, review)

        review = self.review("now")
        review["checks"]["first_glance_contrast"] = False
        with self.assertRaisesRegex(ValueError, "存在未通过项"):
            self.work.record(self.folder, self.data, attempt["id"], self.source, review)

        accepted = self.work.record(self.folder, self.data, attempt["id"], self.source, self.review("now"))
        self.assertEqual(accepted["status"], "accepted")
        self.assertIsNotNone(self.data["pairs"][0]["now"])

    def test_requests_are_parallel_across_groups_but_serial_within_group(self):
        with self.assertRaisesRegex(ValueError, "旧照"):
            self.work.attempt(self.folder, self.data, 1, "now", "now")
        self.work.attempt(self.folder, self.data, 1, "old", "old")
        self.work.plan(self.folder, self.data, 2, self.plan)
        self.work.attempt(self.folder, self.data, 2, "old", "old2")
        with self.assertRaisesRegex(ValueError, "本组"):
            self.work.attempt(self.folder, self.data, 1, "old", "old-again")

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
                "caption": "再也等不到", "first_glance_contrast": "缩小对照仍能看出功能退出",
                "long_term_neglect": "多年封闭后的渗水、锈蚀和植物侵入",
                "anchors": ["廊柱", "台阶"],
                "changes": ["[功能] 候车设施拆除", "[结构/维护] 窗口封堵并渗水",
                            "[环境/设施/用途] 排水沟淤积并长草"]}
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


class DelegationTests(unittest.TestCase):
    def setUp(self):
        self.root = ROOT / "outputs" / "codex-time-echo" / ("test_delegate_" + uuid.uuid4().hex)
        self.root.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, self.root)
        config = dict(DEFAULTS, counts=[6], max_image_calls=36)
        (self.root / "codex-video.json").write_text(json.dumps(config), encoding="utf-8")
        self.work = Workspace(self.root)
        with patch("codex_video.preflight", return_value=["music.mp3"]):
            result = self.work.execute("begin", owner="coordinator")
        self.run_id = result["run_id"]
        self.parent_token = result["execution"]["token"]
        self.folder, self.data = self.work.load(self.run_id)
        self.source = self.root / "image.png"
        Image.new("RGB", (800, 900), "#557766").save(self.source)
        self.plan = {"title": "候车廊", "old_life": "等车回家", "loss": "停运后不再候车",
                     "change_route": "改为库房", "caption": "再也等不到那辆车",
                     "first_glance_contrast": "缩小对照仍能看出从正常候车变为长期失管",
                     "long_term_neglect": "多年封闭造成渗水、锈蚀和植物侵入",
                     "anchors": ["廊柱", "台阶"],
                     "changes": ["[功能] 候车设施拆除后不再使用",
                                 "[结构/维护] 窗口封堵并留下长期渗水痕迹",
                                 "[环境/设施/用途] 排水沟淤积且植物侵入"]}
        self.plan_file = self.root / "plan.json"
        self.plan_file.write_text(json.dumps(self.plan, ensure_ascii=False), encoding="utf-8")
        self.prompt_file = self.root / "prompt.txt"
        self.prompt_file.write_text("完整测试提示词", encoding="utf-8")

    def parent(self, action, **kwargs):
        return self.work.execute(action, owner="coordinator", token=self.parent_token,
                                 run_id=self.run_id, **kwargs)

    def review(self, kind):
        result = {"passed": True, "checks": {k: True for k in CHECKS[kind]},
                  "observations": "实际查看后记录测试证据"}
        if kind == "now":
            result.update(visible_changes=["[功能] 拆牌留孔", "[结构/维护] 窗口封堵",
                                          "[环境/设施/用途] 墙面翻修"],
                          loss_evidence="候车活动消失",
                          first_glance_evidence="缩小对照仍能看出候车功能退出",
                          long_term_neglect_evidence="封闭窗口、渗水和沟渠淤积共同证明长期失管")
        return result

    def delegate_all(self):
        for index in range(1, 7):
            self.parent("plan", pair=index, file=self.plan_file)
        grants = {}
        for index in range(1, 7):
            grants[index] = self.parent("delegate", pair=index, worker=f"worker-{index}",
                                        agent=f"/root/worker_{index}")
        return grants

    def worker(self, index, grants, action="attempt", **kwargs):
        grant = grants[index]
        return self.work.execute(action, owner=f"worker-{index}", token=grant["token"],
                                 run_id=self.run_id, pair=kwargs.pop("pair", index),
                                 agent=f"/root/worker_{index}", **kwargs)

    def test_six_groups_can_reserve_pending_attempts_with_unique_ids(self):
        grants = self.delegate_all()
        attempts = [self.worker(index, grants, file=self.prompt_file, kind="old")
                    for index in range(1, 7)]
        self.assertEqual([attempt["id"] for attempt in attempts], list(range(1, 7)))
        folder, data = self.work.load(self.run_id)
        self.assertEqual(data["image_calls"], 6)
        self.assertEqual(sum(a["status"] == "pending" for p in data["pairs"] for a in p["attempts"]), 6)
        self.assertEqual(sorted(a["id"] for p in data["pairs"] for a in p["attempts"]), list(range(1, 7)))
        with self.assertRaisesRegex(ValueError, "本组"):
            self.worker(1, grants, file=self.prompt_file, kind="old")

    def test_delegate_mode_requires_every_incomplete_group_before_first_attempt(self):
        for index in range(1, 7):
            self.parent("plan", pair=index, file=self.plan_file)
        self.parent("delegate", pair=1, worker="worker-1", agent="/root/worker_1")
        grant = self.parent("delegate", pair=1, worker="worker-1", agent="/root/worker_1")
        with self.assertRaisesRegex(ValueError, "缺少第"):
            self.worker(1, {1: grant}, file=self.prompt_file, kind="old")

    def test_now_still_depends_on_this_groups_accepted_old(self):
        grants = self.delegate_all()
        old = self.worker(1, grants, file=self.prompt_file, kind="old")
        with self.assertRaisesRegex(ValueError, "旧照"):
            self.worker(1, grants, file=self.prompt_file, kind="now")
        self.worker(1, grants, action="fail", attempt=old["id"], reason="测试中断")
        with self.assertRaisesRegex(ValueError, "旧照"):
            self.worker(1, grants, file=self.prompt_file, kind="now")
        old = self.worker(1, grants, file=self.prompt_file, kind="old")
        self.worker(1, grants, action="record", attempt=old["id"], image=self.source,
                    file=self._review_file("old"))
        now = self.worker(1, grants, file=self.prompt_file, kind="now")
        self.assertEqual(now["kind"], "now")

    def _review_file(self, kind):
        path = self.root / f"review-{kind}.json"
        path.write_text(json.dumps(self.review(kind), ensure_ascii=False), encoding="utf-8")
        return path

    def test_worker_is_limited_to_its_group_and_coordinator_keeps_global_rights(self):
        grants = self.delegate_all()
        with self.assertRaises(ExecutionBusy):
            self.worker(1, grants, pair=2, file=self.prompt_file, kind="old")
        with self.assertRaises(ExecutionBusy):
            self.worker(1, grants, action="render")
        with self.assertRaises(ExecutionBusy):
            self.worker(1, grants, action="release", reason="越权释放")
        second = self.parent("delegate", pair=2, worker="worker-2", agent="/root/worker_2")
        self.assertEqual(second["pair"], 2)

    def test_reassigning_a_live_worker_requires_fresh_stop_proof(self):
        grants = self.delegate_all()
        with self.assertRaisesRegex(ValueError, "多个组"):
            self.parent("delegate", pair=2, worker="worker-1", agent="/root/worker_1")
        with self.assertRaises(ExecutionBusy):
            self.parent("delegate", pair=1, worker="worker-replacement", agent="/root/replacement")
        proof = {"thread_id": "worker-1", "observed_status": "interrupted",
                 "source": "read_thread", "checked_at": datetime.now(timezone.utc).isoformat(),
                 "evidence": "已确认原子智能体停止且无法继续"}
        path = self.root / "worker-stop.json"
        for changes in ({"thread_id": "worker-2"},
                        {"checked_at": (datetime.now(timezone.utc) - timedelta(seconds=61)).isoformat()}):
            path.write_text(json.dumps({**proof, **changes}), encoding="utf-8")
            with self.subTest(changes=changes), self.assertRaises(ExecutionBusy):
                self.parent("delegate", pair=1, worker="worker-replacement",
                            agent="/root/replacement", file=path)
        path.write_text(json.dumps(proof), encoding="utf-8")
        replacement = self.parent("delegate", pair=1, worker="worker-replacement",
                                  agent="/root/replacement", file=path)
        self.assertNotEqual(replacement["token"], grants[1]["token"])
        with self.assertRaises(ExecutionBusy):
            self.worker(1, grants, file=self.prompt_file, kind="old")

    def test_global_image_budget_is_shared_by_all_workers(self):
        grants = self.delegate_all()
        _, data = self.work.load(self.run_id)
        data["config"]["max_image_calls"] = 1
        self.work.save(self.folder, data)
        self.worker(1, grants, file=self.prompt_file, kind="old")
        with self.assertRaisesRegex(ValueError, "上限"):
            self.worker(2, grants, file=self.prompt_file, kind="old")

    def _takeover_proof(self, workers=None, owner="coordinator", token=None, revision=None):
        current = self.work.execution()
        proof = {"owner_thread_id": owner, "token": token or current["token"],
                 "revision": revision if revision is not None else current["revision"],
                 "observed_status": "interrupted", "source": "wait_threads",
                 "checked_at": datetime.now(timezone.utc).isoformat(),
                 "evidence": "测试状态工具报告原任务已中断"}
        if workers is not None:
            proof["workers"] = workers
        path = self.root / "takeover.json"
        path.write_text(json.dumps(proof), encoding="utf-8")
        return path

    def _worker_proofs(self, status="interrupted"):
        now = datetime.now(timezone.utc).isoformat()
        return {f"worker-{index}": {"thread_id": f"worker-{index}", "observed_status": status,
                                   "source": "list_agents", "checked_at": now,
                                   "evidence": "已确认该子任务停止"} for index in range(1, 7)}

    def test_takeover_requires_fresh_proof_for_each_worker_and_invalidates_old_tokens(self):
        grants = self.delegate_all()
        with self.assertRaises(ExecutionBusy):
            self.work.execute("takeover", owner="thread-b", run_id=self.run_id,
                              file=self._takeover_proof())
        with self.assertRaises(ExecutionBusy):
            self.work.execute("takeover", owner="thread-b", run_id=self.run_id,
                              file=self._takeover_proof(self._worker_proofs(status="running")))
        result = self.work.execute("takeover", owner="thread-b", run_id=self.run_id,
                                   file=self._takeover_proof(self._worker_proofs()))
        new_token = result["execution"]["token"]
        self.assertNotEqual(new_token, self.parent_token)
        with self.assertRaises(ExecutionBusy):
            self.worker(1, grants, file=self.prompt_file, kind="old")
        self.parent_token = new_token
        reassigned = self.work.execute("delegate", owner="thread-b", token=new_token,
                                       run_id=self.run_id, pair=1, worker="worker-1",
                                       agent="/root/worker_1")
        self.assertNotEqual(reassigned["token"], grants[1]["token"])

    def test_release_refuses_any_group_pending(self):
        grants = self.delegate_all()
        self.worker(1, grants, file=self.prompt_file, kind="old")
        with self.assertRaisesRegex(ValueError, "未登记"):
            self.parent("release", reason="仍有并行请求")

    def test_six_processes_racing_same_batch_do_not_lose_image_call_count(self):
        grants = self.delegate_all()
        script = '''
import json, sys, time
from codex_video import Workspace, ExecutionBusy
root, run_id, pair, owner, token, agent, prompt = sys.argv[1:]
for _ in range(200):
    try:
        result = Workspace(root).execute("attempt", owner=owner, token=token,
            run_id=run_id, pair=int(pair), kind="old", file=prompt, agent=agent)
        print(json.dumps({"status": "ok", "id": result["id"]}))
        break
    except ExecutionBusy:
        time.sleep(.01)
else:
    print(json.dumps({"status": "busy"}))
'''
        processes = []
        try:
            for index in range(1, 7):
                grant = grants[index]
                processes.append(subprocess.Popen(
                    [sys.executable, "-c", script, str(self.root), self.run_id, str(index),
                     f"worker-{index}", grant["token"], f"/root/worker_{index}", str(self.prompt_file)],
                    cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)))
            results = []
            for process in processes:
                stdout, stderr = process.communicate(timeout=30)
                self.assertEqual(process.returncode, 0, stderr.decode(errors="replace"))
                results.append(json.loads(stdout))
            self.assertTrue(all(result["status"] == "ok" for result in results), results)
            _, data = self.work.load(self.run_id)
            self.assertEqual(data["image_calls"], 6)
            self.assertEqual(sorted(result["id"] for result in results), list(range(1, 7)))
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
                    process.communicate()


if __name__ == "__main__":
    unittest.main()
