"""向已绑定的本人 Telegram 私聊发送通知；凭据使用 Windows DPAPI 保存。"""

import argparse
import ctypes
from ctypes import wintypes
import hashlib
import json
import mimetypes
import os
from pathlib import Path
import re
import secrets
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request


class DeliveryError(Exception):
    def __init__(self, message, uncertain=False):
        super().__init__(message)
        self.uncertain = uncertain


def state_dir():
    if os.name != "nt":
        raise ValueError("当前凭据存储仅支持 Windows")
    return Path(os.environ["LOCALAPPDATA"]) / "CodexNotifications" / "telegram"


def protect(data, decrypt=False):
    class Blob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]

    buffer = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    target = Blob()
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    method = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    method.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.POINTER(Blob),
                       ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    method.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    if not method(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)):
        raise ValueError("Windows 凭据加解密失败，请在原 Windows 用户下重新配置")
    try:
        return ctypes.string_at(target.data, target.size)
    finally:
        kernel.LocalFree(target.data)


def save_config(folder, config):
    folder.mkdir(parents=True, exist_ok=True)
    temporary = folder / (secrets.token_hex(8) + ".tmp")
    temporary.write_bytes(protect(json.dumps(config).encode("utf-8")))
    os.replace(temporary, folder / "credentials.dpapi")


def load_config(folder):
    path = folder / "credentials.dpapi"
    if not path.exists():
        raise ValueError("尚未配置 Telegram，请先运行 setup-gui")
    return json.loads(protect(path.read_bytes(), decrypt=True))


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def api(token, method, fields=None, attachment=None, timeout=50):
    fields = fields or {}
    if attachment:
        field, path, content = attachment
        boundary = "codex" + secrets.token_hex(16)
        chunks = []
        for key, value in fields.items():
            chunks.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode("utf-8"))
        # 固定上传文件名，不把本机目录或用户文件名放入请求头。
        suffix = path.suffix.lower()
        mime = mimetypes.guess_type("media" + suffix)[0] or "application/octet-stream"
        chunks.extend([f'--{boundary}\r\nContent-Disposition: form-data; name="{field}"; filename="media{suffix}"\r\nContent-Type: {mime}\r\n\r\n'.encode(),
                       content, f"\r\n--{boundary}--\r\n".encode()])
        data = b"".join(chunks)
        content_type = "multipart/form-data; boundary=" + boundary
    else:
        data = urllib.parse.urlencode(fields).encode("utf-8")
        content_type = "application/x-www-form-urlencoded"
    request = urllib.request.Request("https://api.telegram.org/bot" + token + "/" + method,
                                     data=data, headers={"Content-Type": content_type})
    try:
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=timeout) as response:
            result = json.load(response)
    except urllib.error.HTTPError as error:
        # urllib 的异常和 Telegram 的原始响应可能包含 Token，不输出它们。
        code = error.code
        raise DeliveryError(f"Telegram HTTP {code}；请检查网络、机器人配置或稍后再试", code >= 500) from None
    except (OSError, ValueError):
        raise DeliveryError("Telegram 网络或响应异常，未能确认结果", uncertain=True) from None
    if not isinstance(result, dict) or "ok" not in result:
        raise DeliveryError("Telegram 响应格式异常，未能确认结果", uncertain=True)
    if not result.get("ok"):
        raise DeliveryError("Telegram 未接受请求，请检查配置或稍后重试")
    if "result" not in result:
        raise DeliveryError("Telegram 响应缺少结果，未能确认结果", uncertain=True)
    return result["result"]


