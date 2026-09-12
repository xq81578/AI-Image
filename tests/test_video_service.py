import base64
import json
from pathlib import Path
import shutil
import sys
import threading
import time
import unittest

from PIL import Image

from video_service import VideoService, VideoServiceError, build_ffmpeg_command


class VideoServiceTests(unittest.TestCase):
    root = Path(__file__).resolve().parent.parent / "video-test-runtime"

    def setUp(self):
        self.runtime = self.root / self._testMethodName
        shutil.rmtree(self.runtime, ignore_errors=True)
        self.bgm = self.runtime / "bgm"
        self.output = self.runtime / "output"
        self.bgm.mkdir(parents=True)
        self.output.mkdir(parents=True)
        (self.bgm / "one.mp3").write_bytes(b"test audio")
        (self.bgm / "two.m4a").write_bytes(b"test audio")
        self.runner_state = {"fail": False}
        self.current_output_dir = self.output

        def export_runner(task, target):
            if self.runner_state["fail"]:
                raise RuntimeError("simulated ffmpeg failure")
            target.write_bytes(b"fake mp4")

        self.service = VideoService(
            project_root=self.runtime,
            output_dir_provider=lambda: self.current_output_dir,
            ffmpeg_path=sys.executable,
            audio_probe=lambda _ffmpeg, _audio: True,
            export_runner=export_runner,
            image_saver=lambda filename, data, _request_id, folder: self._save_image(filename, data, folder),
        )

    def tearDown(self):
        deadline = time.time() + 2
        while time.time() < deadline:
            tasks = list(self.service._tasks.values())
            if not any(task.thread is not None and task.thread.is_alive() for task in tasks):
                break
            time.sleep(0.01)
        shutil.rmtree(self.runtime, ignore_errors=True)

    def _save_image(self, filename, data, folder):
        path = folder / filename
        path.write_bytes(data)
        return {"path": str(path), "filename": filename}

    def _data_url(self, color=(90, 120, 150)):
        import io

        stream = io.BytesIO()
        Image.new("RGB", (8, 8), color).save(stream, format="PNG")
        return "data:image/png;base64," + base64.b64encode(stream.getvalue()).decode("ascii")

    def _prepare(self, batch_id="batch1", job_ids=None, filename="clip.mp4", save_images=False):
        return self.service.prepare(
            batch_id=batch_id,
            job_ids=job_ids or ["job-1", "job-2"],
            filename=filename,
            save_images=save_images,
        )

    def _frame(self, batch_id="batch1", job_id="job-1", color=(90, 120, 150), filename=None):
        return self.service.frame(
            batch_id=batch_id,
            job_id=job_id,
            filename=filename or f"{job_id}.png",
            data_url=self._data_url(color),
        )

    def _wait_terminal(self, batch_id):
        deadline = time.time() + 3
        while time.time() < deadline:
            state = self.service.status(batch_id)
            if state["status"] in {"done", "error", "cancelled"}:
                return state
            time.sleep(0.02)
        self.fail("视频任务没有在测试时间内结束")

    def test_prepare_is_idempotent_and_conflict_is_rejected(self):
        first = self._prepare()
        second = self._prepare()
        self.assertEqual(first, second)
        self.assertEqual(first["status"], "waiting")
        self.assertEqual(first["outputDir"], str(self.output.resolve()))
        self.assertEqual(first["missingJobIds"], ["job-1", "job-2"])
        self.current_output_dir = None
        self.assertEqual(self._prepare(), first)
        with self.assertRaisesRegex(VideoServiceError, "不能改变"):
            self._prepare(job_ids=["job-1", "job-3"])
        with self.assertRaisesRegex(VideoServiceError, "不能改变"):
            self._prepare(save_images=True)
        self.assertTrue((self.runtime / ".video-cache" / "batch1" / "manifest.json").is_file())

    def test_missing_frames_and_frame_idempotence(self):
        self._prepare()
        with self.assertRaisesRegex(VideoServiceError, "所有合图"):
            self.service.start(batch_id="batch1")
        with self.assertRaises(VideoServiceError) as context:
            self._frame(job_id="unknown")
        self.assertEqual(context.exception.code, "job_not_required")
        first = self._frame()
        self.assertEqual(first, self._frame())
        with self.assertRaisesRegex(VideoServiceError, "不同合图"):
            self._frame(color=(1, 2, 3))

    def test_frame_cache_sequence_keeps_original_group_order_after_skip(self):
        self._prepare()
        self.service.skip(batch_id="batch1", job_id="job-1")
        self._frame(job_id="job-2")
        frame = self.service._tasks["batch1"].frames["job-2"]
        self.assertEqual(frame.cache_name, "0002_job-2.png")

    def test_success_cleans_cache_and_keeps_manifest(self):
        self._prepare()
        self._frame()
        self._frame(job_id="job-2", color=(1, 2, 3))
        running = self.service.start(batch_id="batch1")
        self.assertEqual(running["status"], "running")
        state = self._wait_terminal("batch1")
        self.assertEqual(state["status"], "done")
        self.assertTrue(Path(state["path"]).is_file())
        cache_dir = self.runtime / ".video-cache" / "batch1"
        self.assertTrue((cache_dir / "manifest.json").is_file())
        self.assertEqual(list(cache_dir.glob("*.png")), [])

    def test_failure_retains_cache_and_retry_succeeds(self):
        self._prepare()
        self._frame()
        self._frame(job_id="job-2")
        self.runner_state["fail"] = True
        self.service.start(batch_id="batch1")
        failed = self._wait_terminal("batch1")
        self.assertEqual(failed["status"], "error")
        self.assertTrue(list((self.runtime / ".video-cache" / "batch1").glob("*.png")))
        self.runner_state["fail"] = False
        self.assertEqual(self.service.start(batch_id="batch1")["status"], "running")
        self.assertEqual(self._wait_terminal("batch1")["status"], "done")
        self.assertEqual(list((self.runtime / ".video-cache" / "batch1").glob("*.png")), [])

    def test_cancel_retains_cache_and_discard_removes_batch(self):
        started = threading.Event()
        cancellation_seen = threading.Event()
        release = threading.Event()

        def blocking_runner(task, target):
            started.set()
            while not task.cancel_event.wait(0.01):
                pass
            cancellation_seen.set()
            release.wait(1)

        self.service._export_runner = blocking_runner
        self._prepare()
        self._frame()
        self._frame(job_id="job-2")
        self.service.start(batch_id="batch1")
        self.assertTrue(started.wait(1))
        self.assertEqual(self.service.cancel(batch_id="batch1")["status"], "cancelled")
        self.assertTrue(cancellation_seen.wait(1))
        with self.assertRaisesRegex(VideoServiceError, "正在停止"):
            self.service.start(batch_id="batch1")
        self.assertEqual(self._wait_terminal("batch1")["status"], "cancelled")
        release.set()
        thread = self.service._tasks["batch1"].thread
        if thread is not None:
            thread.join(1)
        self.assertTrue((self.runtime / ".video-cache" / "batch1" / "0001_job-1.png").is_file())
        self.assertEqual(self.service.discard(batch_id="batch1"), {"batchId": "batch1", "discarded": True})
        with self.assertRaises(VideoServiceError):
            self.service.status("batch1")

    def test_close_stops_running_export_and_keeps_retryable_state(self):
        started = threading.Event()

        def runner(task, _target):
            started.set()
            task.cancel_event.wait(2)

        self.service._export_runner = runner
        self._prepare()
        self._frame()
        self._frame(job_id="job-2")
        self.service.start(batch_id="batch1")
        self.assertTrue(started.wait(1))
        self.service.close(timeout=1)
        self.assertEqual(self.service.status("batch1")["status"], "cancelled")
        self.assertTrue((self.runtime / ".video-cache" / "batch1" / "0001_job-1.png").is_file())

    def test_busy_does_not_change_other_task(self):
        release = threading.Event()

        def first_runner(_task, target):
            release.wait(2)
            target.write_bytes(b"fake mp4")

        self.service._export_runner = first_runner
        self._prepare("batch1", ["a"])
        self._frame("batch1", "a")
        self._prepare("batch2", ["b"])
        self._frame("batch2", "b")
        self.service.start(batch_id="batch1")
        with self.assertRaisesRegex(VideoServiceError, "另一个视频"):
            self.service.start(batch_id="batch2")
        self.assertEqual(self.service.status("batch2")["status"], "waiting")
        release.set()
        self.assertEqual(self._wait_terminal("batch1")["status"], "done")

    def test_skip_is_idempotent_and_empty_batch_cannot_start(self):
        self._prepare()
        state = self.service.skip(batch_id="batch1", job_id="job-1")
        self.assertEqual(state["missingJobIds"], ["job-2"])
        self.assertEqual(self.service.skip(batch_id="batch1", job_id="job-1"), state)
        with self.assertRaisesRegex(VideoServiceError, "已被排除"):
            self._frame(job_id="job-1")
        state = self.service.skip(batch_id="batch1", job_id="job-2")
        self.assertNotIn("missingJobIds", state)
        with self.assertRaisesRegex(VideoServiceError, "空视频"):
            self.service.start(batch_id="batch1")

    def test_cleanup_failure_is_reported_after_video_publish(self):
        self._prepare()
        self._frame()
        self._frame(job_id="job-2")

        def fail_cleanup(_task):
            raise VideoServiceError("simulated cleanup failure", "cache_cleanup_failed", 500)

        self.service._clean_cache_frames = fail_cleanup
        self.service.start(batch_id="batch1")
        state = self._wait_terminal("batch1")
        self.assertEqual(state["status"], "error")
        self.assertEqual(state["error"], "simulated cleanup failure")
        self.assertTrue(Path(state["path"]).is_file())
        self.service._clean_cache_frames = VideoService._clean_cache_frames.__get__(self.service)
        self.assertEqual(self.service.start(batch_id="batch1")["status"], "done")

    def test_restart_running_manifest_becomes_retryable_error(self):
        self._prepare()
        self._frame()
        manifest = self.runtime / ".video-cache" / "batch1" / "manifest.json"
        payload = json.loads(manifest.read_text(encoding="utf-8"))
        payload["status"] = "running"
        manifest.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        restored = VideoService(
            project_root=self.runtime,
            output_dir_provider=lambda: self.output,
            ffmpeg_path=sys.executable,
            audio_probe=lambda _ffmpeg, _audio: True,
        )
        state = restored.status("batch1")
        self.assertEqual(state["status"], "error")
        self.assertIn("服务重启", state["error"])

    def test_same_output_name_is_reserved_without_overwrite(self):
        first = self._prepare("batch1", ["a"])
        second = self._prepare("batch2", ["b"], filename="clip.mp4")
        self.assertEqual(first["filename"], "clip.mp4")
        self.assertEqual(second["filename"], "clip (2).mp4")

    def test_build_command_has_expected_video_contract(self):
        command = build_ffmpeg_command(
            "ffmpeg.exe",
            ["old.png", "now.png"],
            "music.mp3",
            "out.mp4",
        )
        joined = " ".join(command)
        self.assertIn("-stream_loop -1", joined)
        self.assertIn("-t 8", joined)
        self.assertIn("xfade=transition=fade", joined)
        self.assertIn("-filter_complex_threads 1", joined)
        self.assertIn("-threads 2", joined)
        self.assertNotIn("drawtext", joined)


if __name__ == "__main__":
    unittest.main()
