"""Codex 时光对照工作台：只管理本地素材、审核记录和视频，不调用模型 API。"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime
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
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
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
            if data["status"] != "done":
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
        data["status"] = "done"
        self.save(folder, data)
        return {"status": "done", "path": data["video"], "image_calls": data["image_calls"]}


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
    parser.add_argument("action", choices=["begin", "history", "status", "plan", "attempt", "record", "fail", "replace", "render", "finish", "block"])
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
                          "replace": ["pair", "reason"], "finish": ["file"], "block": ["reason"]}.items():
        if args.action == action and any(getattr(args, n) is None for n in names):
            parser.error("该操作需要 " + ", ".join("--" + n for n in names))
    work = Workspace()
    with exclusive(work.output / ".lock"):
        if args.action == "begin":
            result = work.begin()
        elif args.action == "history":
            result = work.history()
        else:
            folder, data = work.load(args.run)
            if args.action == "status": result = data
            elif args.action == "plan": result = work.plan(folder, data, args.pair, read_json(args.file))
            elif args.action == "attempt": result = work.attempt(folder, data, args.pair, args.kind, args.file.read_text(encoding="utf-8-sig"))
            elif args.action == "record": result = work.record(folder, data, args.attempt, args.image, read_json(args.file))
            elif args.action == "fail": result = work.fail(folder, data, args.attempt, args.reason)
            elif args.action == "replace": result = work.replace(folder, data, args.pair, args.reason)
            elif args.action == "render": result = work.render(folder, data)
            elif args.action == "finish": result = work.finish(folder, data, read_json(args.file))
            else:
                data.update(status="blocked", error=args.reason)
                work.save(folder, data)
                result = {"status": "blocked", "reason": args.reason}
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    try:
        main()
    except (ValueError, OSError, subprocess.TimeoutExpired) as error:
        print(json.dumps({"error": str(error)}, ensure_ascii=False), file=sys.stderr)
        sys.exit(1)
