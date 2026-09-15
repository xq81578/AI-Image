"""Strict social-auto-upload Tencent integration; no browser/network work on import."""
from __future__ import annotations
import argparse
import asyncio
import hashlib
from importlib.metadata import PackageNotFoundError, version as package_version
import json
import os
from pathlib import Path
import subprocess
import sys
import types

from codex_video import ROOT, Workspace, PUBLISH_CHECKS, digest, validate_publish_copy
from publish_video import (PublicationState, PublishError, resolve_owner, notify_qr,
                           cleanup_login_files, terminal_publication_result)

UPSTREAM_SHA = "0012d2c355f88f683cc38dde2a2db209e14091bc"
PATCHRIGHT_VERSION = "1.62.3"
UPSTREAM = ROOT / "social-auto-upload"


def load_tencent():
    try:
        installed_patchright = package_version("patchright")
    except PackageNotFoundError as exc:
        raise RuntimeError("Patchright is not installed in .venv-social") from exc
    if installed_patchright != PATCHRIGHT_VERSION:
        raise RuntimeError(
            f"Patchright version mismatch: expected {PATCHRIGHT_VERSION}, got {installed_patchright}"
        )
    sha = subprocess.check_output(["git", "-C", str(UPSTREAM), "rev-parse", "HEAD"], text=True).strip()
    if sha != UPSTREAM_SHA:
        raise RuntimeError("social-auto-upload SHA mismatch; reinstall pinned revision")
    lock = json.loads((ROOT / "social-upload-version.json").read_text(encoding="utf-8"))
    for relative, expected in lock["patched_files"].items():
        if hashlib.sha256((UPSTREAM / relative).read_bytes().replace(b"\r\n", b"\n")).hexdigest() != expected:
            raise RuntimeError(f"Upstream patch mismatch: {relative}")
    edge = Path("C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe")
    if not edge.is_file():
        raise RuntimeError("Configured Edge executable missing")
    conf = types.ModuleType("conf")
    conf.BASE_DIR = UPSTREAM
    conf.LOCAL_CHROME_PATH = str(edge)
    conf.LOCAL_CHROME_HEADLESS = False
    conf.DEBUG_MODE = False
    sys.modules["conf"] = conf
    sys.path.insert(0, str(UPSTREAM))
    from uploader.tencent_uploader import main
    if not hasattr(main.TencentBaseUploader.submit_publish, "__call__"):
        raise RuntimeError("Tencent integration unavailable")
    return main


class QRCallback:
    def __init__(self, state, qr_folder=None):
        self.state = state
        self.qr_folder = (qr_folder or state.folder).resolve()

    def __call__(self, payload):
        image = Path(payload["image_path"]).resolve()
        # Upstream creates images beside this batch's account file only.
        if image.parent != self.qr_folder or not image.name.startswith("social-account_tencent_login_qrcode_") or image.suffix != '.png':
            raise PublishError("QR_INVALID", "二维码路径不属于本批次")
        class ImageCapture:
            def capture_qr(self, target):
                from PIL import Image
                with Image.open(image) as source:
                    source.verify()
                target.write_bytes(image.read_bytes())
                return "social-auto-upload:qrcode_callback"
        try:
            return notify_qr(self.state, ImageCapture(), self.state.progress())
        finally:
            image.unlink(missing_ok=True)
            cleanup_login_files(self.state.folder)


