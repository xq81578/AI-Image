import importlib.util
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
import urllib.error


SCRIPT = Path(__file__).resolve().parents[1] / "skills/telegram-notify/scripts/telegram_notify.py"
spec = importlib.util.spec_from_file_location("telegram_notify", SCRIPT)
notify = importlib.util.module_from_spec(spec)
spec.loader.exec_module(notify)


class TelegramNotifyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.folder = Path(self.temporary.name)
        self.config = {"token": "fake-secret", "bot_id": 1, "username": "example_bot", "chat_id": 2}

    def test_sent_receipt_prevents_duplicate_and_changed_content(self):
        with patch.object(notify, "api", return_value={"message_id": 123}) as api:
            self.assertEqual(notify.send(self.folder, self.config, "run:complete", "完成")["status"], "sent")
            self.assertEqual(notify.send(self.folder, self.config, "run:complete", "完成")["status"], "already_sent")
            with self.assertRaisesRegex(ValueError, "内容已变化"):
                notify.send(self.folder, self.config, "run:complete", "另一条")
            self.assertEqual(api.call_count, 1)

    def test_uncertain_result_is_not_retried(self):
        with patch.object(notify, "api", side_effect=notify.DeliveryError("网络中断", uncertain=True)) as api:
            with self.assertRaises(notify.DeliveryError):
                notify.send(self.folder, self.config, "run:blocked", "需处理")
            with self.assertRaisesRegex(ValueError, "结果不确定"):
                notify.send(self.folder, self.config, "run:blocked", "需处理")
            self.assertEqual(api.call_count, 1)

    def test_process_interruption_leaves_pending(self):
        with patch.object(notify, "api", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                notify.send(self.folder, self.config, "run:complete", "完成")
        with patch.object(notify, "api") as api:
            with self.assertRaisesRegex(ValueError, "结果不确定"):
                notify.send(self.folder, self.config, "run:complete", "完成")
            api.assert_not_called()

    def test_rejected_request_has_cooldown(self):
        with patch.object(notify, "api", side_effect=notify.DeliveryError("拒绝")):
            with self.assertRaises(notify.DeliveryError):
                notify.send(self.folder, self.config, "event", "完成")
        with patch.object(notify, "api", return_value={"message_id": 2}) as api:
            with self.assertRaisesRegex(ValueError, "60 秒"):
                notify.send(self.folder, self.config, "event", "完成")
            with patch.object(notify.time, "time", return_value=time.time() + 61):
                self.assertEqual(notify.send(self.folder, self.config, "event", "完成")["status"], "sent")
            self.assertEqual(api.call_count, 1)

    def test_no_binding_or_invalid_attachment_never_sends(self):
        with patch.object(notify, "api") as api:
            with self.assertRaisesRegex(ValueError, "尚未绑定"):
                notify.send(self.folder, {"token": "fake"}, "event", "完成")
            with self.assertRaisesRegex(ValueError, "不存在"):
                notify.send(self.folder, self.config, "event", "完成", "video", self.folder / "missing.mp4")
            video = self.folder / "large.mp4"
            with video.open("wb") as stream:
                stream.truncate(50_000_001)
            with self.assertRaisesRegex(ValueError, "大小"):
                notify.send(self.folder, self.config, "event", "完成", "video", video)
            api.assert_not_called()

    def test_video_targets_bound_chat_and_uses_local_bytes(self):
        video = self.folder / "成品.mp4"
        video.write_bytes(b"test-mp4-content")
        with patch.object(notify, "api", return_value={"message_id": 8}) as api:
            notify.send(self.folder, self.config, "run:video", "成品", "video", video)
            args = api.call_args.args
            self.assertEqual(args[1], "sendVideo")
            self.assertEqual(args[2], {"chat_id": 2, "caption": "成品"})
            self.assertEqual(args[3][2], b"test-mp4-content")

    def test_binding_only_accepts_fresh_private_challenge(self):
        config = {"token": "fake", "username": "example_bot", "nonce": "secret-code", "expires": time.time() + 800}
        def update(number, text, kind="private", age=0):
            return {"update_id": number, "message": {"text": text, "chat": {"id": 9, "type": kind},
                    "from": {"id": 9, "is_bot": False}, "date": int(time.time() - age)}}
        updates = [update(1, "/start wrong"), update(2, "/start secret-code", "group"),
                   update(3, "/start secret-code", age=3600)]
        with patch.object(notify, "save_config"), patch.object(notify, "api", return_value=updates):
            self.assertEqual(notify.poll_binding(self.folder, config)["status"], "awaiting_user")
        with patch.object(notify, "save_config"), patch.object(notify, "api", return_value=[update(4, "/start secret-code")]):
            self.assertEqual(notify.poll_binding(self.folder, config)["status"], "ready")
        self.assertEqual(config["chat_id"], 9)
        self.assertNotIn("nonce", config)

    def test_existing_webhook_is_not_removed(self):
        with patch.object(notify, "api", return_value={"url": "https://example.test/webhook"}) as api:
            with self.assertRaisesRegex(ValueError, "webhook"):
                notify.start_binding(self.folder, {"token": "fake"})
            self.assertEqual(api.call_count, 1)
            self.assertEqual(api.call_args.args[1], "getWebhookInfo")

    def test_http_error_does_not_expose_token(self):
        url = "https://api.telegram.org/bot123:secret/getMe"
        with patch.object(notify.urllib.request, "build_opener") as opener:
            opener.return_value.open.side_effect = urllib.error.HTTPError(url, 401, url, {}, None)
            with self.assertRaises(notify.DeliveryError) as error:
                notify.api("123:secret", "getMe")
            self.assertNotIn("secret", str(error.exception))
            self.assertNotIn(url, str(error.exception))

    @unittest.skipUnless(os.name == "nt", "Windows DPAPI")
    def test_credentials_are_encrypted_and_round_trip(self):
        notify.save_config(self.folder, self.config)
        raw = (self.folder / "credentials.dpapi").read_bytes()
        self.assertNotIn(b"fake-secret", raw)
        self.assertEqual(notify.load_config(self.folder), self.config)


if __name__ == "__main__":
    unittest.main()
