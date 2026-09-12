"""本机时光对照视频服务。

视频服务只接收浏览器已经合成好的 PNG，使用本机 FFmpeg 生成成品并保存到
用户选择的目录。模型请求仍由浏览器完成；这里不代理模型，也不提供任意文件
读取接口。
"""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass, field
import hashlib
import io
import json
import os
from pathlib import Path
import random
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any, Callable, Optional
import warnings

try:
    from PIL import Image
except ImportError as exc:  # pragma: no cover - installation is part of the app setup
    raise RuntimeError("需要安装 Pillow 才能启动本机视频服务") from exc


VIDEO_STATUSES = frozenset({"waiting", "running", "done", "error", "cancelled"})
AUDIO_EXTENSIONS = frozenset({".mp3", ".m4a", ".wav", ".aac", ".flac", ".ogg"})
MAX_JOBS = 12
MAX_BATCH_ID_LENGTH = 128
MAX_JOB_ID_LENGTH = 180
MAX_VIDEO_FILENAME_LENGTH = 180
MAX_FRAME_FILENAME_LENGTH = 180
MAX_FRAME_BYTES = 20 * 1024 * 1024
VIDEO_SECONDS_PER_IMAGE = 4.0
VIDEO_TRANSITION_SECONDS = 0.6
VIDEO_WIDTH = 1080
VIDEO_HEIGHT = 1920
VIDEO_FPS = 30
VIDEO_CACHE_NAME = ".video-cache"

_BATCH_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_JOB_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,179}$")
_SAFE_FILENAME_RE = re.compile(r"^[^<>:\"/\\|?*\x00-\x1f\x7f]+$")
_WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


class VideoServiceError(Exception):
    """可以安全返回给浏览器的本机视频错误。"""

    def __init__(self, message: str, code: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.code = code
        self.status = status


class _VideoCancelled(Exception):
    pass


@dataclass
class _Frame:
    filename: str
    cache_name: str
    digest: str
    size: int
    saved_path: Optional[str] = None
    saved_filename: Optional[str] = None

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "filename": self.filename,
            "cacheName": self.cache_name,
            "digest": self.digest,
            "size": self.size,
        }
        if self.saved_path:
            result["savedPath"] = self.saved_path
        if self.saved_filename:
            result["savedFilename"] = self.saved_filename
        return result

    @classmethod
    def from_dict(cls, value: Any) -> Optional["_Frame"]:
        if not isinstance(value, dict):
            return None
        filename = value.get("filename")
        cache_name = value.get("cacheName")
        digest = value.get("digest")
        size = value.get("size")
        if (
            not isinstance(filename, str)
            or not isinstance(cache_name, str)
            or not isinstance(digest, str)
            or not re.fullmatch(r"[0-9a-f]{64}", digest)
            or not isinstance(size, int)
            or size <= 0
            or size > MAX_FRAME_BYTES
        ):
            return None
        try:
            _validate_frame_filename(filename)
            _validate_cache_name(cache_name)
        except VideoServiceError:
            return None
        saved_path = value.get("savedPath")
        saved_filename = value.get("savedFilename")
        if saved_path is not None and not isinstance(saved_path, str):
            saved_path = None
        if saved_filename is not None and not isinstance(saved_filename, str):
            saved_filename = None
        return cls(filename, cache_name, digest, size, saved_path, saved_filename)


@dataclass
class _Task:
    batch_id: str
    original_job_ids: list[str]
    job_ids: list[str]
    requested_filename: str
    filename: str
    output_dir: Path
    bgm_path: Path
    ffmpeg_path: Path
    save_images: bool
    status: str = "waiting"
    progress: int = 0
    path: Optional[str] = None
    error: Optional[str] = None
    frames: dict[str, _Frame] = field(default_factory=dict)
    process: Optional[subprocess.Popen] = field(default=None, repr=False)
    thread: Optional[threading.Thread] = field(default=None, repr=False)
    cancel_event: threading.Event = field(default_factory=threading.Event, repr=False)

def _hidden_subprocess_kwargs() -> dict[str, Any]:
    if os.name != "nt":
        return {}
    startup_info = subprocess.STARTUPINFO()
    startup_info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup_info.wShowWindow = subprocess.SW_HIDE
    return {
        "startupinfo": startup_info,
        "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0),
    }


def _validate_id(value: Any, label: str, *, batch: bool = False) -> str:
    limit = MAX_BATCH_ID_LENGTH if batch else MAX_JOB_ID_LENGTH
    pattern = _BATCH_ID_RE if batch else _JOB_ID_RE
    if not isinstance(value, str) or not value or len(value) > limit or not pattern.fullmatch(value):
        raise VideoServiceError(f"{label} 无效", "invalid_id", 400)
    if value in {".", ".."} or ".." in value:
        raise VideoServiceError(f"{label} 无效", "invalid_id", 400)
    return value


def _validate_cache_name(value: Any) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > MAX_FRAME_FILENAME_LENGTH + 10
        or value in {".", ".."}
        or ".." in value
        or not _SAFE_FILENAME_RE.fullmatch(value)
        or Path(value).name != value
    ):
        raise VideoServiceError("视频缓存文件名无效", "cache_unavailable", 500)
    return value


def _validate_filename(value: Any, extension: str, label: str, limit: int) -> str:
    if not isinstance(value, str) or not value or len(value) > limit:
        raise VideoServiceError(f"{label} 无效", "invalid_filename", 400)
    if value in {".", ".."} or ".." in value or not _SAFE_FILENAME_RE.fullmatch(value):
        raise VideoServiceError(f"{label} 包含非法字符", "invalid_filename", 400)
    if any(not character.isprintable() for character in value) or value.endswith((".", " ")):
        raise VideoServiceError(f"{label} 包含非法字符", "invalid_filename", 400)
    suffix = Path(value).suffix.lower()
    if suffix != extension:
        raise VideoServiceError(f"{label} 必须使用 {extension} 扩展名", "invalid_filename", 400)
    stem = Path(value).stem.rstrip(" .")
    if not stem or stem.upper() in _WINDOWS_RESERVED_NAMES:
        raise VideoServiceError(f"{label} 不可用", "invalid_filename", 400)
    return value


