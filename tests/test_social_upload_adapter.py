"""Offline integration: real TencentVideo submission + real durable Workspace."""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from PIL import Image
import test_codex_publication as fixtures
import social_upload_adapter as adapter
from social_upload_adapter import (load_tencent, publish, QRCallback, StrictForm,
                                   PersistentTencentSession, account_path)
from publish_video import PublicationState, PublishError


class SocialTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.PublicationTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        f = self.fixture
        f.call("publish-init", f.info)
        self.state = PublicationState(f.work, f.run_id, "thread-a", f.token)
        self.args = SimpleNamespace(run=f.run_id, dry_run=False, login_timeout=1, upload_timeout=1, result_timeout=0)
        self.module = load_tencent()
        self.clicks = 0
        self.success = True
        self.click_error = False
        self.login_result = {"success": True}
        self.reconcile_result = None
        self.upload_override = None
        self.session = None
        test = self
        class Locator:
            async def count(self): return 1
            async def is_visible(self): return test.success
            async def is_enabled(self): return True
            async def click(self, **kwargs):
                test.assertEqual(test.state.progress()["status"], "submitting")
                test.clicks += 1
                if test.click_error: raise TimeoutError("click response lost")
        class Button(Locator):
            async def is_visible(self): return True
        self.page = SimpleNamespace(url="https://channels.weixin.qq.com/platform/post/create",
            get_by_role=lambda *a, **k: Button(), get_by_text=lambda *a, **k: Locator())
        class Form:
            def __init__(self, page): pass
            async def verify(self, info):
                return "本地测试视频号", {k: True for k in fixtures.PUBLISH_CHECKS if k not in ("video", "upload_complete")}
        self.form = Form

    def execute(self, noop=False):
        test = self
        class Session:
            def __init__(self, module, state, args):
                self.exited = False
                self.saved = False
                test.session = self
            async def __aenter__(self): return self
            async def __aexit__(self, *args): self.exited = True
            async def login(self, account_file): return test.login_result
            async def upload(self, uploader):
                if test.upload_override:
                    return await test.upload_override(uploader)
                if noop:
                    return
                await uploader.upload_video_file(test.page, uploader.file_path)
                await uploader.wait_for_upload_complete(test.page)
                await uploader.submit_publish(test.page)
            async def reconcile_submission(self, info): return test.reconcile_result
            async def save_state(self, account_file): self.saved = True
        with patch.object(self.module.TencentVideo, "upload_video_file", AsyncMock()), \
             patch.object(self.module.TencentVideo, "wait_for_upload_complete", AsyncMock()):
            return asyncio.run(publish(self.args, self.state, self.module, self.form, Session))

    def test_success_durable_before_click_and_no_republish(self):
        self.args.result_timeout = 1
        self.assertEqual(self.execute()["status"], "PUBLISHED")
        self.assertEqual(self.clicks, 1)
        self.execute()
        self.assertEqual(self.clicks, 1)

    def test_no_success_receipt_is_uncertain_and_not_retried(self):
        with self.assertRaisesRegex(PublishError, "禁止自动重发"):
            self.execute()
        self.assertEqual(self.state.progress()["status"], "uncertain")
        self.assertEqual(self.execute()["status"], "UNCERTAIN_SUBMISSION")
        self.assertEqual(self.clicks, 1)

    def test_uncertain_submission_is_reconciled_in_same_list_page(self):
        self.reconcile_result = "作品列表匹配本轮文案，状态原创审核中"
        result = self.execute()
        self.assertEqual(result["status"], "PUBLISHED")
        self.assertEqual(self.state.progress()["status"], "published")
        self.assertEqual(self.clicks, 1)
        self.assertTrue(self.session.saved)
        self.assertTrue(self.session.exited)

    def test_click_timeout_never_falls_back_to_second_click(self):
        self.click_error = True
        with self.assertRaises(PublishError): self.execute()
        self.assertEqual(self.clicks, 1)
        self.assertEqual(self.state.progress()["status"], "uncertain")

    def test_dry_run_does_not_click(self):
        self.args.dry_run = True
        self.assertEqual(self.execute()["status"], "READY")
        self.assertEqual(self.clicks, 0)

    def test_zero_exit_without_hooks_is_not_published(self):
        with self.assertRaisesRegex(PublishError, "没有平台成功证据"): self.execute(noop=True)
        self.assertEqual(self.state.progress()["status"], "blocked")

    def test_missing_setting_blocks_before_submit(self):
        class MissingForm:
            def __init__(self, page): pass
            async def verify(self, info): raise PublishError("FORM_CONTROL_NOT_FOUND", "视频标注")
        self.form = MissingForm
        with self.assertRaises(PublishError): self.execute()
        self.assertEqual(self.clicks, 0)
        self.assertEqual(self.state.progress()["status"], "blocked")

    def qr(self, color):
        path = self.state.folder / 'social-account_tencent_login_qrcode_test.png'
        Image.new('RGB', (128, 128), color).save(path)
        return {"image_path": str(path)}

    def test_qr_real_image_document_persistent_dedupe_refresh_cleanup(self):
        receipt = SimpleNamespace(returncode=0, stdout=json.dumps({"status":"sent", "message_id":1}))
        with patch('publish_video.subprocess.run', return_value=receipt) as send:
            QRCallback(self.state)(self.qr('white'))
            QRCallback(self.state)(self.qr('white'))
            self.assertEqual(send.call_count, 1)
            QRCallback(self.state)(self.qr('black'))
            self.assertEqual(send.call_count, 2)
            self.assertIn('document', send.call_args.args[0])
        self.assertFalse(list(self.state.folder.glob('*qr*.png')))
        events = self.state.progress()['events']
        self.assertEqual(len({e['qr_sha256'] for e in events if 'qr_sha256' in e}), 2)

    def test_qr_failed_send_retry_uses_same_event_and_already_sent_receipt(self):
        fail = SimpleNamespace(returncode=1, stderr='{}')
        ok = SimpleNamespace(returncode=0, stdout=json.dumps({"status":"already_sent", "message_id":2}))
        with patch('publish_video.subprocess.run', side_effect=[fail, ok]) as send:
            with self.assertRaises(PublishError): QRCallback(self.state)(self.qr('white'))
            QRCallback(self.state)(self.qr('white'))
            self.assertEqual(send.call_args_list[0].args[0][4], send.call_args_list[1].args[0][4])
        self.assertFalse(list(self.state.folder.glob('*qr*.png')))

    def test_account_reused_across_batches_and_storage_not_accepted_as_qr(self):
        first = account_path(self.state)
        other = SimpleNamespace(work=self.state.work, folder=self.state.folder / 'another-batch')
        self.assertEqual(first, account_path(other))
        first.write_text('{"cookies":[]}', encoding='utf-8')
        with self.assertRaises(PublishError):
            QRCallback(self.state, first.parent)({'image_path': str(first)})
        self.assertTrue(first.exists())

    def test_non_dict_login_result_is_explicit_failure(self):
        self.login_result = False
        with self.assertRaises(PublishError) as raised:
            self.execute()
        self.assertEqual(raised.exception.code, 'LOGIN_FAILED')
        self.assertEqual(self.state.progress()['status'], 'blocked')
        self.assertEqual(self.clicks, 0)

    def test_authorization_callback_error_prevents_click(self):
        uploader = self.module.TencentVideo('test', str(self.fixture.video), [], 0, 'fake.json')
        uploader.before_submit = AsyncMock(side_effect=RuntimeError('disk write failed'))
        with self.assertRaisesRegex(RuntimeError, 'disk write failed'):
            asyncio.run(uploader.submit_publish(self.page))
        self.assertEqual(self.clicks, 0)

    def test_swallowed_upstream_exception_still_becomes_uncertain(self):
        test = self
        async def swallowed(uploader):
            await uploader.upload_video_file(test.page, uploader.file_path)
            await uploader.wait_for_upload_complete(test.page)
            try:
                await uploader.submit_publish(test.page)
            except Exception:
                pass
        self.upload_override = swallowed
        with self.assertRaises(PublishError):
            self.execute()
        self.assertEqual(self.state.progress()['status'], 'uncertain')
        self.assertEqual(self.clicks, 1)

    def test_upstream_requires_hook(self):
        uploader = self.module.TencentVideo('test', str(self.fixture.video), [], 0, 'fake.json')
        with self.assertRaisesRegex(RuntimeError, 'authorization'):
            asyncio.run(uploader.submit_publish(self.page))
        self.assertEqual(self.clicks, 0)

    def test_load_rejects_wrong_patchright_version(self):
        with patch.object(adapter, 'package_version', return_value='0.0.0'):
            with self.assertRaisesRegex(RuntimeError, 'Patchright version mismatch'):
                adapter.load_tencent()

    def test_unique_waits_for_transient_control_and_returns_visible_match(self):
        class DelayedLocator:
            def __init__(self): self.calls = 0
            async def count(self):
                self.calls += 1
                return 0 if self.calls == 1 else 1
            def nth(self, index): return self
            async def is_visible(self): return True
        locator = DelayedLocator()
        page = SimpleNamespace(locator=lambda selector: locator, wait_for_timeout=AsyncMock())
        result = asyncio.run(StrictForm(page).unique('.value', timeout=1000))
        self.assertIs(result, locator)
        page.wait_for_timeout.assert_awaited_once_with(200)

    def test_unique_attached_waits_for_delayed_hidden_input(self):
        class DelayedInput:
            def __init__(self): self.count_checks = 0
            async def count(self):
                self.count_checks += 1
                return 0 if self.count_checks == 1 else 1

        locator = DelayedInput()
        page = SimpleNamespace(wait_for_timeout=AsyncMock())
        result = asyncio.run(StrictForm(page).unique_attached(
            locator, "定时单选框", timeout=1000))
        self.assertIs(result, locator)
        page.wait_for_timeout.assert_awaited_once_with(100)

    def test_wait_button_ready_waits_for_delayed_publish_button(self):
        class DelayedButton:
            def __init__(self): self.enabled_checks = 0
            async def count(self): return 1
            async def is_visible(self): return True
            async def is_enabled(self):
                self.enabled_checks += 1
                return self.enabled_checks >= 2
            async def get_attribute(self, name): return None

        button = DelayedButton()
        page = SimpleNamespace(wait_for_timeout=AsyncMock())
        asyncio.run(StrictForm(page).wait_button_ready(button))
        self.assertEqual(button.enabled_checks, 2)
        page.wait_for_timeout.assert_awaited_once_with(100)

    def test_select_waits_for_delayed_option_after_trigger(self):
        value = "不显示位置"

        class DelayedSelectPage:
            def __init__(self):
                self.rendered = False
                self.selected = False
                self.selection_pending = False
                self.waits = []
                self.display = self.Display(self)
                self.trigger = self.Trigger(self)
                self.options = self.Options(self)

            async def wait_for_timeout(self, milliseconds):
                self.waits.append(milliseconds)
                if self.selection_pending:
                    self.selected = True
                else:
                    self.rendered = True

            def locator(self, selector):
                return {
                    ".display": self.display,
                    ".trigger": self.trigger,
                    ".options": self.options,
                }[selector]

            class Display:
                def __init__(self, page): self.page = page
                async def count(self): return 1
                def nth(self, index): return self
                async def is_visible(self): return True
                async def inner_text(self):
                    return value if self.page.selected else ""

            class Trigger:
                def __init__(self, page): self.page = page
                async def count(self): return 1
                def nth(self, index): return self
                async def is_visible(self): return True
                async def click(self): pass

            class Options:
                def __init__(self, page):
                    self.page = page
                    self.probes = 0
                async def count(self):
                    self.probes += 1
                    return 1 if self.page.rendered else 0
                def nth(self, index): return self
                async def is_visible(self): return self.page.rendered
                async def inner_text(self): return value
                async def click(self): self.page.selection_pending = True

        page = DelayedSelectPage()
        asyncio.run(StrictForm(page).select(".display", ".trigger", ".options", value))
        self.assertTrue(page.selected)
        self.assertIn(200, page.waits)
        self.assertIn(100, page.waits)
        self.assertGreaterEqual(page.options.probes, 2)

    def test_selected_location_and_collection_value_selectors_are_supported(self):
        self.assertIn('.not-display', StrictForm.LOCATION_VALUE)
        self.assertIn('.collection-text', StrictForm.COLLECTION_VALUE)

    def test_css_disabled_button_is_not_ready(self):
        class Button:
            async def count(self): return 1
            async def is_visible(self): return True
            async def is_enabled(self): return True
            async def get_attribute(self, name): return 'weui-desktop-btn weui-desktop-btn_disabled'
        self.assertFalse(asyncio.run(StrictForm.button_ready(Button())))

    def test_persistent_session_exit_detaches_without_closing_edge(self):
        session = PersistentTencentSession(self.module, self.state, self.args)
        session.playwright = SimpleNamespace(stop=AsyncMock())
        session.browser = SimpleNamespace(close=AsyncMock())
        session.context = SimpleNamespace(close=AsyncMock())
        asyncio.run(session.__aexit__(None, None, None))
        session.playwright.stop.assert_awaited_once()
        session.browser.close.assert_not_awaited()
        session.context.close.assert_not_awaited()


if __name__ == '__main__': unittest.main()