def setup_gui(folder):
    import tkinter as tk
    from tkinter import messagebox, simpledialog
    root = tk.Tk()
    root.withdraw()
    try:
        if (folder / "credentials.dpapi").exists() and not messagebox.askyesno(
                "重新配置", "已有机器人配置。重新配置会清除收件人绑定，继续吗？", parent=root):
            return {"status": "cancelled"}
        token = simpledialog.askstring("Telegram 通知配置", "粘贴 BotFather 给你的 Token（仅保存在本机）：",
                                       show="*", parent=root)
        if token is None:
            return {"status": "cancelled"}
        token = token.strip()
        if not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]{20,}", token):
            raise ValueError("Token 格式不正确，请重新运行配置")
        bot = api(token, "getMe")
        if api(token, "getWebhookInfo").get("url"):
            raise ValueError("该机器人已连接其他服务，请使用专用通知机器人；不会删除原 webhook")
        save_config(folder, {"token": token, "bot_id": bot["id"], "username": bot["username"]})
        messagebox.showinfo("配置成功", "已验证 @" + bot["username"] + "，接下来回到 Codex 绑定你的私聊。", parent=root)
        return {"status": "configured", "bot": "@" + bot["username"]}
    except (ValueError, DeliveryError) as error:
        messagebox.showerror("配置未完成", str(error), parent=root)
        raise
    finally:
        root.destroy()


def start_binding(folder, config):
    if config.get("chat_id"):
        return {"status": "ready", "bot": "@" + config["username"]}
    if api(config["token"], "getWebhookInfo").get("url"):
        raise ValueError("机器人已有 webhook，请使用专用机器人")
    if config.get("expires", 0) < time.time():
        config.update(nonce=secrets.token_urlsafe(24), expires=time.time() + 900)
        save_config(folder, config)
    return {"status": "awaiting_user", "url": "https://t.me/" + config["username"] + "?start=" + config["nonce"],
            "instruction": "在本人 Telegram 中打开链接并点击开始；15 分钟内有效。"}


def poll_binding(folder, config):
    if config.get("chat_id"):
        return {"status": "ready", "bot": "@" + config["username"]}
    if config.get("expires", 0) < time.time():
        raise ValueError("绑定链接尚未生成或已过期，请运行 bind-start")
    updates = api(config["token"], "getUpdates", {"offset": config.get("offset", 0), "timeout": 20,
                                                  "allowed_updates": '["message"]'}, timeout=30)
    for update in updates:
        config["offset"] = max(config.get("offset", 0), update["update_id"] + 1)
        message = update.get("message", {})
        chat, sender = message.get("chat", {}), message.get("from", {})
        if (message.get("text") == "/start " + config["nonce"] and chat.get("type") == "private"
                and not sender.get("is_bot", True) and chat.get("id") == sender.get("id")
                and message.get("date", 0) >= config["expires"] - 900
                and time.time() <= config["expires"]):
            config["chat_id"] = chat["id"]
            config.pop("nonce", None)
            config.pop("expires", None)
            break
    save_config(folder, config)
    return {"status": "ready" if config.get("chat_id") else "awaiting_user", "bot": "@" + config["username"]}


