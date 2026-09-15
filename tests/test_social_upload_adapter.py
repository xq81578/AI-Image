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
from social_upload_adapter import load_tencent, publish, QRCallback, StrictForm, account_path
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
        async def main(uploader):
            if noop: return
            await uploader.upload_video_file(test.page, uploader.file_path)
            await uploader.wait_for_upload_complete(test.page)
            await uploader.submit_publish(test.page)
        with patch.object(self.module, "tencent_setup", AsyncMock(return_value={"success": True})), \
             patch.object(self.module.TencentVideo, "main", main), \
             patch.object(self.module.TencentVideo, "upload_video_file", AsyncMock()), \
             patch.object(self.module.TencentVideo, "wait_for_upload_complete", AsyncMock()):
            return asyncio.run(publish(self.args, self.state, self.module, self.form))

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
        with patch.object(self.module, 'tencent_setup', AsyncMock(return_value=False)):
            with self.assertRaises(PublishError) as raised:
                asyncio.run(publish(self.args, self.state, self.module, self.form))
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
        with patch.object(self.module, 'tencent_setup', AsyncMock(return_value={'success': True})), \
             patch.object(self.module.TencentVideo, 'main', swallowed), \
             patch.object(self.module.TencentVideo, 'upload_video_file', AsyncMock()), \
             patch.object(self.module.TencentVideo, 'wait_for_upload_complete', AsyncMock()):
            with self.assertRaises(PublishError):
                asyncio.run(publish(self.args, self.state, self.module, self.form))
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


if __name__ == '__main__': unittest.main()
