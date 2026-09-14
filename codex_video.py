"""Codex 时光对照工作台：只管理本地素材、审核记录和视频，不调用模型 API。"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import errno
import hashlib
import json
import os
from pathlib import Path
import random
import subprocess
import sys
import uuid

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

from video_service import AUDIO_EXTENSIONS, build_ffmpeg_command


ROOT = Path(__file__).resolve().parent
CHECKS = {"old": ["realism", "era", "anatomy", "composition", "no_text"],
          "now": ["realism", "era", "anatomy", "composition", "no_text",
                  "same_place", "time_change", "loss"]}
DEFAULTS = {"counts": [5, 6], "max_image_calls": 36, "attempts_per_image": 3,
            "candidates_per_pair": 3, "keep_images": False,
            "output_dir": "outputs/codex-time-echo"}
PUBLISH_CHECKS = ("video", "upload_complete", "description", "short_title", "location",
                  "collection", "link", "activity", "schedule", "original", "ai_label")
PUBLISH_TRANSITIONS = {
    "awaiting_publish": {"blocked"},
    "prepared": {"waiting_login", "uploading", "blocked"},
    "waiting_login": {"waiting_login", "uploading", "blocked"},
    "uploading": {"ready", "waiting_login", "blocked"},
    "ready": {"ready", "submitting", "waiting_login", "uploading", "blocked"},
    "submitting": {"published", "uncertain"},
    "uncertain": {"published", "ready"},
    "blocked": {"waiting_login", "uploading", "blocked"},
    "published": {"delivered"},
    "delivered": set(),
}


class ExecutionBusy(ValueError):
    def __init__(self, execution, message="该批次已有执行者；只查看状态，不修改进度"):
        super().__init__(message)
        self.execution = execution


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def write_json(path, value):
    path = Path(path)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def run_command(command, timeout=30):
    result = subprocess.run(command, capture_output=True, timeout=timeout,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if result.returncode:
        raise ValueError(result.stderr.decode("utf-8", errors="replace")[-2000:])
    return result.stdout


def probe(root, path):
    return json.loads(run_command([str(root / "tools/ffprobe.exe"), "-v", "error",
        "-show_streams", "-show_format", "-of", "json", str(path)]))


def preflight(root):
    for name in ("ffmpeg.exe", "ffprobe.exe"):
        if not (root / "tools" / name).is_file():
            raise ValueError(f"缺少 tools/{name}")
    run_command([str(root / "tools/ffmpeg.exe"), "-version"])
    font_path()
    audio = []
    for path in sorted((root / "bgm").glob("*")):
        if path.is_file() and path.suffix.lower() in AUDIO_EXTENSIONS:
            try:
                if any(s["codec_type"] == "audio" for s in probe(root, path)["streams"]):
                    audio.append(str(path.resolve()))
            except (ValueError, subprocess.TimeoutExpired):
                continue
    if not audio:
        raise ValueError("bgm 中没有可用音乐")
    return audio


def font_path():
    for name in ("msyhbd.ttc", "msyh.ttc", "simhei.ttf"):
        path = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / name
        if path.is_file():
            return path
    raise ValueError("缺少可用于年份标注的中文字体")


@contextmanager
def exclusive(path):
    """操作系统释放锁，避免进程退出后留下无法恢复的锁文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        handle.seek(0, 2)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            if error.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                raise ExecutionBusy(None, "项目正在执行本地命令；不修改进度，也不启动另一批") from None
            raise
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)


