import json
import shutil
import subprocess
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import publish_video as publisher


class ShadowDomTests(unittest.TestCase):
    def test_visible_main_document_takes_precedence_over_empty_iframe(self):
        page = Mock()
        page.locator.return_value.count.return_value = 1
        page.frames = [SimpleNamespace(url=publisher.POST_URL),
                       SimpleNamespace(url=publisher.POST_URL.replace('/platform/', '/micro/content/'))]
        channels = publisher.ChannelsPage(page)
        self.assertIs(channels._content_frame(), page)

    def test_radio_check_uses_shadow_piercing_locator(self):
        page = Mock()
        page.locator.return_value.count.return_value = 1
        page.locator.return_value.evaluate_all.return_value = True
        channels = publisher.ChannelsPage(page)
        self.assertTrue(channels._checked_choice('定时发表', '不定时'))
        page.locator.assert_called_with('.form-item')
        page.evaluate.assert_not_called()


class FakeState:
    def __init__(self, folder, progress):
        self.folder = Path(folder)
        self.run_id = progress["run_id"]
        self._progress = progress
        self.events = []
        self.ownership_checks = 0

    def verify_ownership(self):
        self.ownership_checks += 1

    def progress(self):
        return self._progress

    def step(self, status, evidence, **extra):
        event = {"status": status, "evidence": evidence, **extra}
        self.events.append(event)
        self._progress = {**self._progress, "status": status}
        return self._progress


class FakeChannels:
    url = publisher.POST_URL

    def __init__(self, *, login=False, receipt="发布成功"):
        self.login = login
        self.receipt = receipt
        self.calls = []

    def is_login(self):
        return self.login

    def probe(self):
        return {"status": "probe", "url": self.url}

    def wait_for_login(self, timeout, refresh, verify_ownership):
        self.calls.append(("wait_for_login", timeout))
        verify_ownership()
        self.login = False

    def open_post_page(self):
        self.calls.append("open_post_page")

    def upload(self, video):
        self.calls.append(("upload", Path(video)))

    def fill_form(self, info):
        self.calls.append(("fill_form", info["short_title"]))

    def wait_until_ready(self, timeout):
        self.calls.append(("wait_until_ready", timeout))

    def account_name(self):
        return "测试视频号"

    def form_checks(self, info):
        return {key: True for key in publisher.PUBLISH_CHECKS}

    def submit(self):
        self.calls.append("submit")

    def wait_for_result(self, timeout):
        self.calls.append(("wait_for_result", timeout))
        return self.receipt