class StrictForm:
    """Async form verification on the actual upstream upload page, failing closed."""
    LOCATION_VALUE = ".position-display .location-name, .position-display .not-display"
    COLLECTION_VALUE = ".post-album-display .display-text, .post-album-display .collection-text"

    def __init__(self, page):
        self.page = page

    async def unique(self, selector, timeout=10000):
        item = self.page.locator(selector)
        deadline = asyncio.get_running_loop().time() + timeout / 1000
        while True:
            visible = []
            for index in range(await item.count()):
                candidate = item.nth(index)
                if await candidate.is_visible():
                    visible.append(candidate)
            if len(visible) == 1:
                return visible[0]
            if asyncio.get_running_loop().time() >= deadline:
                raise PublishError("FORM_CONTROL_NOT_FOUND", selector)
            await self.page.wait_for_timeout(200)

    async def text(self, selector):
        return (await (await self.unique(selector)).inner_text()).strip()

    @staticmethod
    async def visible(locator):
        for index in range(await locator.count()):
            if await locator.nth(index).is_visible():
                return True
        return False

    @staticmethod
    async def button_ready(button):
        if await button.count() != 1 or not await button.is_visible() or not await button.is_enabled():
            return False
        classes = await button.get_attribute("class")
        return not classes or "weui-desktop-btn_disabled" not in classes

    async def wait_button_ready(self, button, timeout=5000):
        deadline = asyncio.get_running_loop().time() + timeout / 1000
        while not await self.button_ready(button):
            if asyncio.get_running_loop().time() >= deadline:
                raise PublishError("FORM_VALUE_MISMATCH", "声明原创确认按钮未启用")
            await self.page.wait_for_timeout(100)

    async def wait_hidden(self, locator, timeout=5000):
        deadline = asyncio.get_running_loop().time() + timeout / 1000
        while await locator.count() and await locator.is_visible():
            if asyncio.get_running_loop().time() >= deadline:
                raise PublishError("FORM_VALUE_MISMATCH", "声明原创二次确认窗口未关闭")
            await self.page.wait_for_timeout(100)

    async def select(self, display, trigger, options, value):
        if await self.text(display) != value:
            await (await self.unique(trigger)).click()
            candidates = self.page.locator(options)
            matches = []
            for item in await candidates.all():
                if await item.is_visible() and value in (await item.inner_text()).strip().splitlines():
                    matches.append(item)
            if len(matches) != 1:
                raise PublishError("FORM_OPTION_NOT_FOUND", value)
            await matches[0].click()
        if await self.text(display) != value:
            raise PublishError("FORM_VALUE_MISMATCH", value)

    async def verify(self, info):
        s = info["settings"]
        try:
            validate_publish_copy(info["short_title"], info["description"])
        except ValueError as exc:
            raise PublishError("INVALID_PUBLISH_COPY", str(exc)) from exc
        description = info["description"].strip() + ("\n" + " ".join("#" + t.lstrip("#") for t in info["hashtags"]) if info["hashtags"] else "")
        desc = await self.unique('[contenteditable][data-placeholder="添加描述"]')
        await desc.fill(description)
        title = await self.unique('[placeholder="填写短标题有机会获得更多流量"]')
        await title.fill(info["short_title"])
        for display, trigger, options, value in (
            (self.LOCATION_VALUE, ".position-display", ".location-filter-wrap .option-item", s["location"]),
            (self.COLLECTION_VALUE, ".post-album-display", ".post-album-wrap .filter-wrap .option-item", s["collection"]),
            (".activity-display", ".activity-display", ".activity-filter-wrap .option-item", s["activity"]),
            (".mark-tag-select .select-value", ".mark-tag-select .select-display", ".mark-tag-option .option-main", s["ai_label"]),
        ):
            await self.select(display, trigger, options, value)
        if s["link"] is not None or await self.text(".link-display-wrap") != "选择链接":
            raise PublishError("FORM_VALUE_MISMATCH", "必须无链接")
        rows = self.page.locator(".form-item").filter(has=self.page.get_by_text("定时发表", exact=True))
        radio = rows.locator("label").filter(has_text=s["schedule"]).locator('input[type="radio"]')
        if await radio.count() != 1:
            raise PublishError("FORM_CONTROL_NOT_FOUND", "不定时单选框")
        await radio.check()
        original = self.page.locator('.declare-original-checkbox input[type="checkbox"]')
        if await original.count() != 1 or not s["original_requested"]:
            raise PublishError("FORM_CONTROL_NOT_FOUND", "原创声明")
        await original.check()
        dialog = await self.unique('.declare-original-dialog .weui-desktop-dialog__wrp', timeout=5000)
        agreement = dialog.locator('input[type="checkbox"]')
        if await agreement.count() != 1:
            raise PublishError("FORM_CONTROL_NOT_FOUND", "原创声明同意复选框")
        await agreement.check()
        if not await agreement.is_checked():
            raise PublishError("FORM_VALUE_MISMATCH", "原创声明协议未勾选")
        confirm = dialog.get_by_role("button", name="声明原创", exact=True)
        await self.wait_button_ready(confirm)
        await confirm.click()
        await self.wait_hidden(dialog)
        await self.page.wait_for_timeout(300)
        title_error = self.page.get_by_text("标题包含特殊字符", exact=False)
        if await self.visible(title_error):
            raise PublishError("TITLE_VALIDATION_FAILED", "页面提示标题包含特殊字符")
        publish_button = self.page.get_by_role("button", name="发表", exact=True)
        if not await self.button_ready(publish_button):
            raise PublishError("FORM_NOT_READY", "发表按钮仍处于禁用状态")
        checks = {"description": (await desc.inner_text()).strip() == description,
                  "short_title": await title.input_value() == info["short_title"],
                  "schedule": await radio.is_checked(), "original": await original.is_checked()}
        checks.update({
            "location": await self.text(self.LOCATION_VALUE) == s['location'],
            "collection": await self.text(self.COLLECTION_VALUE) == s['collection'],
            "link": await self.text('.link-display-wrap') == '选择链接',
            "activity": await self.text('.activity-display') == s['activity'],
            "ai_label": await self.text('.mark-tag-select .select-value') == s['ai_label'],
        })
        if not all(checks.values()):
            raise PublishError("FORM_VALUE_MISMATCH", "文案或固定设置核验失败")
        account = await self.text(".account-info > .name")
        return account, checks


def account_path(state):
    # All batches of a workspace share one login, outside per-run artifacts.
    folder = state.work.root / '.social-upload-account'
    folder.mkdir(parents=True, exist_ok=True)
    return folder / 'social-account.json'