class Workspace:
    def __init__(self, root=ROOT):
        self.root = Path(root).resolve()
        self.config = {**DEFAULTS, **read_json(self.root / "codex-video.json")}
        self.output = (self.root / self.config["output_dir"]).resolve()
        # 运行数据保持在项目内，方便定时任务在工作区权限下保存及恢复。
        if not self.output.is_relative_to(self.root) or self.output == self.root:
            raise ValueError("output_dir 必须是项目内的独立子目录")
        if not self.config["counts"] or any(type(n) is not int or n not in (5, 6) for n in self.config["counts"]):
            raise ValueError("counts 只能包含 5、6")
        for name in ("max_image_calls", "attempts_per_image", "candidates_per_pair"):
            if type(self.config[name]) is not int or self.config[name] < 1:
                raise ValueError(f"{name} 必须是正整数")
        if type(self.config["keep_images"]) is not bool:
            raise ValueError("keep_images 必须是布尔值")
        self.output.mkdir(parents=True, exist_ok=True)

    def execution(self):
        path = self.output / "execution.json"
        return read_json(path) if path.exists() else None

    def save_execution(self, execution):
        execution["updated_at"] = datetime.now(timezone.utc).isoformat()
        execution["revision"] = execution.get("revision", 0) + 1
        write_json(self.output / "execution.json", execution)

    def require_execution(self, owner, token, run_id):
        execution = self.execution()
        if (not execution or execution["state"] != "held"
                or execution["owner_thread_id"] != owner
                or execution["token"] != token or execution["run_id"] != run_id):
            raise ExecutionBusy(execution, "未持有当前批次的执行凭证；先 begin，其他执行者不得修改进度")
        return execution

    def claim(self, owner, run_id, previous=None):
        execution = {"owner_thread_id": owner, "run_id": run_id,
                     "token": uuid.uuid4().hex, "state": "held", "revision": 0}
        if previous:
            execution["previous_execution"] = previous
        self.save_execution(execution)
        return execution

    def takeover(self, owner, run_id, proof):
        execution = self.execution()
        if not execution or execution["state"] != "held" or execution["run_id"] != run_id:
            raise ValueError("占用已变化，请重新 begin")
        # 核验由 Codex 状态工具完成；脚本校验凭据和版本，不能靠超时猜测原任务已停止。
        if not isinstance(proof, dict) or any(proof.get(k) != execution[k]
                for k in ("owner_thread_id", "token", "revision")):
            raise ExecutionBusy(execution, "占用记录已变化，必须重新核实原执行者状态")
        if (proof.get("observed_status") not in ("idle", "completed", "interrupted")
                or proof.get("source") not in ("wait_threads", "read_thread")
                or not isinstance(proof.get("evidence"), str) or not proof["evidence"].strip()):
            raise ExecutionBusy(execution, "尚未确认原任务停止；运行中、等待工具或状态未知时禁止接管")
        try:
            checked = datetime.fromisoformat(proof["checked_at"])
            age = (datetime.now(timezone.utc) - checked).total_seconds()
        except (KeyError, TypeError, ValueError):
            raise ValueError("核验时间必须包含时区") from None
        if not 0 <= age <= 60:
            raise ExecutionBusy(execution, "状态核验已过期，必须重新核实原执行者状态")
        folder, data = self.load(run_id)
        previous = {**execution, "stop_check": proof}
        # 只保留上一次占用，避免接管记录无限嵌套。
        previous.pop("previous_execution", None)
        claimed = self.claim(owner, run_id, previous)
        return {"resumed": True, "folder": str(folder), **data, "execution": claimed}

    def execute(self, action, *, owner=None, token=None, run_id=None, pair=None,
                kind=None, attempt=None, file=None, image=None, reason=None):
        """技能命令唯一入口：在同一文件锁内校验占用，再读写批次。"""
        with exclusive(self.output / ".lock"):
            if action == "history":
                return self.history()
            if action == "ownership":
                return {"execution": self.execution()}
            if action == "status":
                return {**self.load(run_id)[1], "execution": self.execution()}
            if action == "publish-status":
                return self.publication(self.load(run_id)[0])
            if not isinstance(owner, str) or not owner.strip():
                raise ValueError("缺少执行者身份；在 Codex 任务中运行，或通过 --owner 指定真实任务 ID")
            if action == "begin":
                execution = self.execution()
                if execution and execution["state"] == "held":
                    if execution["owner_thread_id"] != owner or execution["token"] != token:
                        raise ExecutionBusy(execution)
                    folder, data = self.load(execution["run_id"])
                    self.save_execution(execution)
                    return {"resumed": True, "folder": str(folder), **data, "execution": execution}
                result = self.begin()
                return {**result, "execution": self.claim(owner, result["run_id"])}
            if action == "takeover":
                return self.takeover(owner, run_id, read_json(file))
            execution = self.require_execution(owner, token, run_id)
            folder, data = self.load(run_id)
            if action == "release":
                if any(a["status"] == "pending" for p in data["pairs"] for a in p["attempts"]):
                    raise ValueError("仍有未登记的生图请求；确认结果或中断并登记后才能释放")
                execution.update(state="released", release_reason=reason)
                self.save_execution(execution)
                return {"status": "released", "run_id": run_id}
            # 开始操作就递增版本，失败的尝试也使先前的接管核验失效。
            self.save_execution(execution)
            if action == "plan": return self.plan(folder, data, pair, read_json(file))
            if action == "attempt": return self.attempt(folder, data, pair, kind, Path(file).read_text(encoding="utf-8-sig"))
            if action == "record": return self.record(folder, data, attempt, image, read_json(file))
            if action == "fail": return self.fail(folder, data, attempt, reason)
            if action == "replace": return self.replace(folder, data, pair, reason)
            if action == "render": return self.render(folder, data)
            if action == "finish": return self.finish(folder, data, read_json(file))
            if action == "publish-init": return self.prepare_publication(folder, data, read_json(file))
            if action == "publish-step": return self.publication_step(folder, data, read_json(file))
            if action == "block":
                data.update(status="blocked", error=reason)
                self.save(folder, data)
                return {"status": "blocked", "reason": reason}
            raise ValueError("未知操作")

    def load(self, run_id):
        if not run_id or any(c not in "0123456789abcdefghijklmnopqrstuvwxyz_-" for c in run_id):
            raise ValueError("批次编号无效")
        folder = self.output / run_id
        data = read_json(folder / "manifest.json")
        return folder, data

    def save(self, folder, data):
        data["updated_at"] = datetime.now().astimezone().isoformat()
        write_json(folder / "manifest.json", data)

    def begin(self):
        active = self.output / "active.json"
        if active.exists():
            folder, data = self.load(read_json(active)["run_id"])
            publication = self.publication(folder)
            if data["status"] != "done" or publication["status"] not in ("not_started", "delivered"):
                return {"resumed": True, "folder": str(folder), **data}
        audio = preflight(self.root)
        run_id = datetime.now().strftime("%Y%m%d_%H%M%S_") + uuid.uuid4().hex[:6]
        folder = self.output / run_id
        folder.mkdir()
        (folder / "assets").mkdir()
        new_year = random.choice([datetime.now().year - 1, datetime.now().year])
        count = random.choice(self.config["counts"])
        pairs = [{"index": i + 1, "old_year": random.randint(2000, new_year - 10),
                  "new_year": new_year, "candidate": 1, "plan": None,
                  "attempts": [], "old": None, "now": None} for i in range(count)]
        data = {"run_id": run_id, "status": "working", "count": count,
                "config": self.config, "bgm": random.choice(audio), "image_calls": 0,
                "pairs": pairs, "events": [], "video": None}
        self.save(folder, data)
        write_json(active, {"run_id": run_id})
        return {"resumed": False, "folder": str(folder), **data}

    def history(self):
        rows = []
        for path in sorted(self.output.glob("*/manifest.json"))[-40:]:
            data = read_json(path)
            rows.append({"run_id": data["run_id"], "status": data["status"],
                         "plans": [p["plan"] for p in data["pairs"] if p["plan"]]})
        return rows

    def pair(self, data, index):
        if not 1 <= index <= data["count"]:
            raise ValueError("组号超出范围")
        return data["pairs"][index - 1]

    def plan(self, folder, data, index, plan):
        pair = self.pair(data, index)
        if pair["plan"] or pair["attempts"]:
            raise ValueError("已开始的选题不能改写；换题请使用 replace")
        for key in ("title", "old_life", "loss", "change_route", "caption"):
            if not isinstance(plan.get(key), str) or not plan[key].strip():
                raise ValueError(f"选题缺少 {key}")
        for key, minimum in (("anchors", 2), ("changes", 3)):
            if not isinstance(plan.get(key), list) or len(plan[key]) < minimum or not all(isinstance(v, str) and v.strip() for v in plan[key]):
                raise ValueError(f"选题 {key} 至少需要 {minimum} 项")
        pair["plan"] = plan
        self.save(folder, data)
        return pair

    def attempt(self, folder, data, index, kind, prompt):
        pair = self.pair(data, index)
        if data["status"] in ("done", "rendered"):
            raise ValueError("已成片批次不能继续生图")
        if not pair["plan"] or not prompt.strip():
            raise ValueError("先保存选题与完整提示词")
        if pair[kind]:
            raise ValueError("该图片已合格，不需要重新调用生图")
        if kind == "now" and not pair["old"]:
            raise ValueError("旧照尚未审核合格")
        if data["image_calls"] >= data["config"]["max_image_calls"]:
            raise ValueError("本轮已达到生图调用上限；保留进度并报告，不自动重置预算")
        if any(a["status"] == "pending" for p in data["pairs"] for a in p["attempts"]):
            raise ValueError("本轮仍有未登记结果的请求，请先恢复或标记失败；生图串行执行")
        attempts = [a for a in pair["attempts"] if a["kind"] == kind]
        if any(a["status"] == "pending" for a in attempts):
            raise ValueError("有未登记结果的请求，先恢复图片或用 fail 标记中断")
        if len(attempts) >= data["config"]["attempts_per_image"]:
            raise ValueError("该图重做次数已用尽，请换选题")
        data["image_calls"] += 1
        record = {"id": data["image_calls"], "kind": kind, "prompt": prompt,
                  "status": "pending", "reference_sha256": pair["old"]["sha256"] if kind == "now" else None}
        pair["attempts"].append(record)
        data["status"] = "working"
        self.save(folder, data)
        return record

    def find_attempt(self, data, attempt_id):
        for pair in data["pairs"]:
            for attempt in pair["attempts"]:
                if attempt["id"] == attempt_id:
                    return pair, attempt
        raise ValueError("找不到这个请求")

    def record(self, folder, data, attempt_id, source, review):
        pair, attempt = self.find_attempt(data, attempt_id)
        if attempt["status"] != "pending":
            raise ValueError("请求已登记，不能覆盖")
        kind = attempt["kind"]
        if type(review.get("passed")) is not bool or not isinstance(review.get("checks"), dict):
            raise ValueError("审核需要 passed 布尔值和 checks 对象")
        if not isinstance(review.get("observations"), str) or not review["observations"].strip():
            raise ValueError("必须写下实际看图观察，不得只填写通过")
        for key in CHECKS[kind]:
            if type(review["checks"].get(key)) is not bool:
                raise ValueError(f"缺少审核项 {key}")
        if review["passed"] and not all(review["checks"][k] for k in CHECKS[kind]):
            raise ValueError("存在未通过项，不能标记合格")
        if kind == "now" and review["passed"]:
            details = review.get("visible_changes")
            if not isinstance(details, list) or len(details) < 3 or not all(isinstance(v, str) and v.strip() for v in details):
                raise ValueError("今照需至少三项实际可见的跨年变化证据")
            if not isinstance(review.get("loss_evidence"), str) or not review["loss_evidence"].strip():
                raise ValueError("今照需说明失去的生活细节")
        target = folder / "assets" / f"{pair['index']:02d}-{kind}-{attempt_id:02d}.png"
        with Image.open(source) as original:
            original.load()
            if min(original.size) < 640:
                raise ValueError("图片短边不足 640 像素")
            ImageOps.exif_transpose(original).convert("RGB").save(target)
        asset = {"path": str(target), "sha256": digest(target), "review": review}
        attempt.update({"status": "accepted" if review["passed"] else "rejected", **asset})
        if review["passed"]:
            pair[kind] = asset
        self.save(folder, data)
        return attempt

    def fail(self, folder, data, attempt_id, reason):
        _, attempt = self.find_attempt(data, attempt_id)
        if attempt["status"] != "pending":
            raise ValueError("请求已登记")
        attempt.update(status="failed", reason=reason)
        self.save(folder, data)
        return attempt

    def replace(self, folder, data, index, reason):
        pair = self.pair(data, index)
        if data["status"] in ("done", "rendered") or (pair["old"] and pair["now"]):
            raise ValueError("已合格的整组不需要换题")
        if any(a["status"] == "pending" for a in pair["attempts"]):
            raise ValueError("先处理尚未登记结果的请求")
        if pair["candidate"] >= data["config"]["candidates_per_pair"]:
            raise ValueError("本组换题次数已用尽，保存进度并报告")
        data["events"].append({"event": "replace", "reason": reason, "pair": json.loads(json.dumps(pair))})
        pair.update(candidate=pair["candidate"] + 1, plan=None, attempts=[], old=None, now=None)
        self.save(folder, data)
        return pair

    def ready_frames(self, folder, data):
        if len(data["pairs"]) != data["count"]:
            raise ValueError("组数不完整")
        frames = []
        for pair in data["pairs"]:
            for kind in ("old", "now"):
                asset = pair[kind]
                if not asset or not asset["review"]["passed"]:
                    raise ValueError(f"第 {pair['index']} 组 {kind} 尚未审核合格")
                if digest(asset["path"]) != asset["sha256"]:
                    raise ValueError("图片在审核后被修改，请重新审核")
            target = folder / "assets" / f"{pair['index']:02d}-stacked.png"
            compose(pair["old"]["path"], pair["now"]["path"], pair["old_year"], pair["new_year"], target)
            frames.append(target)
        return frames

    def render(self, folder, data):
        target = folder / "时光对照.mp4"
        if target.exists():
            info = validate_video(self.root, target, data["count"])
            if not (folder / "preview.jpg").is_file():
                make_preview(self.root, target, data["count"], folder / "preview.jpg")
            data.update(status="rendered" if data["status"] != "done" else "done", video=str(target), video_info=info)
            self.save(folder, data)
            return {"path": str(target), **info}
        frames = self.ready_frames(folder, data)
        temporary = folder / "encoding.partial.mp4"
        data["status"] = "rendering"
        self.save(folder, data)
        command = build_ffmpeg_command(self.root / "tools/ffmpeg.exe", frames, data["bgm"], temporary)
        try:
            run_command(command, timeout=900)
            info = validate_video(self.root, temporary, data["count"])
            temporary.rename(target)
            data.update(status="rendered", video=str(target), video_info=info)
            make_preview(self.root, target, data["count"], folder / "preview.jpg")
            self.save(folder, data)
        except Exception as error:
            data.update(status="blocked", error=str(error))
            self.save(folder, data)
            raise
        return {"path": str(target), "preview": str(folder / "preview.jpg"), **info}

    def finish(self, folder, data, review):
        if data["status"] not in ("rendered", "finishing", "done"):
            raise ValueError("先导出并检查成片")
        if not all(review.get(k) is True for k in ("framing", "years", "order", "transitions")) or not review.get("observations"):
            raise ValueError("成片检查须确认 framing/years/order/transitions 并记录观察")
        validate_video(self.root, data["video"], data["count"])
        data.update(status="finishing", final_review=review)
        self.save(folder, data)
        if not data["config"]["keep_images"]:
            asset_folder = folder / "assets"
            assets = asset_folder.resolve()
            if not assets.is_relative_to(folder.resolve()) or asset_folder.is_symlink() or asset_folder.is_junction():
                raise ValueError("素材目录位置异常，不清理")
            for path in assets.iterdir():
                if path.is_file() and not path.is_symlink() and path.suffix == ".png":
                    path.unlink()
            data["images_cleared"] = True
        if self.publication(folder)["status"] == "not_started":
            write_json(folder / "publish-progress.json", {"run_id": data["run_id"],
                "status": "awaiting_publish", "events": []})
        data["status"] = "done"
        self.save(folder, data)
        return {"status": "done", "path": data["video"], "image_calls": data["image_calls"]}

    def publication(self, folder):
        path = folder / "publish-progress.json"
        return read_json(path) if path.exists() else {"status": "not_started"}

    def prepare_publication(self, folder, data, info):
        if data["status"] != "done":
            raise ValueError("先完成视频审核与 finish，再准备发布")
        if info.get("run_id") != data["run_id"] or Path(info.get("video", "")).resolve() != Path(data["video"]).resolve():
            raise ValueError("发布资料必须对应本批次实际成片")
        for key in ("short_title", "description"):
            if not isinstance(info.get(key), str) or not info[key].strip():
                raise ValueError(f"发布资料缺少 {key}")
        if not isinstance(info.get("hashtags"), list) or not all(isinstance(t, str) for t in info["hashtags"]):
            raise ValueError("hashtags 必须是字符串数组")
        expected = {"location": "不显示位置", "collection": "旧时光", "link": None,
                    "activity": "不参与活动", "schedule": "不定时", "original_requested": True,
                    "ai_label": "内容为虚构剧情，仅供娱乐"}
        if info.get("settings") != expected:
            raise ValueError("发布设置与本项目用户指定的截图不一致")
        info = {k: info[k] for k in ("run_id", "video", "short_title", "description", "hashtags", "settings")}
        video_hash = digest(data["video"])
        progress = self.publication(folder)
        if "info" in progress:
            if progress["video_sha256"] != video_hash:
                raise ValueError("已登记的视频发生变化，不能覆盖原发布记录")
            if progress["info"] == info:
                return progress
            if any(e["status"] == "submitting" for e in progress["events"]):
                raise ValueError("已经尝试提交，不能覆盖原发布资料")
        if progress["status"] not in ("not_started", "awaiting_publish", "prepared", "waiting_login", "uploading", "ready", "blocked"):
            raise ValueError("当前发布状态不允许初始化")
        progress.update(run_id=data["run_id"], status="prepared", info=info, video_sha256=video_hash)
        progress.setdefault("events", []).append({"status": "prepared", "at": datetime.now(timezone.utc).isoformat()})
        write_json(folder / "publish-progress.json", progress)
        return progress

    def publication_step(self, folder, data, event):
        progress = self.publication(folder)
        previous = progress["status"]
        target = event.get("status")
        if target not in PUBLISH_TRANSITIONS.get(previous, set()):
            raise ValueError(f"不能从 {previous} 转到 {target}；提交中或结果不确定时先核实，不能重复发布")
        if not isinstance(event.get("evidence"), str) or not event["evidence"].strip():
            raise ValueError("必须记录实际页面观察或真实阻塞原因")
        if target != "blocked" and "info" not in progress:
            raise ValueError("先 publish-init 保存发布资料")
        if target in ("ready", "submitting", "published"):
            if digest(data["video"]) != progress["video_sha256"]:
                raise ValueError("成片已变化，不能沿用原发布记录")
        if target == "ready":
            if not isinstance(event.get("account"), str) or not event["account"].strip():
                raise ValueError("必须记录实际登录的视频号名称")
            if not isinstance(event.get("checks"), dict) or not all(event["checks"].get(k) is True for k in PUBLISH_CHECKS):
                raise ValueError("发布前必须核对视频、上传完成、文案及截图中的全部设置")
            if previous == "uncertain" and event.get("confirmed_not_submitted") is not True:
                raise ValueError("结果不确定时，必须先取得未提交成功的明确证据")
        if target == "published":
            if not isinstance(event.get("receipt"), str) or not event["receipt"].strip():
                raise ValueError("必须记录平台发表成功提示或匹配本条视频的作品记录")
        if target == "delivered":
            receipts = event.get("notification_receipts")
            if not isinstance(receipts, dict):
                raise ValueError("必须记录成片、视频和发布结果的通知回执")
            for key in ("complete", "video", "published"):
                receipt = receipts.get(key, {})
                if not isinstance(receipt, dict):
                    raise ValueError("通知回执结构无效")
                if key == "video" and receipt.get("status") == "skipped_oversize" and receipt.get("reason"):
                    continue
                if receipt.get("status") not in ("sent", "already_sent") or type(receipt.get("message_id")) is not int:
                    raise ValueError("通知尚未确认成功，保留已发布状态，下次仅补通知")
        # 点击发布之前先落盘 submitting；中断后该状态只能走核验，不能直接再点。
        progress["status"] = target
        progress["events"].append({**event, "at": datetime.now(timezone.utc).isoformat()})
        write_json(folder / "publish-progress.json", progress)
        return progress


