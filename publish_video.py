"""通过受控 Edge 将已完成的时光对照视频发布到视频号。"""

from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from urllib.parse import urlsplit
import urllib.request
import uuid

from codex_video import PUBLISH_CHECKS, ROOT, Workspace, read_json, write_json


POST_URL = "https://channels.weixin.qq.com/platform/post/create"
CONTENT_PATH = "/micro/content/post/create"
LOGIN_PATH = "/login.html"
CDP_URL = "http://127.0.0.1:9223"
PYTHON = Path("C:/Python313/python.exe")
LAUNCHER = ROOT / "打开视频号.bat"
NOTIFIER = ROOT / "skills/telegram-notify/scripts/telegram_notify.py"


class PublishError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class PublicationState:
    def __init__(self, work, run_id, owner, token):
        self.work = work
        self.run_id = run_id
        self.owner = owner
        self.token = token
        self.folder, _ = work.load(run_id)

    def progress(self):
        return self.work.execute("publish-status", run_id=self.run_id)

    def verify_ownership(self):
        execution = self.work.execute("ownership")["execution"]
        expected = ("held", self.owner, self.token, self.run_id)
        actual = ((execution or {}).get("state"), (execution or {}).get("owner_thread_id"),
                  (execution or {}).get("token"), (execution or {}).get("run_id"))
        if actual != expected:
            raise PublishError("OWNERSHIP_LOST", "当前任务已失去本批次执行权")

    def prepare(self):
        self.verify_ownership()
        progress = self.progress()
        if progress["status"] in ("not_started", "awaiting_publish"):
            path = self.folder / "publish-info.json"
            if not path.is_file():
                raise PublishError("MISSING_PUBLISH_INFO", "批次缺少 publish-info.json")
            self.verify_ownership()
            progress = self.work.execute("publish-init", owner=self.owner, token=self.token,
                                         run_id=self.run_id, file=path)
        if "info" not in progress:
            raise PublishError("NOT_PREPARED", "发布资料尚未初始化")
        return progress

    def step(self, status, evidence, **extra):
        self.verify_ownership()
        path = self.folder / (".publish-event-" + uuid.uuid4().hex + ".json")
        write_json(path, {"status": status, "evidence": evidence, **extra})
        try:
            return self.work.execute("publish-step", owner=self.owner, token=self.token,
                                     run_id=self.run_id, file=path)
        finally:
            path.unlink(missing_ok=True)