def send(folder, config, event, text, kind="text", path=None):
    if not config.get("chat_id"):
        raise ValueError("尚未绑定本人私聊，请先完成 bind-start 和 bind-poll")
    if not event.strip() or len(event) > 300:
        raise ValueError("事件键不能为空或超过 300 字符")
    limit = 4096 if kind == "text" else 1024
    if not text.strip() or len(text.encode("utf-16-le")) // 2 > limit:
        raise ValueError(f"通知文字为空或超过 {limit} 字符，请缩短")
    attachment = None
    digest = hashlib.sha256((kind + "\0" + text).encode("utf-8"))
    if kind != "text":
        if not path or not path.is_file():
            raise ValueError("附件文件不存在")
        suffixes = {"photo": {".png", ".jpg", ".jpeg"}, "video": {".mp4"}, "document": {".png", ".jpg", ".jpeg", ".mp4", ".txt", ".md", ".json", ".pdf"}}
        size_limit = (10 if kind == "photo" else 50) * 1_000_000
        if path.suffix.lower() not in suffixes[kind] or not 0 < path.stat().st_size <= size_limit:
            raise ValueError("附件类型或大小不符合要求（图片 10 MB，视频/文件 50 MB）")
        content = path.read_bytes()
        if not 0 < len(content) <= size_limit:
            raise ValueError("附件大小在读取时发生变化，请重新检查")
        digest.update(content)
        attachment = (kind, path, content)
    fingerprint = digest.hexdigest()
    folder.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(folder / "deliveries.sqlite3", timeout=1)
    try:
        db.execute("CREATE TABLE IF NOT EXISTS deliveries (identity TEXT, event TEXT, digest TEXT, status TEXT, message_id INTEGER, updated REAL, PRIMARY KEY(identity,event))")
        db.commit()
        db.execute("BEGIN IMMEDIATE")
        identity = str(config["bot_id"]) + ":" + str(config["chat_id"])
        previous = db.execute("SELECT digest,status,message_id,updated FROM deliveries WHERE identity=? AND event=?", (identity, event)).fetchone()
        if previous:
            if previous[0] != fingerprint:
                raise ValueError("同一事件键的内容已变化；请核实后为新事件使用新键")
            if previous[1] == "sent":
                return {"status": "already_sent", "message_id": previous[2]}
            if previous[1] in ("pending", "uncertain"):
                raise ValueError("先前发送结果不确定，请先在 Telegram 核对；不会自动重发")
            if time.time() - previous[3] < 60:
                raise ValueError("刚刚发送失败，请至少 60 秒后再试")
        db.execute("INSERT OR REPLACE INTO deliveries VALUES (?,?,?,?,?,?)", (identity, event, fingerprint, "pending", None, time.time()))
        db.commit()
        # pending 先落盘：进程被杀或回执丢失时，也不会自动重复发送。
        fields = {"chat_id": config["chat_id"], "text" if kind == "text" else "caption": text}
        method = {"text": "sendMessage", "photo": "sendPhoto", "video": "sendVideo", "document": "sendDocument"}[kind]
        try:
            result = api(config["token"], method, fields, attachment)
            if not isinstance(result, dict) or not isinstance(result.get("message_id"), int):
                raise DeliveryError("Telegram 回执缺少消息编号，发送结果不确定", uncertain=True)
        except DeliveryError as error:
            db.execute("UPDATE deliveries SET status=?,updated=? WHERE identity=? AND event=?",
                       ("uncertain" if error.uncertain else "failed", time.time(), identity, event))
            db.commit()
            raise
        db.execute("UPDATE deliveries SET status='sent',message_id=?,updated=? WHERE identity=? AND event=?",
                   (result["message_id"], time.time(), identity, event))
        db.commit()
        return {"status": "sent", "message_id": result["message_id"]}
    finally:
        db.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["setup-gui", "status", "bind-start", "bind-poll", "send"])
    parser.add_argument("--event")
    parser.add_argument("--text-file", type=Path)
    parser.add_argument("--kind", choices=["text", "photo", "video", "document"], default="text")
    parser.add_argument("--file", type=Path)
    args = parser.parse_args()
    folder = state_dir()
    if args.action == "setup-gui":
        result = setup_gui(folder)
    elif args.action == "status" and not (folder / "credentials.dpapi").exists():
        result = {"status": "not_configured"}
    else:
        config = load_config(folder)
        if args.action == "status":
            result = {"status": "ready" if config.get("chat_id") else "not_bound", "bot": "@" + config["username"]}
        elif args.action == "bind-start":
            result = start_binding(folder, config)
        elif args.action == "bind-poll":
            result = poll_binding(folder, config)
        else:
            if not args.event or not args.text_file:
                parser.error("send 需要 --event 和 --text-file")
            result = send(folder, config, args.event, args.text_file.read_text(encoding="utf-8-sig"), args.kind, args.file)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    if sys.stdout is not None:
        sys.stdout.reconfigure(encoding="utf-8")
    if sys.stderr is not None:
        sys.stderr.reconfigure(encoding="utf-8")
    try:
        main()
    except (ValueError, OSError, DeliveryError, sqlite3.Error) as error:
        # 只输出受控业务错误；文件/数据库异常不输出潜在敏感路径。
        message = str(error) if isinstance(error, (ValueError, DeliveryError)) else "本机文件或通知记录不可用，请检查配置和是否有并发运行"
        print(json.dumps({"status": "error", "message": message}, ensure_ascii=False), file=sys.stderr)
        sys.exit(1)