def compose(old_path, now_path, old_year, new_year, target):
    canvas = Image.new("RGB", (1080, 1920))
    for image_path, y in ((old_path, 0), (now_path, 960)):
        with Image.open(image_path) as source:
            source = ImageOps.exif_transpose(source).convert("RGB")
            background = ImageOps.fit(source, (1080, 960)).filter(ImageFilter.GaussianBlur(28))
            background = Image.blend(Image.new("RGB", (1080, 960), "#282a27"), background, .45)
            foreground = ImageOps.contain(source, (1080, 960), Image.Resampling.LANCZOS)
            background.paste(foreground, ((1080 - foreground.width) // 2, (960 - foreground.height) // 2))
            canvas.paste(background, (0, y))
    overlay = Image.new("RGBA", canvas.size)
    draw = ImageDraw.Draw(overlay)
    font = ImageFont.truetype(str(font_path()), 54)
    for year, y in ((old_year, 880), (new_year, 984)):
        text = f"{year}年"
        width = draw.textlength(text, font=font)
        draw.rectangle(((1080-width)/2-14, y-8, (1080+width)/2+14, y+66), fill=(0,0,0,122))
        draw.text((540, y), text, font=font, anchor="mt", fill="white", stroke_width=2, stroke_fill="black")
    Image.alpha_composite(canvas.convert("RGBA"), overlay).convert("RGB").save(target)


def validate_video(root, path, count):
    data = probe(root, path)
    video = next((s for s in data["streams"] if s["codec_type"] == "video"), {})
    audio = next((s for s in data["streams"] if s["codec_type"] == "audio"), {})
    duration = float(data["format"]["duration"])
    if (video.get("width"), video.get("height")) != (1080, 1920) or not audio:
        raise ValueError("成片画幅或音轨不符合要求")
    if video.get("r_frame_rate") != "30/1" or abs(duration - count * 4) > .25:
        raise ValueError("成片帧率或时长不符合要求")
    return {"width": 1080, "height": 1920, "fps": 30, "duration": duration, "audio": True}


def make_preview(root, video, count, target):
    run_command([str(root / "tools/ffmpeg.exe"), "-y", "-v", "error", "-ss", "2", "-i", str(video),
        "-vf", f"fps=1/4,scale=270:480,tile={count}x1:nb_frames={count}", "-frames:v", "1", str(target)])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["begin", "history", "status", "ownership", "takeover", "release", "plan", "attempt", "record", "fail", "replace", "render", "finish", "block", "publish-status", "publish-init", "publish-step"])
    parser.add_argument("--owner", help="仅在没有 CODEX_THREAD_ID 时指定真实任务 ID")
    parser.add_argument("--token", help="begin 或 takeover 返回的本轮执行凭证")
    parser.add_argument("--run")
    parser.add_argument("--pair", type=int)
    parser.add_argument("--kind", choices=["old", "now"])
    parser.add_argument("--attempt", type=int)
    parser.add_argument("--file", type=Path)
    parser.add_argument("--image", type=Path)
    parser.add_argument("--reason")
    args = parser.parse_args()
    for action, names in {"plan": ["pair", "file"], "attempt": ["pair", "kind", "file"],
                          "record": ["attempt", "image", "file"], "fail": ["attempt", "reason"],
                          "replace": ["pair", "reason"], "finish": ["file"], "block": ["reason"],
                          "takeover": ["run", "file"], "release": ["run", "reason"],
                          "publish-init": ["run", "file"], "publish-step": ["run", "file"]}.items():
        if args.action == action and any(getattr(args, n) is None for n in names):
            parser.error("该操作需要 " + ", ".join("--" + n for n in names))
    work = Workspace()
    owner = os.environ.get("CODEX_THREAD_ID") or args.owner
    if args.owner and owner != args.owner:
        raise ValueError("--owner 与当前 Codex 任务不一致，不得冒用其他执行者身份")
    result = work.execute(args.action, owner=owner, token=args.token, run_id=args.run,
                          pair=args.pair, kind=args.kind, attempt=args.attempt,
                          file=args.file, image=args.image, reason=args.reason)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    try:
        main()
    except ExecutionBusy as error:
        print(json.dumps({"status": "busy", "error": str(error), "execution": error.execution}, ensure_ascii=False), file=sys.stderr)
        sys.exit(2)
    except (ValueError, OSError, subprocess.TimeoutExpired) as error:
        print(json.dumps({"error": str(error)}, ensure_ascii=False), file=sys.stderr)
        sys.exit(1)