def _validate_video_filename(value: Any) -> str:
    return _validate_filename(value, ".mp4", "视频文件名", MAX_VIDEO_FILENAME_LENGTH)


def _validate_frame_filename(value: Any) -> str:
    return _validate_filename(value, ".png", "合图文件名", MAX_FRAME_FILENAME_LENGTH)


def _path_inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (OSError, ValueError):
        return False


def _is_occupied(path: Path) -> bool:
    return os.path.lexists(os.fspath(path))


def _same_file_bytes(path: Path, digest: str, size: int) -> bool:
    try:
        if not path.is_file() or path.stat().st_size != size:
            return False
        hasher = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                hasher.update(chunk)
        return hasher.hexdigest() == digest
    except OSError:
        return False


def _alternate_filename(filename: str, number: int) -> str:
    path = Path(filename)
    return f"{path.stem} ({number}){path.suffix}"


def _decode_png_data_url(value: Any) -> bytes:
    if not isinstance(value, str) or len(value) > (MAX_FRAME_BYTES * 4 // 3 + 4096):
        raise VideoServiceError("PNG 数据无效或过大", "invalid_png", 400)
    header, separator, payload = value.partition(",")
    parts = header.split(";")
    if not separator or not parts or parts[0].lower() != "data:image/png" or "base64" not in {
        part.lower() for part in parts[1:]
    }:
        raise VideoServiceError("只接受 base64 PNG 数据", "invalid_png", 400)
    payload = re.sub(r"\s+", "", payload)
    try:
        data = base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise VideoServiceError("PNG 数据无法解码", "invalid_png", 400) from exc
    if not data or len(data) > MAX_FRAME_BYTES:
        raise VideoServiceError("PNG 体积超出限制", "invalid_png", 413)
    try:
        with Image.open(io.BytesIO(data)) as image:
            if (image.format or "").upper() != "PNG":
                raise VideoServiceError("数据内容不是 PNG", "invalid_png", 400)
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                image.verify()
        with Image.open(io.BytesIO(data)) as image:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                image.load()
            if image.width <= 0 or image.height <= 0:
                raise VideoServiceError("PNG 尺寸无效", "invalid_png", 400)
    except VideoServiceError:
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning, OSError, ValueError) as exc:
        raise VideoServiceError("PNG 内容无法验证", "invalid_png", 400) from exc
    return data


def build_ffmpeg_command(
    ffmpeg: os.PathLike[str] | str,
    images: list[os.PathLike[str] | str],
    audio: os.PathLike[str] | str,
    output: os.PathLike[str] | str,
    *,
    seconds_per_image: float = VIDEO_SECONDS_PER_IMAGE,
    transition_duration: float = VIDEO_TRANSITION_SECONDS,
    width: int = VIDEO_WIDTH,
    height: int = VIDEO_HEIGHT,
    fps: int = VIDEO_FPS,
) -> list[str]:
    """构造固定竖屏视频命令；输入合图已经带年份文字，因此不追加 drawtext。"""
    if not images:
        raise VideoServiceError("没有可制作的视频图片", "empty_frames", 409)
    still_duration = seconds_per_image + transition_duration if len(images) > 1 else seconds_per_image
    command: list[str] = [os.fspath(ffmpeg), "-y", "-hide_banner", "-loglevel", "error"]
    for image in images:
        command.extend(["-loop", "1", "-t", f"{still_duration:g}", "-i", os.fspath(image)])
    command.extend(["-stream_loop", "-1", "-i", os.fspath(audio)])

    filters: list[str] = []
    for index in range(len(images)):
        filters.append(
            f"[{index}:v]scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1,fps={fps},"
            f"format=yuv420p,settb=AVTB[v{index}]"
        )
    if len(images) == 1:
        filters.append("[v0]null[outv]")
    else:
        previous = "v0"
        for index in range(1, len(images)):
            output_label = f"xf{index}"
            offset = seconds_per_image * index
            filters.append(
                f"[{previous}][v{index}]xfade=transition=fade:duration={transition_duration:g}:"
                f"offset={offset:g}[{output_label}]"
            )
            previous = output_label
        filters.append(f"[{previous}]null[outv]")

    total_duration = seconds_per_image * len(images)
    command.extend([
        "-filter_complex",
        ";".join(filters),
        "-map",
        "[outv]",
        "-map",
        f"{len(images)}:a:0",
        "-t",
        f"{total_duration:g}",
        "-r",
        str(fps),
        "-c:v",
        "libx264",
        "-preset",
        "medium",
        "-crf",
        "20",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-movflags",
        "+faststart",
        "-filter_complex_threads",
        "1",
        "-threads",
        "2",
        "-progress",
        "pipe:1",
        "-nostats",
        os.fspath(output),
    ])
    return command