class LoginWaitTests(unittest.TestCase):
    def setUp(self):
        self.now = 0
        self.page = Mock(url="https://channels.weixin.qq.com/login.html")
        self.page.wait_for_timeout.side_effect = self.advance
        self.channels = publisher.ChannelsPage(self.page)
        self.reload_times = []
        self.callback_times = []
        self.page.reload.side_effect = lambda **kwargs: self.reload_times.append(self.now)
        self.callback = Mock(side_effect=lambda: self.callback_times.append(self.now))
        self.verify = Mock()
        clock = patch.object(publisher.time, "monotonic", side_effect=lambda: self.now)
        clock.start()
        self.addCleanup(clock.stop)

    def advance(self, milliseconds):
        self.now += milliseconds / 1000

    def assert_timeout(self, timeout):
        with self.assertRaises(publisher.PublishError) as error:
            self.channels.wait_for_login(timeout, self.callback, self.verify)
        self.assertEqual(error.exception.code, "LOGIN_TIMEOUT")

    def test_refresh_every_sixty_seconds_without_two_refresh_cap(self):
        self.assert_timeout(241)
        self.assertEqual(self.reload_times, [60, 120, 180, 240])
        self.assertEqual(self.callback_times, self.reload_times)
        self.assertEqual(self.now, 241)

    def test_already_logged_in_does_not_reload(self):
        self.page.url = publisher.POST_URL
        self.channels.wait_for_login(900, self.callback, self.verify)
        self.page.reload.assert_not_called()
        self.callback.assert_not_called()
        self.page.wait_for_timeout.assert_not_called()

    def test_login_at_refresh_boundary_prevents_reload(self):
        def advance(milliseconds):
            self.advance(milliseconds)
            if self.now >= 60:
                self.page.url = publisher.POST_URL
        self.page.wait_for_timeout.side_effect = advance
        self.channels.wait_for_login(900, self.callback, self.verify)
        self.assertEqual(self.now, 60)
        self.page.reload.assert_not_called()
        self.callback.assert_not_called()

    def test_reload_redirect_to_logged_in_page_does_not_request_qr(self):
        def reload(**kwargs):
            self.page.url = publisher.POST_URL
        self.page.reload.side_effect = reload
        self.channels.wait_for_login(900, self.callback, self.verify)
        self.page.reload.assert_called_once()
        self.callback.assert_not_called()
        self.assertEqual(self.now, 60)

    def test_timeout_at_refresh_boundary_does_not_reload(self):
        for timeout in (0, 59, 60):
            with self.subTest(timeout=timeout):
                self.now = 0
                self.assert_timeout(timeout)
                self.assertEqual(self.now, timeout)
        self.page.reload.assert_not_called()
        self.callback.assert_not_called()

    def test_callback_time_counts_towards_total_timeout(self):
        self.callback.side_effect = lambda: self.advance(5000)
        self.assert_timeout(123)
        self.assertEqual(self.reload_times, [60, 120])
        self.assertEqual(self.now, 125)
        self.assertEqual(self.callback.call_count, 2)

    def test_lost_ownership_prevents_refresh(self):
        def verify():
            if self.now >= 60:
                raise publisher.PublishError("OWNERSHIP_LOST", "执行权丢失")
        self.verify.side_effect = verify
        with self.assertRaisesRegex(publisher.PublishError, "执行权丢失"):
            self.channels.wait_for_login(900, self.callback, self.verify)
        self.page.reload.assert_not_called()
        self.callback.assert_not_called()


class PublishVideoTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.folder = Path(self.temporary.name)
        self.video = self.folder / "video.mp4"
        self.video.write_bytes(b"video")
        self.progress = {
            "run_id": "run_1",
            "status": "prepared",
            "events": [],
            "info": {
                "run_id": "run_1",
                "video": str(self.video),
                "short_title": "旧日难回",
                "description": "AI 创作场景",
                "hashtags": ["旧时光"],
                "settings": {
                    "location": "不显示位置", "collection": "旧时光", "link": None,
                    "activity": "不参与活动", "schedule": "不定时",
                    "original_requested": True, "ai_label": "内容为虚构剧情，仅供娱乐",
                },
            },
        }

    def args(self, **changes):
        values = {"run": "run_1", "token": "token_1", "owner": "owner_1",
                  "probe": False, "dry_run": False, "login_timeout": 900,
                  "upload_timeout": 1200, "result_timeout": 120}
        values.update(changes)
        return SimpleNamespace(**values)

    def test_probe_never_changes_state_or_page(self):
        state = FakeState(self.folder, self.progress)
        channels = FakeChannels()
        result = publisher.publish_on_page(self.args(probe=True), state, self.progress, channels)
        self.assertEqual(result["status"], "probe")
        self.assertEqual(state.events, [])
        self.assertEqual(channels.calls, [])

    def test_dry_run_uploads_and_fills_without_submit(self):
        state = FakeState(self.folder, self.progress)
        channels = FakeChannels()
        result = publisher.publish_on_page(self.args(dry_run=True), state, self.progress, channels)
        self.assertEqual(result["status"], "READY")
        self.assertNotIn("submit", channels.calls)
        self.assertEqual([event["status"] for event in state.events], ["uploading", "ready"])

    def test_submit_intent_is_saved_before_single_click(self):
        state = FakeState(self.folder, self.progress)
        channels = FakeChannels()
        result = publisher.publish_on_page(self.args(), state, self.progress, channels)
        self.assertEqual(result["status"], "PUBLISHED")
        self.assertEqual(channels.calls.count("submit"), 1)
        self.assertEqual([event["status"] for event in state.events],
                         ["uploading", "ready", "submitting", "published"])

    def test_uncertain_result_never_clicks_again_on_resume(self):
        progress = {**self.progress, "status": "uncertain"}
        state = FakeState(self.folder, progress)
        channels = FakeChannels()
        result = publisher.publish_on_page(self.args(), state, progress, channels)
        self.assertEqual(result["status"], "UNCERTAIN_SUBMISSION")
        self.assertEqual(channels.calls, [])
        self.assertEqual(state.events, [])

    def test_submitting_state_never_clicks_again_on_resume(self):
        progress = {**self.progress, "status": "submitting"}
        state = FakeState(self.folder, progress)
        channels = FakeChannels()
        result = publisher.publish_on_page(self.args(), state, progress, channels)
        self.assertEqual(result["status"], "UNCERTAIN_SUBMISSION")
        self.assertEqual(channels.calls, [])

    def test_failed_submitting_persistence_prevents_click(self):
        state = FakeState(self.folder, self.progress)
        original_step = state.step
        def step(status, evidence, **extra):
            if status == "submitting":
                raise OSError("disk full")
            return original_step(status, evidence, **extra)
        state.step = step
        channels = FakeChannels()
        with self.assertRaisesRegex(OSError, "disk full"):
            publisher.publish_on_page(self.args(), state, self.progress, channels)
        self.assertNotIn("submit", channels.calls)

    def test_login_failure_is_recorded_as_blocked(self):
        state = FakeState(self.folder, self.progress)
        channels = FakeChannels(login=True)
        with patch.object(publisher, "notify_qr",
                          side_effect=publisher.PublishError("QR_NOT_FOUND", "没有二维码")):
            with self.assertRaisesRegex(publisher.PublishError, "没有二维码"):
                publisher.publish_on_page(self.args(), state, self.progress, channels)
        self.assertEqual(state.events[-1]["status"], "blocked")
        self.assertEqual(state.events[-1]["reason"], "QR_NOT_FOUND")

    def test_lost_ownership_stops_before_upload_without_block_write(self):
        state = FakeState(self.folder, {**self.progress, "status": "uploading"})
        def verify():
            raise publisher.PublishError("OWNERSHIP_LOST", "执行权丢失")
        state.verify_ownership = verify
        channels = FakeChannels()
        with self.assertRaisesRegex(publisher.PublishError, "执行权丢失"):
            publisher.publish_on_page(self.args(), state, state.progress(), channels)
        self.assertEqual(channels.calls, [])
        self.assertEqual(state.events, [])

    def test_submit_exception_becomes_uncertain(self):
        state = FakeState(self.folder, self.progress)
        channels = FakeChannels()
        channels.submit = Mock(side_effect=RuntimeError("browser stopped"))
        with self.assertRaisesRegex(publisher.PublishError, "不会自动再次发布"):
            publisher.publish_on_page(self.args(), state, self.progress, channels)
        self.assertEqual([event["status"] for event in state.events][-2:], ["submitting", "uncertain"])
        channels.submit.assert_called_once_with()

    def test_pre_submit_page_error_is_blocked(self):
        state = FakeState(self.folder, self.progress)
        channels = FakeChannels()
        channels.fill_form = Mock(side_effect=publisher.PublishError("FORM_CONTROL_NOT_FOUND", "找不到控件"))
        with self.assertRaisesRegex(publisher.PublishError, "找不到控件"):
            publisher.publish_on_page(self.args(), state, self.progress, channels)
        self.assertEqual(state.events[-1]["status"], "blocked")
        self.assertNotIn("submit", channels.calls)

    def test_unexpected_pre_submit_browser_error_is_blocked(self):
        state = FakeState(self.folder, self.progress)
        channels = FakeChannels()
        channels.open_post_page = Mock(side_effect=RuntimeError("frame detached"))
        with self.assertRaisesRegex(publisher.PublishError, "批次进度已保留"):
            publisher.publish_on_page(self.args(), state, self.progress, channels)
        self.assertEqual(state.events[-1]["status"], "blocked")
        self.assertEqual(state.events[-1]["reason"], "BROWSER_ERROR")
        self.assertNotIn("submit", channels.calls)

    def test_login_sends_qr_then_continues(self):
        state = FakeState(self.folder, self.progress)
        channels = FakeChannels(login=True)
        qr = self.folder / "login-qr-1.png"
        qr.write_bytes(b"qr")
        text = self.folder / "telegram-login-1.txt"
        text.write_text("scan", encoding="utf-8")
        with patch.object(publisher, "notify_qr", return_value=(qr, self.progress)) as notify:
            result = publisher.publish_on_page(self.args(dry_run=True), state, self.progress, channels)
        self.assertEqual(result["status"], "READY")
        notify.assert_called_once()
        self.assertIn(("wait_for_login", 900), channels.calls)
        self.assertFalse(qr.exists())
        self.assertFalse(text.exists())

    def test_login_exit_cleans_files_on_timeout_notification_error_and_interrupt(self):
        for failure in (publisher.PublishError("LOGIN_TIMEOUT", "登录超时"),
                        publisher.PublishError("TELEGRAM_FAILED", "通知失败"),
                        KeyboardInterrupt()):
            with self.subTest(failure=type(failure).__name__):
                state = FakeState(self.folder, self.progress)
                channels = FakeChannels(login=True)
                files = [self.folder / name for name in (
                    "login-qr-1.png", "telegram-login-1.txt", ".login-qr-current.png")]
                def notify(*args):
                    for path in files:
                        path.write_bytes(b"partial")
                    if isinstance(failure, publisher.PublishError) and failure.code == "LOGIN_TIMEOUT":
                        return files[0], self.progress
                    raise failure
                channels.wait_for_login = Mock(side_effect=failure)
                with patch.object(publisher, "notify_qr", side_effect=notify):
                    with self.assertRaises(type(failure)):
                        publisher.publish_on_page(self.args(), state, self.progress, channels)
                self.assertTrue(all(not path.exists() for path in files))
                self.assertTrue(self.video.exists())

    def test_refresh_cleans_old_qr_and_text_before_capture(self):
        state = FakeState(self.folder, self.progress)
        old_files = [self.folder / name for name in (
            "login-qr-1.png", "telegram-login-1.txt", ".login-qr-current.png")]
        for path in old_files:
            path.write_bytes(b"old")
        receipt_file = self.folder / "notification-receipts.json"
        receipt_file.write_text('{"deliveries": {}}', encoding="utf-8")
        unrelated = self.folder / "telegram-complete.txt"
        unrelated.write_text("keep", encoding="utf-8")
        channels = Mock()
        def capture(path):
            self.assertTrue(all(not old.exists() for old in old_files))
            path.write_bytes(b"new-qr")
            return "oauth-qr-image"
        channels.capture_qr.side_effect = capture
        completed = Mock(returncode=0, stdout=json.dumps({"status": "sent"}), stderr="")
        with patch.object(publisher.subprocess, "run", return_value=completed):
            image, _ = publisher.notify_qr(state, channels, self.progress)
        self.assertEqual(image.read_bytes(), b"new-qr")
        self.assertTrue(receipt_file.exists())
        self.assertEqual(unrelated.read_text(encoding="utf-8"), "keep")
        self.assertFalse((self.folder / ".login-qr-current.png").exists())

    def test_result_redirects_are_not_publication_evidence(self):
        page = Mock()
        page.frames = []
        page.url = "https://channels.weixin.qq.com/login.html"
        empty = Mock()
        empty.count.return_value = 0
        page.get_by_text.return_value = empty
        channels = publisher.ChannelsPage(page)
        self.assertIsNone(channels.wait_for_result(timeout=0.01))

        page.url = "https://channels.weixin.qq.com/platform/content/video"
        page.wait_for_timeout.side_effect = lambda _: None
        with patch.object(publisher.time, "monotonic", side_effect=[0, 0, 1]):
            self.assertIsNone(channels.wait_for_result(timeout=0.5))

    def test_exact_success_text_is_required_for_receipt(self):
        page = Mock()
        page.frames = []
        page.url = publisher.POST_URL
        visible = Mock()
        visible.is_visible.return_value = True
        exact_locator = Mock()
        exact_locator.count.return_value = 1
        exact_locator.nth.return_value = visible
        missing = Mock()
        missing.count.return_value = 0
        page.get_by_text.side_effect = lambda text, exact: exact_locator if text == "发表成功" else missing
        channels = publisher.ChannelsPage(page)
        receipt = channels.wait_for_result(timeout=1)
        self.assertIn("发表成功", receipt)
        page.get_by_text.assert_any_call("发表成功", exact=True)

    def test_description_text_includes_exact_hashtags(self):
        expected = "AI 创作场景\n#旧时光"
        self.assertEqual(publisher.ChannelsPage.description_text(self.progress["info"]), expected)

    def upload_fixture(self, selected):
        channels = publisher.ChannelsPage(Mock())
        inputs = Mock()
        inputs.count.return_value = 1
        original = inputs.element_handle.return_value
        record = original.evaluate_handle.return_value
        record.evaluate.side_effect = lambda script: selected if script == 'record => record.files' else None
        channels._content_frame = Mock()
        channels._content_frame.return_value.locator.return_value = inputs
        return channels, inputs, original, record

    def test_upload_captures_before_setting_original_and_disposes_handles(self):
        channels, inputs, original, record = self.upload_fixture([{"name": "video.mp4", "size": 5}])
        calls = Mock()
        calls.attach_mock(original.evaluate_handle, 'capture')
        calls.attach_mock(original.set_input_files, 'set_files')
        calls.attach_mock(record.evaluate, 'read')
        channels.upload(self.video)
        self.assertEqual([call[0] for call in calls.mock_calls],
                         ['capture', 'set_files', 'read', 'read', 'capture().dispose'])
        original.set_input_files.assert_called_once_with(str(self.video))
        inputs.set_input_files.assert_not_called()
        inputs.evaluate.assert_not_called()
        self.assertEqual(channels.uploaded_video, ('video.mp4', 5))
        record.dispose.assert_called_once()
        original.dispose.assert_called_once()

    def test_upload_rejects_missing_wrong_or_multiple_files_and_clears_old_evidence(self):
        for selected in (None, [], [{"name": "other.mp4", "size": 5}],
                         [{"name": "video.mp4", "size": 6}],
                         [{"name": "video.mp4", "size": 5}] * 2):
            with self.subTest(selected=selected):
                channels, inputs, original, record = self.upload_fixture(selected)
                channels.uploaded_video = ('video.mp4', 5)
                with self.assertRaisesRegex(publisher.PublishError, "未捕获"):
                    channels.upload(self.video)
                self.assertIsNone(channels.uploaded_video)
                record.dispose.assert_called_once()
                original.dispose.assert_called_once()

    def test_set_files_failure_clears_evidence_and_removes_listener(self):
        channels, inputs, original, record = self.upload_fixture(None)
        channels.uploaded_video = ('video.mp4', 5)
        original.set_input_files.side_effect = RuntimeError('detached')
        with self.assertRaisesRegex(RuntimeError, 'detached'):
            channels.upload(self.video)
        self.assertIsNone(channels.uploaded_video)
        record.evaluate.assert_called_once_with('record => record.stop()')
        record.dispose.assert_called_once()
        original.dispose.assert_called_once()

    @unittest.skipUnless(shutil.which('node'), 'Node is required for the local event fixture')
    def test_actual_capture_script_survives_clear_replace_and_shadow_change(self):
        channels, inputs, original, record = self.upload_fixture([{"name": "video.mp4", "size": 5}])
        channels.upload(self.video)
        script = original.evaluate_handle.call_args.args[0]
        # Execute production JS without a browser. This models capture propagation,
        # including a non-composed shadow event, but is not a full DOM implementation.
        fixture = r'''
const assert = require('node:assert/strict');
const install = eval('(' + JSON.parse(require('node:fs').readFileSync(0, 'utf8')) + ')');
class Scope {
    listeners = [];
    addEventListener(type, listener, capture) {
        assert.equal(type, 'change'); assert.equal(capture, true);
        this.listeners.push(listener);
    }
    removeEventListener(type, listener, capture) {
        this.listeners = this.listeners.filter(item => item !== listener);
    }
    dispatch(event) { for (const listener of [...this.listeners]) listener(event); }
}
for (const composed of [true, false]) {
    for (const replace of [true, false]) {
        const view = new Scope(), root = new Scope();
        const input = {ownerDocument: {defaultView: view}, getRootNode: () => root,
                       files: [{name: 'old.mp4', size: 3}]};
        const record = install(input);
        assert.equal(record.files, null); // retained old files are not evidence
        const other = {files: [{name: 'video.mp4', size: 5}]};
        const unrelated = {composedPath: () => [other, root, view]};
        view.dispatch(unrelated); root.dispatch(unrelated);
        assert.equal(record.files, null);
        input.files = [{name: 'video.mp4', size: 5}];
        const event = {composedPath: () => [input, root, ...(composed ? [view] : [])]};
        if (composed) view.dispatch(event);
        root.dispatch(event);
        // Site's target listener consumes the file and clears/replaces the input.
        input.files = [];
        const current = replace ? other : input;
        assert.notEqual(current.files, record.files);
        root.dispatch(event); // a later clearing event must not erase evidence
        assert.deepEqual(record.files, [{name: 'video.mp4', size: 5}]);
        record.stop();
        assert.equal(view.listeners.length + root.listeners.length, 0);
    }
}
'''
        completed = subprocess.run([shutil.which('node'), '-e', fixture], input=json.dumps(script),
                                   text=True, capture_output=True, timeout=15)
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_readiness_requires_current_upload_and_no_processing(self):
        channels = publisher.ChannelsPage(Mock())
        button = Mock()
        button.count.return_value = 1
        button.get_attribute.return_value = ''
        channels._publish_button = Mock(return_value=button)
        channels._visible_text = Mock(return_value=False)
        self.assertFalse(channels._upload_ready())
        with self.assertRaisesRegex(publisher.PublishError, '缺少本轮'):
            channels.wait_until_ready()
        channels.uploaded_video = ('video.mp4', 5)
        self.assertTrue(channels._upload_ready())
        for busy in ('上传中', '处理中', '转码中'):
            channels._visible_text.side_effect = lambda text: text == busy
            self.assertFalse(channels._upload_ready())
        channels._visible_text.side_effect = None
        button.count.return_value = 2
        self.assertFalse(channels._upload_ready())

    def test_form_checks_rejects_processing_even_with_enabled_button(self):
        channels = publisher.ChannelsPage(Mock())
        channels.uploaded_video = ('video.mp4', 5)
        channels._visible_text = Mock(return_value=True)
        channels._publish_button = Mock()
        channels._publish_button.return_value.count.return_value = 1
        channels._publish_button.return_value.get_attribute.return_value = ''
        channels._field_value = Mock(side_effect=[channels.description_text(self.progress['info']),
                                                self.progress['info']['short_title']])
        channels._row_has_value = Mock(return_value=True)
        channels._checked_choice = Mock(return_value=True)
        channels._original_checked = Mock(return_value=True)
        with self.assertRaisesRegex(publisher.PublishError, 'upload_complete'):
            channels.form_checks(self.progress['info'])

    def test_css_disabled_publish_button_is_not_ready(self):
        button = Mock()
        button.get_attribute.return_value = "weui-desktop-btn weui-desktop-btn_disabled"
        button.is_visible.return_value = True
        button.is_enabled.return_value = True
        self.assertFalse(publisher.ChannelsPage._button_ready(button))

    def test_open_post_page_accepts_real_controls_in_content_frame(self):
        upload = Mock()
        upload.count.return_value = 1
        publish = Mock()
        publish.count.return_value = 1
        frame = Mock()
        frame.url = "https://channels.weixin.qq.com/micro/content/post/create"
        frame.locator.return_value = upload
        frame.get_by_role.return_value = publish
        page = Mock()
        page.url = publisher.POST_URL
        page.frames = [frame]
        publisher.ChannelsPage(page).open_post_page()
        page.goto.assert_not_called()
        frame.get_by_role.assert_called_once_with("button", name="发表", exact=True)

    def test_real_publish_button_name_and_content_frame_are_used(self):
        content = Mock()
        content.url = "https://channels.weixin.qq.com/micro/content/post/create"
        button = Mock()
        content.get_by_role.return_value = button
        page = Mock()
        page.frames = [SimpleNamespace(url=publisher.POST_URL), content]
        channels = publisher.ChannelsPage(page)
        self.assertIs(channels._publish_button(), button)
        content.get_by_role.assert_called_once_with("button", name="发表", exact=True)
        self.assertIs(channels._content_frame(), content)

    def test_duplicate_content_frames_are_rejected(self):
        first = SimpleNamespace(url="https://channels.weixin.qq.com/micro/content/post/create")
        second = SimpleNamespace(url="https://channels.weixin.qq.com/micro/content/post/create?copy=1")
        page = Mock(frames=[first, second])
        channels = publisher.ChannelsPage(page)
        with self.assertRaisesRegex(publisher.PublishError, "唯一"):
            channels._content_frame()

    def test_connect_page_prefers_rendered_publish_tab(self):
        def candidate(url, upload_count=0, publish_count=0):
            upload = Mock()
            upload.count.return_value = upload_count
            publish = Mock()
            publish.count.return_value = publish_count
            frame = Mock()
            frame.url = "https://channels.weixin.qq.com/micro/content/post/create"
            frame.locator.return_value = upload
            frame.get_by_role.return_value = publish
            page = Mock(url=url, frames=[frame])
            return page
        blank = candidate(publisher.POST_URL)
        ready = candidate(publisher.POST_URL, 1, 1)
        login = Mock(url="https://channels.weixin.qq.com/login.html", frames=[])
        browser = Mock(contexts=[Mock(pages=[blank, login, ready])])
        playwright = Mock()
        playwright.chromium.connect_over_cdp.return_value = browser
        selected_browser, selected_page = publisher.connect_page(playwright)
        self.assertIs(selected_browser, browser)
        self.assertIs(selected_page, ready)

    def test_terminal_state_returns_before_edge_connection(self):
        progress = {**self.progress, "status": "uncertain"}
        args = self.args()
        state = Mock()
        state.prepare.return_value = progress
        with patch.object(publisher, "resolve_owner", return_value="owner_1"), \
                patch.object(publisher, "Workspace"), \
                patch.object(publisher, "PublicationState", return_value=state), \
                patch.object(publisher, "ensure_edge") as edge:
            result = publisher.run_publication(args)
        self.assertEqual(result["status"], "UNCERTAIN_SUBMISSION")
        edge.assert_not_called()

    def test_notify_qr_reuses_event_key_after_interruption(self):
        state = FakeState(self.folder, self.progress)
        content = b"same-qr"
        digest = publisher.hashlib.sha256(content).hexdigest()
        progress = {**self.progress, "status": "waiting_login", "events": [{
            "status": "waiting_login", "qr_sequence": 1, "qr_sha256": digest,
            "qr_event": "time-echo:run_1:login-qr:1",
        }]}
        state._progress = progress
        channels = Mock()
        channels.capture_qr.side_effect = lambda path: Path(path).write_bytes(content) or "oauth-qr-image"
        completed = Mock(returncode=0, stdout=json.dumps({"status": "sent", "message_id": 8}), stderr="")
        with patch.object(publisher.subprocess, "run", return_value=completed) as command:
            publisher.notify_qr(state, channels, progress)
        self.assertIn("time-echo:run_1:login-qr:1", command.call_args.args[0])
        self.assertNotIn("time-echo:run_1:login-qr:2", command.call_args.args[0])
        self.assertEqual(state.events[-1]["telegram_receipt"]["message_id"], 8)


if __name__ == "__main__":
    unittest.main()