async def publish(args, state, module=None, form_type=StrictForm):
    progress = state.prepare()
    terminal = terminal_publication_result(args, progress)
    if terminal:
        return terminal
    info = progress["info"]
    try:
        module = module or load_tencent()
        state.verify_ownership()
        account_file = account_path(state)
        result = await asyncio.wait_for(module.tencent_setup(str(account_file), handle=True,
            return_detail=True, qrcode_callback=QRCallback(state, account_file.parent), headless=False), args.login_timeout)
        if not isinstance(result, dict) or result.get("success") is not True:
            message = result.get("message", "登录失败") if isinstance(result, dict) else '腾讯登录返回了非字典结果'
            raise PublishError("LOGIN_FAILED", str(message))
        state.verify_ownership()
        if state.progress()["status"] != "uploading":
            state.step("uploading", "Tencent setup 已确认登录，开始上游视频上传")
        uploader = module.TencentVideo(title=info["short_title"], file_path=info["video"],
            tags=info["hashtags"], publish_date=0, account_file=str(account_file),
            desc=info["description"], short_title=info["short_title"],
            collection_name=info["settings"]["collection"], headless=False, debug=False)
        uploaded = False
        completed = False
        original_upload = uploader.upload_video_file
        original_wait = uploader.wait_for_upload_complete
        async def upload_file(page, file_path):
            nonlocal uploaded
            state.verify_ownership()
            if Path(file_path).resolve() != Path(info["video"]).resolve() or digest(file_path) != progress["video_sha256"]:
                raise PublishError("VIDEO_CHANGED", "成片不一致")
            await original_upload(page, file_path)
            uploaded = True
        async def wait_complete(page):
            nonlocal completed
            await asyncio.wait_for(original_wait(page), args.upload_timeout)
            completed = True
        uploader.upload_video_file = upload_file
        uploader.wait_for_upload_complete = wait_complete
        async def before_submit(page):
            state.verify_ownership()
            account, checks = await form_type(page).verify(info)
            checks.update(video=uploaded, upload_complete=completed)
            state.step("ready", "实际腾讯上传页逐项填写并核验完成", account=account, checks=checks)
            if args.dry_run:
                return False
            state.step("submitting", "已落盘提交意图，授权上游单次 click")
            # Flush the persisted intent before granting the only click authorization.
            with (state.folder / 'publish-progress.json').open('r+b') as intent:
                os.fsync(intent.fileno())
            state.verify_ownership()
            return True
        async def on_published(receipt):
            state.step("published", receipt, receipt=receipt)
        uploader.before_submit = before_submit
        uploader.on_published = on_published
        uploader.result_timeout = args.result_timeout
        await uploader.main()
        final = state.progress()
        if final["status"] == "published":
            return {"status": "PUBLISHED", "run_id": args.run, "receipt": uploader.publish_receipt}
        if args.dry_run and final["status"] == "ready":
            return {"status": "READY", "run_id": args.run}
        raise PublishError("UNVERIFIED_RESULT", "上游正常返回但没有平台成功证据")
    except Exception as exc:
        current = state.progress()["status"]
        if current == "published":
            return {"status": "PUBLISHED", "run_id": args.run, "message": "平台成功已落盘；后续清理异常"}
        if current == "submitting":
            state.step("uncertain", f"提交后无法确认结果：{exc}")
            raise PublishError("UNCERTAIN_SUBMISSION", "禁止自动重发，需核对平台结果") from exc
        if current not in ("uncertain", "blocked") and getattr(exc, "code", "") != "OWNERSHIP_LOST":
            state.step("blocked", str(exc) or type(exc).__name__, reason=getattr(exc, "code", "UPSTREAM_ERROR"))
        raise
    finally:
        cleanup_login_files(state.folder)
        for path in account_path(state).parent.glob("social-account_tencent_login_qrcode_*.png"):
            path.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run")
    parser.add_argument("--token")
    parser.add_argument("--owner")
    parser.add_argument("--smoke", action="store_true", help="只校验固定版本与导入，不启动浏览器")
    parser.add_argument("--dry-run", action="store_true", help="真实上传填表但不提交")
    parser.add_argument("--login-timeout", type=int, default=900)
    parser.add_argument("--upload-timeout", type=int, default=1200)
    parser.add_argument("--result-timeout", type=int, default=120)
    args = parser.parse_args()
    if args.smoke:
        load_tencent()
        result = {
            "status": "OK",
            "upstream_sha": UPSTREAM_SHA,
            "patchright_version": PATCHRIGHT_VERSION,
        }
    else:
        if not args.run or not args.token:
            parser.error("需要 --run 和 --token")
        state = PublicationState(Workspace(ROOT), args.run, resolve_owner(args.owner), args.token)
        result = asyncio.run(publish(args, state))
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    try:
        main()
    except Exception as exc:
        print(json.dumps({"status": getattr(exc, "code", "FAILED"), "message": str(exc)}, ensure_ascii=False), file=sys.stderr)
        sys.exit(1)