class ChannelsPage:
    def __init__(self, page):
        self.page = page
        self.uploaded_video = None
        self.page.set_default_timeout(5000)

    @property
    def url(self):
        return self.page.url

    def is_login(self):
        return urlsplit(self.page.url).path == LOGIN_PATH

    def probe(self):
        def visible_controls(frame):
            return frame.locator("input,textarea,button,[role=button],[role=combobox],[contenteditable]").evaluate_all(
                """els => els.filter(e => {
                    const r=e.getBoundingClientRect(), s=getComputedStyle(e);
                    return r.width>0 && r.height>0 && s.display!=='none' && s.visibility!=='hidden';
                }).map(e => ({tag:e.tagName,type:e.getAttribute('type'),role:e.getAttribute('role'),
                    placeholder:e.getAttribute('placeholder'),text:(e.innerText||e.getAttribute('aria-label')||'').trim().slice(0,100)}))""")
        return {
            "status": "probe",
            "url": self.page.url,
            "title": self.page.title(),
            "frames": [{"url": url_without_query(frame.url), "controls": visible_controls(frame)}
                       for frame in self.page.frames],
        }

    def _content_frame(self):
        # Wujie 将可见表单挂在主文档的 shadow DOM，子 iframe 可能为空。
        upload = self.page.locator('input[type="file"][accept*="video"]')
        if upload.count() == 1:
            return self.page
        matches = [frame for frame in self.page.frames
                   if urlsplit(frame.url).path == CONTENT_PATH]
        if len(matches) != 1:
            raise PublishError("PAGE_CHANGED", "未找到唯一的视频号发表动态内容页面")
        return matches[0]

    def _oauth_frame(self):
        for frame in self.page.frames:
            if urlsplit(frame.url).netloc == "open.weixin.qq.com":
                return frame
        return None

    def capture_qr(self, target, timeout=30):
        deadline = time.monotonic() + timeout
        retried = False
        while time.monotonic() < deadline:
            oauth = self._oauth_frame()
            failure = oauth.get_by_text("加载失败，点击重试", exact=True) if oauth else self.page.get_by_text(
                "加载失败，点击重试", exact=True)
            if failure.count() and failure.first.is_visible() and not retried:
                failure.first.click()
                retried = True
                self.page.wait_for_timeout(1000)
            frame = self._oauth_frame()
            if frame:
                images = frame.locator("img.js_qrcode_img")
                for index in range(images.count()):
                    image = images.nth(index)
                    box = image.bounding_box()
                    if image.is_visible() and box and box["width"] >= 120 and box["height"] >= 120:
                        image.screenshot(path=str(target))
                        return "oauth-qr-image"
            self.page.wait_for_timeout(500)
        raise PublishError("QR_NOT_FOUND", "登录页未出现可用二维码")

    def qr_expired(self):
        frame = self._oauth_frame()
        for scope in (frame, self.page):
            if not scope:
                continue
            for text in ("二维码已失效", "二维码已过期", "点击刷新", "刷新二维码"):
                locator = scope.get_by_text(text, exact=False)
                if locator.count() and locator.first.is_visible():
                    return locator.first
        return None

    def wait_for_login(self, timeout, on_new_qr, verify_ownership):
        started = time.monotonic()
        deadline = started + timeout
        next_refresh = started + 60
        while time.monotonic() < deadline:
            verify_ownership()
            if not self.is_login():
                self.page.wait_for_load_state("domcontentloaded")
                return
            now = time.monotonic()
            if now >= next_refresh:
                verify_ownership()
                self.page.reload(wait_until="domcontentloaded",
                                 timeout=max(1, (deadline - now) * 1000))
                next_refresh = time.monotonic() + 60
                if time.monotonic() >= deadline:
                    break
                if not self.is_login():
                    return
                on_new_qr()
            remaining = min(deadline, next_refresh) - time.monotonic()
            if remaining > 0:
                self.page.wait_for_timeout(min(2000, remaining * 1000))
        raise PublishError("LOGIN_TIMEOUT", "等待扫码登录超时；批次进度已保留")

    def open_post_page(self):
        if self.is_login():
            raise PublishError("LOGIN_REQUIRED", "视频号登录状态尚未生效")
        for attempt in range(2):
            if attempt or url_without_query(self.page.url) != POST_URL:
                self.page.goto(POST_URL, wait_until="domcontentloaded")
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                if self.is_login():
                    raise PublishError("LOGIN_REQUIRED", "视频号登录状态尚未生效")
                try:
                    frame = self._content_frame()
                except PublishError:
                    self.page.wait_for_timeout(500)
                    continue
                upload = frame.locator('input[type="file"][accept*="video"]')
                publish = frame.get_by_role("button", name="发表", exact=True)
                if upload.count() == 1 and publish.count() == 1:
                    return
                self.page.wait_for_timeout(500)
        raise PublishError("PAGE_CHANGED", "视频号发表动态内容页面未正常加载")

    def upload(self, video):
        inputs = self._content_frame().locator('input[type="file"][accept*="video"]')
        if inputs.count() != 1:
            raise PublishError("UPLOAD_CONTROL_NOT_FOUND", "未找到唯一的视频上传控件")
        inputs.set_input_files(str(video))
        selected = inputs.evaluate("""input => {
            const file = input.files && input.files[0];
            return file ? {name: file.name, size: file.size} : null;
        }""")
        expected = {"name": video.name, "size": video.stat().st_size}
        if selected != expected:
            raise PublishError("UPLOAD_VALUE_MISMATCH", "视频上传控件未保留本轮文件")
        self.uploaded_video = (selected["name"], selected["size"])

    @staticmethod
    def description_text(info):
        description = info["description"].strip()
        hashtags = ["#" + tag.lstrip("#") for tag in info["hashtags"] if tag.strip()]
        return description + (("\n" + " ".join(hashtags)) if hashtags else "")

    def fill_form(self, info):
        self._fill_unique('[contenteditable][data-placeholder="添加描述"]',
                          self.description_text(info), "视频描述")
        self._fill_unique('[placeholder="填写短标题有机会获得更多流量"]',
                          info["short_title"], "短标题")
        settings = info["settings"]
        self._choose_option("位置", settings["location"])
        self._choose_option("添加到合集", settings["collection"])
        if settings.get("link") is not None:
            raise PublishError("UNSUPPORTED_SETTING", "固定发布流程只支持不添加链接")
        self._ensure_text_setting("活动", settings["activity"])
        self._ensure_radio("定时发表", settings["schedule"])
        if settings["original_requested"]:
            self._ensure_original()
        self._choose_mark(settings["ai_label"])

    def _publish_button(self):
        return self._content_frame().get_by_role("button", name="发表", exact=True)

    @staticmethod
    def _button_ready(button):
        classes = (button.get_attribute("class") or "").split()
        return (button.is_visible() and button.is_enabled()
                and "weui-desktop-btn_disabled" not in classes)

    def wait_until_ready(self, timeout=1200):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            frame = self._content_frame()
            busy = any(self._visible_text(text) for text in ("上传中", "处理中", "转码中"))
            publish = self._publish_button()
            if publish.count() == 1 and self._button_ready(publish) and not busy:
                return
            frame.wait_for_timeout(2000)
        raise PublishError("UPLOAD_TIMEOUT", "视频上传或平台处理未在限定时间内完成")

    def account_name(self):
        locator = self.page.locator(".account-info > .name")
        visible = [locator.nth(index) for index in range(locator.count())
                   if locator.nth(index).is_visible()]
        names = {item.inner_text().strip() for item in visible if item.inner_text().strip()}
        if len(names) == 1:
            return names.pop()
        raise PublishError("ACCOUNT_NOT_FOUND", "未能从发布页读取唯一的当前视频号名称")

    def form_checks(self, info):
        settings = info["settings"]
        video = Path(info["video"])
        expected_video = (video.name, video.stat().st_size) if video.is_file() else None
        checks = {
            "video": expected_video is not None and self.uploaded_video == expected_video,
            "upload_complete": self._button_ready(self._publish_button()),
            "description": self._field_value(
                '[contenteditable][data-placeholder="添加描述"]') == self.description_text(info),
            "short_title": self._field_value(
                '[placeholder="填写短标题有机会获得更多流量"]') == info["short_title"],
            "location": self._row_has_value("位置", settings["location"]),
            "collection": self._row_has_value("添加到合集", settings["collection"]),
            "link": settings.get("link") is None and self._row_has_value("链接", "选择链接"),
            "activity": self._row_has_value("活动", settings["activity"]),
            "schedule": self._checked_choice("定时发表", settings["schedule"]),
            "original": (not settings["original_requested"] or self._original_checked()),
            "ai_label": self._row_has_value("视频标注", settings["ai_label"]),
        }
        if not all(checks.values()):
            missing = "、".join(name for name, passed in checks.items() if not passed)
            raise PublishError("FORM_VALUE_MISMATCH", f"发布前页面核验未通过：{missing}")
        return checks

    def submit(self):
        publish = self._publish_button()
        if publish.count() != 1 or not self._button_ready(publish):
            raise PublishError("PUBLISH_BUTTON_NOT_READY", "发表按钮不存在或尚不可点击")
        publish.click()

    def wait_for_result(self, timeout=120):
        deadline = time.monotonic() + timeout
        success_texts = ("发表成功", "发布成功", "提交成功")
        while time.monotonic() < deadline:
            scopes = [self.page]
            try:
                scopes.append(self._content_frame())
            except PublishError:
                pass
            for text in success_texts:
                for scope in scopes:
                    locator = scope.get_by_text(text, exact=True)
                    if any(locator.nth(index).is_visible() for index in range(locator.count())):
                        return f"页面显示'{text}'，地址 {self.page.url}"
            if self.is_login():
                return None
            self.page.wait_for_timeout(1000)
        return None

    def _fill_unique(self, selector, value, name):
        locator = self._content_frame().locator(selector)
        if locator.count() != 1 or not locator.is_visible():
            raise PublishError('FORM_CONTROL_NOT_FOUND', f'未找到唯一的{name}输入框')
        locator.fill(value)

    def _field_value(self, selector):
        locator = self._content_frame().locator(selector)
        if locator.count() != 1:
            return ''
        if locator.get_attribute('contenteditable') is not None:
            return locator.inner_text().strip()
        return locator.input_value().strip()

    def _visible_text(self, text):
        for frame in self.page.frames:
            locator = frame.get_by_text(text, exact=False)
            if any(locator.nth(index).is_visible() for index in range(locator.count())):
                return True
        return False

    def _row_has_value(self, label, desired):
        selectors = {
            "位置": ".position-display .location-name",
            "添加到合集": ".post-album-display .display-text",
            "链接": ".link-display-wrap",
            "活动": ".activity-display",
            "视频标注": ".mark-tag-select .select-value",
        }
        selector = selectors.get(label)
        if not selector:
            raise PublishError("FORM_CONTROL_NOT_FOUND", f"不支持核验'{label}'")
        locator = self._content_frame().locator(selector)
        return (locator.count() == 1 and locator.is_visible()
                and locator.inner_text().strip() == desired)

    def _choose_option(self, label, desired):
        if self._row_has_value(label, desired):
            return
        selectors = {
            '位置': ('.position-display', '.location-filter-wrap .option-item'),
            '添加到合集': ('.post-album-display', '.post-album-wrap .filter-wrap .option-item'),
            '活动': ('.activity-display', '.activity-filter-wrap .option-item'),
        }
        controls = selectors.get(label)
        if not controls:
            raise PublishError("FORM_CONTROL_NOT_FOUND", f"不支持设置'{label}'")
        trigger_selector, option_selector = controls
        frame = self._content_frame()
        options = frame.locator(option_selector).filter(has_text=desired)
        visible = [options.nth(index) for index in range(options.count())
                   if options.nth(index).is_visible()
                   and desired in options.nth(index).inner_text().splitlines()]
        if not visible:
            trigger = frame.locator(trigger_selector)
            if trigger.count() != 1 or not trigger.is_visible():
                raise PublishError("FORM_CONTROL_NOT_FOUND", f"未找到'{label}'选择控件")
            trigger.click()
            visible = [options.nth(index) for index in range(options.count())
                       if options.nth(index).is_visible()
                       and desired in options.nth(index).inner_text().splitlines()]
        if len(visible) != 1:
            raise PublishError("FORM_OPTION_NOT_FOUND", f"未找到唯一的'{desired}'选项")
        visible[0].click()
        if not self._row_has_value(label, desired):
            raise PublishError("FORM_VALUE_MISMATCH", f"'{label}'未设置为'{desired}'")

    def _ensure_text_setting(self, label, desired):
        if not self._row_has_value(label, desired):
            self._choose_option(label, desired)

    def _checked_choice(self, label, desired=None, input_type='radio'):
        return self._content_frame().locator('.form-item').evaluate_all('''(rows, {label, desired, inputType}) => {
            const visible=e=>{const r=e.getBoundingClientRect(),s=getComputedStyle(e);return r.width>0&&r.height>0&&s.display!=='none'&&s.visibility!=='hidden'};
            for(const row of rows){
                const labelElement=[...row.querySelectorAll('.label,.with-tip-label')]
                    .find(e=>visible(e)&&e.textContent.trim()===label);
                if(!labelElement)continue;
                for(const input of row.querySelectorAll(`input[type=${inputType}]`)){
                    const owner=input.closest('label')||input.parentElement;
                    if(input.checked && (desired===null || owner.innerText.trim().includes(desired)))return true;
                }
            }
            return false;
        }''', {'label': label, 'desired': desired, 'inputType': input_type})

    def _ensure_radio(self, label, desired):
        if self._checked_choice(label, desired):
            return
        frame = self._content_frame()
        group = frame.locator('.weui-desktop-radio-group')
        option = group.get_by_text(desired, exact=True)
        visible = [option.nth(index) for index in range(option.count())
                   if option.nth(index).is_visible()]
        if len(visible) != 1:
            raise PublishError("FORM_OPTION_NOT_FOUND", f"未找到唯一的'{desired}'选项")
        visible[0].click()
        if not self._checked_choice(label, desired):
            raise PublishError("FORM_VALUE_MISMATCH", f"'{label}'未设置为'{desired}'")

    def _original_checked(self):
        checkbox = self._content_frame().locator(
            '.declare-original-checkbox input[type="checkbox"]')
        return checkbox.count() == 1 and checkbox.is_checked()

    def _ensure_original(self):
        if self._original_checked():
            return
        frame = self._content_frame()
        checkbox = frame.locator('.declare-original-checkbox input[type=checkbox]')
        if checkbox.count() != 1:
            raise PublishError('FORM_CONTROL_NOT_FOUND', '未找到声明原创选项')
        checkbox.check()
        dialog = frame.locator('.declare-original-dialog .weui-desktop-dialog__wrp')
        if dialog.count() and dialog.is_visible():
            agreement = dialog.locator('input[type="checkbox"]')
            if agreement.count() != 1:
                raise PublishError('FORM_CONTROL_NOT_FOUND', '未找到原创声明协议选项')
            if not agreement.is_checked():
                agreement.check()
            confirm = dialog.get_by_role('button', name='声明原创', exact=True)
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if confirm.count() == 1 and self._button_ready(confirm):
                    confirm.click()
                    break
                frame.wait_for_timeout(250)
            else:
                raise PublishError('FORM_CONTROL_NOT_FOUND', '原创声明确认按钮尚不可点击')
        if not self._original_checked():
            raise PublishError('FORM_VALUE_MISMATCH', '声明原创未成功勾选')

    def _choose_mark(self, desired):
        if self._row_has_value('视频标注', desired):
            return
        frame = self._content_frame()
        options = frame.locator(".mark-tag-option .option-main").filter(has_text=desired)
        visible = [options.nth(index) for index in range(options.count())
                   if options.nth(index).is_visible()
                   and options.nth(index).inner_text().strip() == desired]
        if not visible:
            trigger = frame.locator(".mark-tag-select .select-display")
            if trigger.count() != 1 or not trigger.is_visible():
                raise PublishError('FORM_CONTROL_NOT_FOUND', '未找到视频标注选择控件')
            trigger.click()
            visible = [options.nth(index) for index in range(options.count())
                       if options.nth(index).is_visible()
                       and options.nth(index).inner_text().strip() == desired]
        if len(visible) != 1:
            raise PublishError("FORM_OPTION_NOT_FOUND", f"未找到唯一的'{desired}'选项")
        visible[0].click()
        if not self._row_has_value('视频标注', desired):
            raise PublishError("FORM_VALUE_MISMATCH", f"视频标注未设置为'{desired}'")