class VideoService:
    """管理本机视频批次、持久 manifest 与单一 FFmpeg 导出线程。"""

    def __init__(
        self,
        *,
        project_root: os.PathLike[str] | str,
        output_dir_provider: Callable[[], Optional[Path | str]],
        cache_root: Optional[os.PathLike[str] | str] = None,
        image_saver: Optional[Callable[[str, bytes, str, Path], dict[str, str]]] = None,
        ffmpeg_path: Optional[os.PathLike[str] | str] = None,
        ffprobe_path: Optional[os.PathLike[str] | str] = None,
        audio_probe: Optional[Callable[[Path, Path], bool]] = None,
        export_runner: Optional[Callable[[_Task, Path], None]] = None,
        recover_running: bool = True,
    ):
        self.project_root = Path(project_root).expanduser().resolve()
        self.cache_root = (
            Path(cache_root).expanduser().resolve()
            if cache_root is not None
            else self.project_root / VIDEO_CACHE_NAME
        )
        self._output_dir_provider = output_dir_provider
        self._image_saver = image_saver
        self._ffmpeg_override = Path(ffmpeg_path).expanduser().resolve() if ffmpeg_path is not None else None
        self._ffprobe_override = Path(ffprobe_path).expanduser().resolve() if ffprobe_path is not None else None
        self._audio_probe_override = audio_probe
        self._export_runner = export_runner
        self._lock = threading.RLock()
        self._tasks: dict[str, _Task] = {}
        self._reserved_outputs: dict[Path, str] = {}
        self._active_batch_id: Optional[str] = None
        self._load_manifests(recover_running=recover_running)

    def _cache_dir(self, batch_id: str) -> Path:
        return self.cache_root / batch_id

    def _manifest_path(self, task_or_batch: _Task | str) -> Path:
        batch_id = task_or_batch.batch_id if isinstance(task_or_batch, _Task) else task_or_batch
        return self._cache_dir(batch_id) / "manifest.json"

    def _find_ffmpeg(self) -> Optional[Path]:
        candidates: list[Path] = []
        if self._ffmpeg_override is not None:
            candidates.append(self._ffmpeg_override)
        candidates.extend([
            self.project_root / "tools" / "ffmpeg.exe",
            self.project_root / "tools" / "ffmpeg",
            Path(r"C:\Users\Administrator\Documents\AI出视频\tools\ffmpeg.exe"),
        ])
        executable = shutil.which("ffmpeg")
        if executable:
            candidates.append(Path(executable))
        for candidate in candidates:
            try:
                if candidate.is_file():
                    return candidate.resolve()
            except OSError:
                continue
        return None

    def _find_ffprobe(self, ffmpeg: Path) -> Optional[Path]:
        candidates: list[Path] = []
        if self._ffprobe_override is not None:
            candidates.append(self._ffprobe_override)
        if ffmpeg.name.lower().startswith("ffmpeg"):
            candidates.append(ffmpeg.with_name("ffprobe" + ffmpeg.suffix))
        executable = shutil.which("ffprobe")
        if executable:
            candidates.append(Path(executable))
        for candidate in candidates:
            try:
                if candidate.is_file():
                    return candidate.resolve()
            except OSError:
                continue
        return None

    def _probe_audio(self, ffmpeg: Path, audio: Path) -> bool:
        if self._audio_probe_override is not None:
            try:
                return bool(self._audio_probe_override(ffmpeg, audio))
            except Exception:
                return False
        ffprobe = self._find_ffprobe(ffmpeg)
        if ffprobe is not None:
            command = [
                os.fspath(ffprobe),
                "-v",
                "error",
                "-select_streams",
                "a:0",
                "-show_entries",
                "stream=codec_type",
                "-of",
                "json",
                os.fspath(audio),
            ]
            try:
                completed = subprocess.run(
                    command,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=10,
                    **_hidden_subprocess_kwargs(),
                )
                if completed.returncode != 0:
                    return False
                payload = json.loads(completed.stdout.decode("utf-8", "replace"))
                return any(
                    isinstance(stream, dict) and stream.get("codec_type") == "audio"
                    for stream in payload.get("streams", [])
                )
            except (OSError, ValueError, json.JSONDecodeError, subprocess.TimeoutExpired):
                return False
        command = [
            os.fspath(ffmpeg),
            "-v",
            "error",
            "-nostdin",
            "-i",
            os.fspath(audio),
            "-map",
            "0:a:0",
            "-frames:a",
            "1",
            "-f",
            "null",
            "-",
        ]
        try:
            completed = subprocess.run(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=12,
                **_hidden_subprocess_kwargs(),
            )
            return completed.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            return False

    def _scan_audio(self, ffmpeg: Path) -> list[Path]:
        bgm_root = self.project_root / "bgm"
        try:
            candidates = sorted(
                (path for path in bgm_root.iterdir() if path.is_file() and path.suffix.lower() in AUDIO_EXTENSIONS),
                key=lambda path: path.name.casefold(),
            )
        except OSError:
            return []
        valid: list[Path] = []
        for path in candidates:
            try:
                if path.stat().st_size > 0 and self._probe_audio(ffmpeg, path):
                    valid.append(path.resolve())
            except OSError:
                continue
        return valid

    def environment(self) -> dict[str, Any]:
        ffmpeg = self._find_ffmpeg()
        if ffmpeg is None:
            return {
                "ready": False,
                "bgmCount": 0,
                "message": "未找到本机 FFmpeg，请检查 tools/ffmpeg.exe。",
            }
        valid = self._scan_audio(ffmpeg)
        if not valid:
            return {
                "ready": False,
                "bgmCount": 0,
                "ffmpeg": str(ffmpeg),
                "message": "bgm 目录中没有可用音轨。",
            }
        return {"ready": True, "bgmCount": len(valid), "ffmpeg": str(ffmpeg)}

    def _output_dir(self) -> Path:
        try:
            value = self._output_dir_provider()
        except Exception as exc:
            raise VideoServiceError("保存目录不可用", "invalid_folder", 409) from exc
        if value is None or value == "":
            raise VideoServiceError("请先选择视频保存目录", "folder_not_selected", 409)
        try:
            path = Path(value).expanduser().resolve()
            if not path.is_dir():
                raise VideoServiceError("保存目录不可用", "invalid_folder", 409)
            # 在准备阶段确认目录可写，避免已调用模型后才发现输出路径无效。
            fd, probe_name = tempfile.mkstemp(prefix=".time-echo-video-probe-", dir=path)
            os.close(fd)
            Path(probe_name).unlink(missing_ok=True)
            return path
        except VideoServiceError:
            raise
        except OSError as exc:
            raise VideoServiceError("保存目录不可用", "invalid_folder", 409) from exc

    def _allocate_output_filename(self, output_dir: Path, filename: str, batch_id: str) -> str:
        attempt = 1
        while True:
            candidate_name = filename if attempt == 1 else _alternate_filename(filename, attempt)
            candidate = (output_dir / candidate_name).resolve()
            if not _path_inside(candidate, output_dir):
                raise VideoServiceError("视频输出路径无效", "invalid_filename", 400)
            owner = self._reserved_outputs.get(candidate)
            if owner is None and not _is_occupied(candidate):
                self._reserved_outputs[candidate] = batch_id
                return candidate_name
            if owner == batch_id:
                return candidate_name
            attempt += 1

    def _validate_job_ids(self, values: Any) -> list[str]:
        if not isinstance(values, list) or not values or len(values) > MAX_JOBS:
            raise VideoServiceError(f"每批需要 1 至 {MAX_JOBS} 组图片", "invalid_job_ids", 400)
        result = [_validate_id(value, "jobId") for value in values]
        if len(set(result)) != len(result):
            raise VideoServiceError("jobId 不能重复", "invalid_job_ids", 400)
        return result

    def _load_manifests(self, *, recover_running: bool) -> None:
        try:
            entries = list(self.cache_root.iterdir())
        except OSError:
            return
        for directory in entries:
            if not directory.is_dir() or directory.is_symlink():
                continue
            manifest_path = directory / "manifest.json"
            try:
                payload = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                continue
            task = self._task_from_manifest(directory.name, payload)
            if task is None:
                continue
            if recover_running and task.status == "running":
                task.status = "error"
                task.error = "服务重启，视频任务已停止，请重试。"
                task.progress = min(task.progress, 99)
            self._tasks[task.batch_id] = task
            output_path = (task.output_dir / task.filename).resolve()
            if task.status in {"waiting", "running", "error", "cancelled"}:
                self._reserved_outputs[output_path] = task.batch_id
            if recover_running and task.status == "running":
                self._write_manifest(task)

    def recover_running_tasks(self) -> None:
        """在 HTTP 端口成功绑定后恢复上次未收尾的任务。

        LocalApp 延迟调用这一方法，避免重复启动器在绑定失败前修改正在运行
        的旧实例 manifest。
        """
        with self._lock:
            for task in self._tasks.values():
                if task.status != "running":
                    continue
                task.status = "error"
                task.error = "服务重启，视频任务已停止，请重试。"
                task.progress = min(task.progress, 99)
                self._write_manifest(task)

    def _task_from_manifest(self, directory_name: str, payload: Any) -> Optional[_Task]:
        try:
            batch_id = _validate_id(payload.get("batchId", directory_name), "batchId", batch=True)
            if batch_id != directory_name:
                return None
            original = self._validate_job_ids(payload.get("originalJobIds") or payload.get("jobIds"))
            active_values = payload.get("jobIds", original)
            if not isinstance(active_values, list):
                return None
            active = [_validate_id(value, "jobId") for value in active_values]
            if len(set(active)) != len(active) or any(value not in original for value in active):
                return None
            requested = _validate_video_filename(payload.get("requestedFilename") or payload.get("filename"))
            filename = _validate_video_filename(payload.get("filename"))
            output_dir = Path(payload.get("outputDir", "")).expanduser().resolve()
            bgm_path = Path(payload.get("bgmPath", "")).expanduser().resolve()
            ffmpeg_path = Path(payload.get("ffmpegPath", "")).expanduser().resolve()
            if not output_dir.is_dir() or not bgm_path.is_file() or not ffmpeg_path.is_file():
                # 输出目录/音轨暂时不可用时仍保留任务，start 会给出可重试错误。
                pass
            save_images = payload.get("saveImages")
            if not isinstance(save_images, bool):
                return None
            status = payload.get("status", "waiting")
            if status not in VIDEO_STATUSES:
                return None
            progress = payload.get("progress", 0)
            if not isinstance(progress, (int, float)):
                progress = 0
            progress = max(0, min(100, int(progress)))
            frames: dict[str, _Frame] = {}
            for job_id, frame_value in (payload.get("frames") or {}).items():
                if job_id in active and isinstance(job_id, str):
                    frame = _Frame.from_dict(frame_value)
                    if frame is not None:
                        frames[job_id] = frame
            path = payload.get("path") if isinstance(payload.get("path"), str) else None
            error = payload.get("error") if isinstance(payload.get("error"), str) else None
            return _Task(
                batch_id=batch_id,
                original_job_ids=original,
                job_ids=active,
                requested_filename=requested,
                filename=filename,
                output_dir=output_dir,
                bgm_path=bgm_path,
                ffmpeg_path=ffmpeg_path,
                save_images=save_images,
                status=status,
                progress=progress,
                path=path,
                error=error,
                frames=frames,
            )
        except (AttributeError, TypeError, ValueError, OSError, VideoServiceError):
            return None

    def _manifest_payload(self, task: _Task) -> dict[str, Any]:
        return {
            "batchId": task.batch_id,
            "originalJobIds": list(task.original_job_ids),
            "jobIds": list(task.job_ids),
            "requestedFilename": task.requested_filename,
            "filename": task.filename,
            "outputDir": str(task.output_dir),
            "bgm": task.bgm_path.name,
            "bgmPath": str(task.bgm_path),
            "ffmpegPath": str(task.ffmpeg_path),
            "saveImages": task.save_images,
            "status": task.status,
            "progress": task.progress,
            "path": task.path,
            "error": task.error,
            "frames": {job_id: frame.as_dict() for job_id, frame in task.frames.items()},
        }

    def _write_manifest(self, task: _Task) -> None:
        manifest_path = self._manifest_path(task)
        try:
            manifest_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = manifest_path.with_name(".manifest.tmp")
            data = json.dumps(self._manifest_payload(task), ensure_ascii=False, separators=(",", ":"))
            with temporary.open("w", encoding="utf-8", newline="\n") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, manifest_path)
        except OSError as exc:
            raise VideoServiceError("视频状态保存失败", "manifest_failed", 500) from exc

    def _snapshot(self, task: _Task) -> dict[str, Any]:
        # 成功后 PNG 已按约定清掉，manifest 中仍保留元数据；此时不能把清理
        # 结果误报成“缺少合图”。
        missing = [] if task.status == "done" else [
            job_id for job_id in task.job_ids if not self._frame_is_ready(task, job_id, verify_digest=False)
        ]
        result: dict[str, Any] = {
            "batchId": task.batch_id,
            "status": task.status,
            "progress": max(0, min(100, int(task.progress))),
            "bgm": task.bgm_path.name,
            "filename": task.filename,
            "outputDir": str(task.output_dir),
            "saveImages": task.save_images,
        }
        if task.path:
            result["path"] = task.path
        if task.error:
            result["error"] = task.error
        if missing:
            result["missingJobIds"] = missing
        return result

    def status(self, batch_id: Any) -> dict[str, Any]:
        bid = _validate_id(batch_id, "batchId", batch=True)
        with self._lock:
            task = self._tasks.get(bid)
            if task is None:
                raise VideoServiceError("视频批次不存在", "batch_not_found", 404)
            return self._snapshot(task)

    def output_folder(self, batch_id: Any) -> Path:
        """返回 manifest 中固定的批次目录，供主线程打开文件夹。"""
        bid = _validate_id(batch_id, "batchId", batch=True)
        with self._lock:
            task = self._tasks.get(bid)
            if task is None:
                raise VideoServiceError("视频批次不存在", "batch_not_found", 404)
            try:
                folder = task.output_dir.resolve()
            except OSError as exc:
                raise VideoServiceError("视频保存目录不可用", "invalid_folder", 409) from exc
            if not folder.is_dir():
                raise VideoServiceError("视频保存目录不可用", "invalid_folder", 409)
            return folder

    def prepare(self, *, batch_id: Any, job_ids: Any, filename: Any, save_images: Any) -> dict[str, Any]:
        bid = _validate_id(batch_id, "batchId", batch=True)
        ids = self._validate_job_ids(job_ids)
        requested = _validate_video_filename(filename)
        if not isinstance(save_images, bool):
            raise VideoServiceError("saveImages 必须是布尔值", "invalid_save_images", 400)
        with self._lock:
            existing = self._tasks.get(bid)
            if existing is not None:
                # 输出目录、音轨和 FFmpeg 路径来自首次 prepare；重试时即使
                # 当前全局目录尚未恢复，也应返回原状态而不是重新建批次。
                if (
                    existing.original_job_ids == ids
                    and existing.requested_filename == requested
                    and existing.save_images == save_images
                ):
                    return self._snapshot(existing)
                raise VideoServiceError("同一批次的输入不能改变", "batch_conflict", 409)

            output_dir = self._output_dir()

            ffmpeg = self._find_ffmpeg()
            if ffmpeg is None:
                raise VideoServiceError("未找到本机 FFmpeg，请检查 tools/ffmpeg.exe。", "ffmpeg_unavailable", 503)
            valid_audio = self._scan_audio(ffmpeg)
            if not valid_audio:
                raise VideoServiceError("bgm 目录中没有可用音轨。", "audio_unavailable", 503)
            bgm = random.SystemRandom().choice(valid_audio)
            cache_dir = self._cache_dir(bid)
            if cache_dir.exists() and (cache_dir.is_symlink() or not cache_dir.is_dir()):
                raise VideoServiceError("视频缓存目录无效", "cache_unavailable", 409)
            try:
                cache_dir.mkdir(parents=True, exist_ok=False)
            except FileExistsError:
                raise VideoServiceError("视频批次已存在，请刷新后重试", "batch_conflict", 409)
            except OSError as exc:
                raise VideoServiceError("无法创建视频缓存目录", "cache_unavailable", 500) from exc
            actual_filename = self._allocate_output_filename(output_dir, requested, bid)
            task = _Task(
                batch_id=bid,
                original_job_ids=list(ids),
                job_ids=list(ids),
                requested_filename=requested,
                filename=actual_filename,
                output_dir=output_dir,
                bgm_path=bgm,
                ffmpeg_path=ffmpeg,
                save_images=save_images,
            )
            self._tasks[bid] = task
            try:
                self._write_manifest(task)
            except Exception:
                self._tasks.pop(bid, None)
                self._reserved_outputs.pop((output_dir / actual_filename).resolve(), None)
                shutil.rmtree(cache_dir, ignore_errors=True)
                raise
            return self._snapshot(task)

    def _task_for(self, batch_id: Any) -> _Task:
        bid = _validate_id(batch_id, "batchId", batch=True)
        task = self._tasks.get(bid)
        if task is None:
            raise VideoServiceError("视频批次不存在", "batch_not_found", 404)
        return task

    def _frame_is_ready(self, task: _Task, job_id: str, *, verify_digest: bool = True) -> bool:
        frame = task.frames.get(job_id)
        if frame is None:
            return False
        path = self._cache_dir(task.batch_id) / frame.cache_name
        try:
            if not path.is_file() or path.stat().st_size != frame.size:
                return False
        except OSError:
            return False
        return not verify_digest or _same_file_bytes(path, frame.digest, frame.size)

    def _atomic_cache_write(self, path: Path, data: bytes) -> None:
        temporary: Optional[Path] = None
        try:
            fd, temporary_name = tempfile.mkstemp(prefix=".frame-", suffix=".tmp", dir=path.parent)
            temporary = Path(temporary_name)
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(os.fspath(temporary), os.fspath(path))
            except FileExistsError:
                if not _same_file_bytes(path, hashlib.sha256(data).hexdigest(), len(data)):
                    raise VideoServiceError("同一组收到了不同合图", "frame_conflict", 409)
        except VideoServiceError:
            raise
        except OSError as exc:
            raise VideoServiceError("视频合图缓存失败", "frame_cache_failed", 500) from exc
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass

    def frame(self, *, batch_id: Any, job_id: Any, filename: Any, data_url: Any) -> dict[str, Any]:
        bid = _validate_id(batch_id, "batchId", batch=True)
        jid = _validate_id(job_id, "jobId")
        frame_filename = _validate_frame_filename(filename)
        data = _decode_png_data_url(data_url)
        digest = hashlib.sha256(data).hexdigest()
        with self._lock:
            task = self._tasks.get(bid)
            if task is None:
                raise VideoServiceError("视频批次不存在", "batch_not_found", 404)
            if jid not in task.job_ids:
                if jid in task.original_job_ids:
                    raise VideoServiceError("该组已被排除，不能再上传视频素材", "job_skipped", 409)
                raise VideoServiceError("jobId 不属于此批次", "job_not_required", 400)
            if task.status == "running":
                raise VideoServiceError("视频正在制作，不能修改素材", "video_running", 409)
            previous = task.frames.get(jid)
            cache_name = f"{task.original_job_ids.index(jid) + 1:04d}_{frame_filename}"
            cache_path = self._cache_dir(task.batch_id) / cache_name
            if previous is not None:
                if previous.filename != frame_filename or previous.digest != digest:
                    raise VideoServiceError("同一组收到了不同合图", "frame_conflict", 409)
                if task.status != "done" and not self._frame_is_ready(task, jid):
                    raise VideoServiceError("视频合图缓存已损坏，请重新上传", "frame_cache_failed", 500)
                result: dict[str, Any] = {"cached": True, "filename": previous.filename}
                if previous.saved_path:
                    result["path"] = previous.saved_path
                if previous.saved_filename:
                    result["savedFilename"] = previous.saved_filename
                return result
            if task.status == "done":
                raise VideoServiceError("视频已完成，不能修改素材", "video_done", 409)

            self._atomic_cache_write(cache_path, data)
            frame = _Frame(frame_filename, cache_name, digest, len(data))
            if task.save_images:
                if self._image_saver is None:
                    try:
                        cache_path.unlink(missing_ok=True)
                    except OSError:
                        pass
                    raise VideoServiceError("图片保存服务不可用", "image_save_unavailable", 500)
                try:
                    saved = self._image_saver(
                        frame_filename,
                        data,
                        f"video:{task.batch_id}:{jid}",
                        task.output_dir,
                    )
                except VideoServiceError:
                    raise
                except Exception as exc:
                    raise VideoServiceError("合图保存失败", "image_save_failed", 500) from exc
                if not isinstance(saved, dict) or not isinstance(saved.get("path"), str):
                    raise VideoServiceError("合图保存结果无效", "image_save_failed", 500)
                frame.saved_path = saved["path"]
                if isinstance(saved.get("filename"), str):
                    frame.saved_filename = saved["filename"]
            task.frames[jid] = frame
            self._write_manifest(task)
            result = {"cached": True, "filename": frame.filename}
            if frame.saved_path:
                result["path"] = frame.saved_path
            if frame.saved_filename:
                result["savedFilename"] = frame.saved_filename
            return result

    def skip(self, *, batch_id: Any, job_id: Any) -> dict[str, Any]:
        bid = _validate_id(batch_id, "batchId", batch=True)
        jid = _validate_id(job_id, "jobId")
        with self._lock:
            task = self._task_for(bid)
            if task.thread is not None and task.thread.is_alive():
                raise VideoServiceError("上一次视频任务正在停止，请稍后重试。", "video_busy", 409)
            if task.status in {"running", "done"}:
                raise VideoServiceError("当前视频状态不能排除组", "video_not_skippable", 409)
            if jid not in task.original_job_ids:
                raise VideoServiceError("jobId 不属于此批次", "job_not_required", 400)
            if jid not in task.job_ids:
                return self._snapshot(task)
            task.job_ids.remove(jid)
            if task.status in {"error", "cancelled"}:
                task.status = "waiting"
                task.progress = 0
                task.error = None
                task.path = None
            self._write_manifest(task)
            return self._snapshot(task)

    def _missing_frames(self, task: _Task) -> list[str]:
        return [job_id for job_id in task.job_ids if not self._frame_is_ready(task, job_id)]

    def _published_output_path(self, task: _Task) -> Optional[Path]:
        if not task.path:
            return None
        try:
            candidate = Path(task.path).expanduser().resolve()
        except OSError:
            return None
        if not _path_inside(candidate, task.output_dir) or not candidate.is_file():
            return None
        return candidate

    def start(self, *, batch_id: Any) -> dict[str, Any]:
        bid = _validate_id(batch_id, "batchId", batch=True)
        with self._lock:
            task = self._tasks.get(bid)
            if task is None:
                raise VideoServiceError("视频批次不存在", "batch_not_found", 404)
            if task.status == "running":
                return self._snapshot(task)
            if task.status == "done":
                return self._snapshot(task)
            # cancel 会先把状态改为 cancelled，再等待旧线程退出。旧线程尚未
            # 完成时禁止同批次再次 start，避免清除 cancel_event 后并发编码。
            if task.thread is not None and task.thread.is_alive():
                raise VideoServiceError("上一次视频任务正在停止，请稍后重试。", "video_busy", 409)
            # 成品已发布但临时 PNG 清理失败时，只重试清理，不重新编码或要求
            # 浏览器再次上传图片，避免用户得到重复成品。
            published = self._published_output_path(task)
            if published is not None:
                try:
                    self._clean_cache_frames(task)
                except VideoServiceError as cleanup_error:
                    task.status = "error"
                    task.error = cleanup_error.message
                    task.progress = 100
                    self._write_manifest(task)
                    raise cleanup_error
                task.status = "done"
                task.progress = 100
                task.path = str(published)
                task.error = None
                self._write_manifest(task)
                return self._snapshot(task)
            if not task.job_ids:
                task.error = "所有组都已排除，无法制作空视频。"
                self._write_manifest(task)
                raise VideoServiceError(task.error, "empty_batch", 409)
            if self._active_batch_id is not None and self._active_batch_id != bid:
                active = self._tasks.get(self._active_batch_id)
                if active is not None and active.thread is not None and active.thread.is_alive():
                    raise VideoServiceError("已有另一个视频正在制作，请等待完成。", "video_busy", 409)
                self._active_batch_id = None
            missing = self._missing_frames(task)
            if missing:
                task.error = "等待所有合图上传后才能制作视频。"
                self._write_manifest(task)
                raise VideoServiceError(task.error, "missing_frames", 409)
            if not task.output_dir.is_dir():
                raise VideoServiceError("视频保存目录不可用", "invalid_folder", 409)
            if not task.ffmpeg_path.is_file():
                task.ffmpeg_path = self._find_ffmpeg() or task.ffmpeg_path
            if not task.ffmpeg_path.is_file():
                raise VideoServiceError("未找到本机 FFmpeg，请检查 tools/ffmpeg.exe。", "ffmpeg_unavailable", 503)
            if not task.bgm_path.is_file() or not self._probe_audio(task.ffmpeg_path, task.bgm_path):
                raise VideoServiceError("已选择的音轨不可用，请重新准备视频。", "audio_unavailable", 503)
            task.cancel_event.clear()
            task.status = "running"
            task.progress = 0
            task.error = None
            task.path = None
            self._active_batch_id = bid
            self._write_manifest(task)
            worker = threading.Thread(target=self._run_task, args=(task,), name=f"time-echo-video-{bid}", daemon=True)
            task.thread = worker
            worker.start()
            return self._snapshot(task)

    def _update_progress(self, task: _Task, progress: int) -> None:
        with self._lock:
            if task.status != "running":
                return
            value = max(0, min(99, int(progress)))
            if value <= task.progress:
                return
            task.progress = value
            try:
                self._write_manifest(task)
            except VideoServiceError:
                # 状态线程仍应持续运行；最终态会再次尝试保存 manifest。
                pass

    def _run_ffmpeg(self, task: _Task, output: Path) -> None:
        image_paths = [self._cache_dir(task.batch_id) / task.frames[job_id].cache_name for job_id in task.job_ids]
        command = build_ffmpeg_command(task.ffmpeg_path, image_paths, task.bgm_path, output)
        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                **_hidden_subprocess_kwargs(),
            )
        except OSError as exc:
            raise VideoServiceError("无法启动 FFmpeg。", "ffmpeg_start_failed", 500) from exc
        with self._lock:
            task.process = process
        last_error_lines: list[str] = []
        total = VIDEO_SECONDS_PER_IMAGE * len(task.job_ids)
        try:
            if process.stdout is not None:
                for line in process.stdout:
                    if task.cancel_event.is_set():
                        self._terminate_process(task)
                        raise _VideoCancelled()
                    text_line = line.strip()
                    if text_line.startswith("out_time_ms="):
                        try:
                            seconds = int(text_line.split("=", 1)[1]) / 1_000_000
                            self._update_progress(task, int(seconds / total * 100))
                        except (ValueError, ZeroDivisionError):
                            pass
                    elif text_line and len(last_error_lines) < 10:
                        last_error_lines.append(text_line[-500:])
            return_code = process.wait(timeout=10)
        except subprocess.TimeoutExpired as exc:
            self._terminate_process(task)
            raise VideoServiceError("FFmpeg 响应超时。", "ffmpeg_timeout", 500) from exc
        finally:
            with self._lock:
                task.process = None
        if task.cancel_event.is_set():
            raise _VideoCancelled()
        if return_code != 0:
            detail = "；".join(last_error_lines[-3:])
            message = "FFmpeg 导出失败。" if not detail else f"FFmpeg 导出失败：{detail}"
            raise VideoServiceError(message, "ffmpeg_failed", 500)

    def _terminate_process(self, task: _Task) -> None:
        with self._lock:
            process = task.process
        if process is None or process.poll() is not None:
            return
        try:
            process.terminate()
            process.wait(timeout=3)
        except (OSError, subprocess.TimeoutExpired):
            try:
                process.kill()
                process.wait(timeout=3)
            except (OSError, subprocess.TimeoutExpired):
                pass

    def _publish_output(self, task: _Task, temporary: Path) -> Path:
        with self._lock:
            attempt = 1
            while True:
                filename = task.filename if attempt == 1 else _alternate_filename(task.requested_filename, attempt)
                candidate = (task.output_dir / filename).resolve()
                owner = self._reserved_outputs.get(candidate)
                if owner not in {None, task.batch_id} or (owner is None and _is_occupied(candidate)):
                    attempt += 1
                    continue
                self._reserved_outputs[candidate] = task.batch_id
                try:
                    os.link(os.fspath(temporary), os.fspath(candidate))
                except FileExistsError:
                    attempt += 1
                    continue
                except OSError as exc:
                    raise VideoServiceError("成品视频发布失败。", "video_publish_failed", 500) from exc
                task.filename = filename
                task.path = str(candidate)
                return candidate

    def _clean_cache_frames(self, task: _Task) -> None:
        cache_dir = self._cache_dir(task.batch_id)
        failures: list[str] = []
        try:
            if not cache_dir.is_dir() or cache_dir.is_symlink():
                raise OSError("cache directory unavailable")
        except OSError as exc:
            raise VideoServiceError("视频临时图片清理失败。", "cache_cleanup_failed", 500) from exc
        for frame in task.frames.values():
            frame_path = cache_dir / frame.cache_name
            if not _path_inside(frame_path, cache_dir):
                failures.append(frame.cache_name)
                continue
            try:
                frame_path.unlink(missing_ok=True)
            except OSError:
                failures.append(frame.cache_name)
        try:
            for item in cache_dir.iterdir():
                if item.name == "manifest.json":
                    continue
                if item.is_file() or item.is_symlink():
                    try:
                        item.unlink(missing_ok=True)
                    except OSError:
                        failures.append(item.name)
                else:
                    # 服务只创建 PNG 与 manifest；未知目录不递归删除，避免
                    # 误触用户放入缓存目录的内容。
                    failures.append(item.name)
        except OSError:
            failures.append("cache-directory")
        if failures:
            raise VideoServiceError("视频临时图片清理失败，请稍后重试。", "cache_cleanup_failed", 500)

    def _run_task(self, task: _Task) -> None:
        temporary: Optional[Path] = None
        published_path: Optional[Path] = None
        try:
            fd, temp_name = tempfile.mkstemp(
                prefix=f".time-echo-{task.batch_id}-",
                suffix=".mp4",
                dir=task.output_dir,
            )
            os.close(fd)
            temporary = Path(temp_name)
            if task.cancel_event.is_set():
                raise _VideoCancelled()
            if self._export_runner is not None:
                self._export_runner(task, temporary)
            else:
                self._run_ffmpeg(task, temporary)
            if task.cancel_event.is_set():
                raise _VideoCancelled()
            if not temporary.is_file() or temporary.stat().st_size <= 0:
                raise VideoServiceError("FFmpeg 没有生成有效视频。", "ffmpeg_failed", 500)
            final_path = self._publish_output(task, temporary)
            published_path = final_path
            with self._lock:
                task.path = str(final_path)
                task.process = None
                # 成品已用同目录 hard link 发布后，取消请求不能再把本批标成
                # cancelled，否则下次重试会产生第二个成品。此时按已完成处理。
                try:
                    self._clean_cache_frames(task)
                except VideoServiceError as cleanup_error:
                    task.status = "error"
                    task.progress = 100
                    task.error = cleanup_error.message
                    self._write_manifest(task)
                    return
                task.status = "done"
                task.progress = 100
                task.error = None
                self._write_manifest(task)
        except _VideoCancelled:
            with self._lock:
                task.status = "cancelled"
                task.error = "视频制作已取消，合图已保留，可重试。"
                task.process = None
                task.progress = min(task.progress, 99)
                try:
                    self._write_manifest(task)
                except VideoServiceError:
                    pass
        except VideoServiceError as exc:
            with self._lock:
                if task.cancel_event.is_set() and published_path is None:
                    task.status = "cancelled"
                    task.error = "视频制作已取消，合图已保留，可重试。"
                    task.progress = min(task.progress, 99)
                else:
                    task.status = "error"
                    task.error = exc.message
                task.process = None
                try:
                    self._write_manifest(task)
                except VideoServiceError:
                    pass
        except Exception:
            with self._lock:
                if task.cancel_event.is_set() and published_path is None:
                    task.status = "cancelled"
                    task.error = "视频制作已取消，合图已保留，可重试。"
                    task.progress = min(task.progress, 99)
                else:
                    task.status = "error"
                    task.error = "视频制作失败，请重试。"
                task.process = None
                try:
                    self._write_manifest(task)
                except VideoServiceError:
                    pass
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass
            with self._lock:
                task.process = None
                task.thread = None
                if self._active_batch_id == task.batch_id:
                    self._active_batch_id = None

    def cancel(self, *, batch_id: Any) -> dict[str, Any]:
        bid = _validate_id(batch_id, "batchId", batch=True)
        with self._lock:
            task = self._tasks.get(bid)
            if task is None:
                raise VideoServiceError("视频批次不存在", "batch_not_found", 404)
            if task.status == "done":
                return self._snapshot(task)
            task.cancel_event.set()
            task.status = "cancelled"
            task.error = "视频制作已取消，合图已保留，可重试。"
            task.progress = min(task.progress, 99)
            self._write_manifest(task)
        self._terminate_process(task)
        return self._snapshot(task)

    def close(self, timeout: float = 3.0) -> None:
        """服务退出时停止活动导出并保留可重试缓存。"""
        with self._lock:
            active = [
                task for task in self._tasks.values()
                if task.status == "running" or task.process is not None
            ]
            for task in active:
                task.cancel_event.set()
                if task.status == "running":
                    task.status = "cancelled"
                    task.error = "服务已停止，合图已保留，可重试。"
                    task.progress = min(task.progress, 99)
                    try:
                        self._write_manifest(task)
                    except VideoServiceError:
                        pass
        for task in active:
            self._terminate_process(task)
        end_time = time.monotonic() + max(0.0, timeout)
        for task in active:
            thread = task.thread
            if thread is None or thread is threading.current_thread():
                continue
            remaining = end_time - time.monotonic()
            if remaining <= 0:
                break
            thread.join(remaining)

    def discard(self, *, batch_id: Any) -> dict[str, Any]:
        bid = _validate_id(batch_id, "batchId", batch=True)
        with self._lock:
            task = self._tasks.get(bid)
            if task is None:
                raise VideoServiceError("视频批次不存在", "batch_not_found", 404)
            if task.status == "running" or (task.thread is not None and task.thread.is_alive()) or task.process is not None:
                raise VideoServiceError("视频仍在制作，暂时不能清理批次", "video_busy", 409)
            cache_dir = self._cache_dir(task.batch_id).resolve()
            if not _path_inside(cache_dir, self.cache_root) or cache_dir == self.cache_root:
                raise VideoServiceError("视频缓存路径无效", "cache_unavailable", 500)
            self._tasks.pop(task.batch_id, None)
            self._reserved_outputs.pop((task.output_dir / task.filename).resolve(), None)
            try:
                shutil.rmtree(cache_dir)
            except FileNotFoundError:
                pass
            except OSError as exc:
                self._tasks[task.batch_id] = task
                self._reserved_outputs[(task.output_dir / task.filename).resolve()] = task.batch_id
                raise VideoServiceError("视频缓存清理失败", "discard_failed", 500) from exc
            return {"batchId": task.batch_id, "discarded": True}


__all__ = ["VideoService", "VideoServiceError", "build_ffmpeg_command"]