def url_without_query(url):
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}{parts.path}" if parts.scheme else url


def resolve_owner(argument):
    current = os.environ.get("CODEX_THREAD_ID")
    if current and argument and argument != current:
        raise PublishError("OWNER_MISMATCH", "--owner 与当前 Codex 任务不一致")
    owner = current or argument
    if not owner:
        raise PublishError("MISSING_OWNER", "缺少真实任务 ID；请在 Codex 中运行或传入 --owner")
    return owner


def wait_for_cdp(url=CDP_URL, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url + "/json/version", timeout=1) as response:
                if json.load(response).get("webSocketDebuggerUrl"):
                    return
        except (OSError, ValueError):
            time.sleep(.5)
    raise PublishError("EDGE_NOT_CONNECTED", "未能连接由打开视频号.bat启动的 Edge")


def ensure_edge():
    try:
        wait_for_cdp(timeout=1)
        return
    except PublishError:
        pass
    subprocess.Popen(["cmd.exe", "/c", str(LAUNCHER)], cwd=ROOT,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    wait_for_cdp()


def connect_page(playwright):
    browser = playwright.chromium.connect_over_cdp(CDP_URL)
    pages = [page for context in browser.contexts for page in context.pages
             if urlsplit(page.url).netloc == "channels.weixin.qq.com"]
    if not pages:
        context = browser.contexts[0] if browser.contexts else browser.new_context()
        page = context.new_page()
        page.goto(POST_URL, wait_until="domcontentloaded")
    else:
        ready = []
        login = []
        for candidate in pages:
            if urlsplit(candidate.url).path == LOGIN_PATH:
                login.append(candidate)
                continue
            frames = [frame for frame in candidate.frames
                      if urlsplit(frame.url).path == CONTENT_PATH]
            if len(frames) == 1:
                upload = frames[0].locator('input[type="file"][accept*="video"]')
                publish = frames[0].get_by_role("button", name="发表", exact=True)
                if upload.count() == 1 and publish.count() == 1:
                    ready.append(candidate)
        page = ready[-1] if ready else (login[-1] if login else pages[-1])
    return browser, page


def append_notification_receipt(folder, key, receipt):
    path = folder / "notification-receipts.json"
    data = read_json(path) if path.exists() else {"deliveries": {}}
    data.setdefault("deliveries", {})[key] = receipt
    write_json(path, data)


def cleanup_login_files(folder):
    for pattern in (".login-qr-current.png", "login-qr-*.png", "telegram-login-*.txt"):
        for path in folder.glob(pattern):
            path.unlink(missing_ok=True)


def notify_qr(state, channels, progress):
    state.verify_ownership()
    folder = state.folder
    cleanup_login_files(folder)
    previous = [event for event in progress.get("events", []) if event.get("qr_sha256")]
    next_sequence = max((event.get("qr_sequence", 0) for event in previous), default=0) + 1
    temporary = folder / ".login-qr-current.png"
    source = channels.capture_qr(temporary)
    image_hash = hashlib.sha256(temporary.read_bytes()).hexdigest()
    matching = next((event for event in reversed(previous) if event.get("qr_sha256") == image_hash), None)
    sequence = matching.get("qr_sequence") if matching else next_sequence
    image = folder / f"login-qr-{sequence}.png"
    os.replace(temporary, image)
    if matching and matching.get("telegram_receipt"):
        return image, progress
    event = matching.get("qr_event") if matching else f"time-echo:{state.run_id}:login-qr:{sequence}"
    if not matching:
        state.step("waiting_login", "视频号登录页显示完整二维码", qr_sequence=sequence,
                   qr_sha256=image_hash, qr_event=event, qr_source=source)
    text_file = folder / f"telegram-login-{sequence}.txt"
    text_file.write_text(f"时光对照视频 {state.run_id} 需要登录视频号。请用微信扫描附件二维码，并在手机上确认登录。\n", encoding="utf-8")
    state.verify_ownership()
    result = subprocess.run([str(PYTHON), str(NOTIFIER), "send", "--event", event,
                             "--text-file", str(text_file), "--kind", "document", "--file", str(image)],
                            cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
                            timeout=60, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if result.returncode:
        try:
            message = json.loads(result.stderr)["message"]
        except (ValueError, KeyError, TypeError):
            message = "Telegram 二维码通知失败"
        raise PublishError("TELEGRAM_FAILED", message)
    receipt = json.loads(result.stdout)
    if receipt.get("status") not in ("sent", "already_sent"):
        raise PublishError("TELEGRAM_FAILED", "Telegram 尚未完成二维码通知配置")
    append_notification_receipt(folder, f"login_qr_{sequence}", receipt)
    progress = state.step("waiting_login", "二维码已发送到本人 Telegram，继续等待网页登录",
                          qr_sequence=sequence, qr_sha256=image_hash, qr_event=event,
                          telegram_receipt=receipt)
    return image, progress


def terminal_publication_result(args, progress):
    status = progress["status"]
    if status in ("published", "delivered"):
        return {"status": status, "run_id": args.run, "message": "该批次不再重复发布"}
    if status in ("submitting", "uncertain"):
        return {"status": "UNCERTAIN_SUBMISSION", "run_id": args.run,
                "message": "批次已经尝试提交；必须先核对平台结果，脚本不会再次点击发表"}
    return None


def publish_on_page(args, state, progress, channels):
    terminal = terminal_publication_result(args, progress)
    if terminal:
        return terminal
    if args.probe:
        return channels.probe()
    try:
        state.verify_ownership()
        if channels.is_login():
            try:
                _, progress = notify_qr(state, channels, progress)

                def refresh_qr():
                    notify_qr(state, channels, state.progress())

                channels.wait_for_login(args.login_timeout, refresh_qr, state.verify_ownership)
            finally:
                cleanup_login_files(state.folder)
        state.verify_ownership()
        channels.open_post_page()
        current = state.progress()["status"]
        if current != "uploading":
            progress = state.step("uploading", f"已登录并打开发表动态页面：{channels.url}")
        info = progress["info"]
        state.verify_ownership()
        channels.upload(Path(info["video"]))
        state.verify_ownership()
        channels.fill_form(info)
        channels.wait_until_ready(args.upload_timeout)
        state.verify_ownership()
        account = channels.account_name()
        checks = channels.form_checks(info)
        state.step("ready", "页面已显示本轮视频、文案、固定设置及可点击的发表按钮",
                   account=account, checks=checks)
        if args.dry_run:
            return {"status": "READY", "run_id": args.run, "account": account,
                    "message": "试运行已完成上传和填表，未点击发表"}
    except PublishError as error:
        if error.code != "OWNERSHIP_LOST" and state.progress()["status"] not in (
                "blocked", "submitting", "uncertain"):
            state.step("blocked", str(error), reason=error.code)
        raise
    except Exception as error:
        if state.progress()["status"] not in ("blocked", "submitting", "uncertain"):
            state.step("blocked", f"发布页操作异常：{error}", reason="BROWSER_ERROR")
        raise PublishError("BROWSER_ERROR", "发布页操作异常；批次进度已保留") from error
    state.step("submitting", "发表前检查完成，现登记单次提交意图")
    try:
        channels.submit()
        receipt = channels.wait_for_result(args.result_timeout)
    except Exception as error:
        state.step("uncertain", "登记提交意图后浏览器操作异常，无法确认平台结果")
        raise PublishError("UNCERTAIN_SUBMISSION", "提交阶段异常；脚本不会自动再次发布") from error
    if not receipt:
        state.step("uncertain", "点击发布后未在限定时间内取得明确平台结果")
        return {"status": "UNCERTAIN_SUBMISSION", "run_id": args.run}
    state.step("published", receipt, receipt=receipt)
    return {"status": "PUBLISHED", "run_id": args.run, "account": account, "receipt": receipt}


def run_publication(args):
    owner = resolve_owner(args.owner)
    work = Workspace(ROOT)
    state = PublicationState(work, args.run, owner, args.token)
    progress = state.prepare()
    terminal = terminal_publication_result(args, progress)
    if terminal:
        return terminal
    state.verify_ownership()
    ensure_edge()
    state.verify_ownership()
    from playwright.sync_api import sync_playwright
    with sync_playwright() as playwright:
        _, page = connect_page(playwright)
        state.verify_ownership()
        return publish_on_page(args, state, progress, ChannelsPage(page))


def probe_page():
    ensure_edge()
    from playwright.sync_api import sync_playwright
    with sync_playwright() as playwright:
        _, page = connect_page(playwright)
        return ChannelsPage(page).probe()


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run")
    parser.add_argument("--token")
    parser.add_argument("--owner")
    parser.add_argument("--probe", action="store_true", help="只读取当前页面结构，不上传或填写")
    parser.add_argument("--dry-run", action="store_true", help="上传并填表，但不点击发布")
    parser.add_argument("--login-timeout", type=int, default=900)
    parser.add_argument("--upload-timeout", type=int, default=1200)
    parser.add_argument("--result-timeout", type=int, default=120)
    args = parser.parse_args()
    if not args.probe and (not args.run or not args.token):
        parser.error("正式发布或 --dry-run 需要 --run 和 --token")
    return args


def main():
    args = parse_args()
    result = probe_page() if args.probe and not args.run else run_publication(args)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    if sys.stdout is not None:
        sys.stdout.reconfigure(encoding="utf-8")
    if sys.stderr is not None:
        sys.stderr.reconfigure(encoding="utf-8")
    try:
        main()
    except PublishError as error:
        print(json.dumps({"status": error.code, "message": str(error)}, ensure_ascii=False), file=sys.stderr)
        sys.exit(1)
    except Exception as error:
        print(json.dumps({"status": "FAILED", "message": str(error)}, ensure_ascii=False), file=sys.stderr)
        sys.exit(1)
