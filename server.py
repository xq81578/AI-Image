"""本机目录保存服务。

服务只绑定回环地址，浏览器端通过一次性会话 token 调用本机 API。模型请求仍
由前端直接完成；本文件只负责静态文件、公开图片中转、目录选择和 PNG 保存。
"""

from __future__ import annotations

import argparse
import base64
import binascii
import collections
import hashlib
import http.client
import http.server
import io
import ipaddress
import json
import os
from pathlib import Path
import queue
import re
import secrets
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
from typing import Any, Callable, Optional
import warnings
from urllib.parse import unquote, urljoin, urlsplit
from urllib.request import Request, build_opener, ProxyHandler
import webbrowser

try:
    from PIL import Image
except ImportError as exc:  # pragma: no cover - installation is part of the app setup
    raise RuntimeError("需要安装 Pillow 才能启动本机保存服务") from exc


APP_NAME = "time-echo"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
LOCAL_TOKEN_HEADER = "X-Local-Token"
MAX_JSON_BODY = 36 * 1024 * 1024
MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_IMAGE_REDIRECTS = 3
IMAGE_TIMEOUT_SECONDS = 15
MAX_FILENAME_LENGTH = 180
MAX_REQUEST_ID_LENGTH = 160
MAX_IDEMPOTENCY_RECORDS = 4096

STATIC_FILES = {
    "index.html": "text/html; charset=utf-8",
    "batch-core.js": "text/javascript; charset=utf-8",
    "batch-app.js": "text/javascript; charset=utf-8",
    "batch-prompts.js": "text/javascript; charset=utf-8",
    "batch-app.css": "text/css; charset=utf-8",
}

IMAGE_MIME_TYPES = {
    "PNG": "image/png",
    "JPEG": "image/jpeg",
    "JPG": "image/jpeg",
    "WEBP": "image/webp",
    "GIF": "image/gif",
    "BMP": "image/bmp",
    "TIFF": "image/tiff",
    "ICO": "image/x-icon",
}

_SAFE_FILENAME_RE = re.compile(r"^[^<>:\"/\\|?*\x00-\x1f\x7f]+$")
_WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


class ApiError(Exception):
    """一个可安全返回给前端的 API 错误。"""

    def __init__(self, message: str, code: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.code = code
        self.status = status


class DialogRequest:
    def __init__(self, action: str):
        self.action = action
        self.event = threading.Event()
        self.result: Any = None
        self.error: Optional[BaseException] = None


class MainThreadDispatcher:
    """把 Tk 对话框和打开文件夹动作交给主线程执行。"""

    def __init__(self):
        self._requests: queue.Queue[DialogRequest] = queue.Queue()

    def submit(self, action: str, timeout: float = 300.0) -> Any:
        request = DialogRequest(action)
        self._requests.put(request)
        if not request.event.wait(timeout):
            raise ApiError("等待本机窗口响应超时", "ui_timeout", 504)
        if request.error is not None:
            if isinstance(request.error, ApiError):
                raise request.error
            raise ApiError("本机窗口操作失败", "ui_error", 500)
        return request.result

    def get_nowait(self) -> Optional[DialogRequest]:
        try:
            return self._requests.get_nowait()
        except queue.Empty:
            return None


class _PinnedHTTPConnection(http.client.HTTPConnection):
    """连接到已校验的 IP，同时保留原始域名作为 Host。"""

    def __init__(self, display_host: str, connect_host: str, port: int, timeout: float):
        super().__init__(display_host, port=port, timeout=timeout)
        self._connect_host = connect_host

    def connect(self) -> None:
        self.sock = socket.create_connection((self._connect_host, self.port), self.timeout)


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPS 连接固定到已校验 IP，并用域名做证书校验和 SNI。"""

    def __init__(
        self,
        display_host: str,
        connect_host: str,
        port: int,
        timeout: float,
        context: ssl.SSLContext,
    ):
        super().__init__(display_host, port=port, timeout=timeout, context=context)
        self._connect_host = connect_host

    def connect(self) -> None:
        raw_socket = socket.create_connection((self._connect_host, self.port), self.timeout)
        try:
            self.sock = self._context.wrap_socket(raw_socket, server_hostname=self.host)
        except BaseException:
            raw_socket.close()
            raise


def _is_disallowed_ip(value: ipaddress._BaseAddress) -> bool:
    # is_global 在不同 Python/IP 地址类别上对组播的含义并不适合作为唯一
    # 的 SSRF 判据，下面把特殊地址类别显式排除。
    return (
        not value.is_global
        or value.is_private
        or value.is_loopback
        or value.is_link_local
        or value.is_multicast
        or value.is_unspecified
        or value.is_reserved
    )


def _resolve_public_host(hostname: str, port: int) -> list[str]:
    try:
        literal = ipaddress.ip_address(hostname)
    except ValueError:
        literal = None

    addresses: set[ipaddress._BaseAddress] = set()
    if literal is not None:
        addresses.add(literal)
    else:
        try:
            infos = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
        except (OSError, socket.gaierror) as exc:
            raise ApiError("图片地址无法解析", "address_unavailable", 400) from exc
        for info in infos:
            try:
                addresses.add(ipaddress.ip_address(info[4][0]))
            except (IndexError, ValueError):
                continue

    if not addresses:
        raise ApiError("图片地址无法解析", "address_unavailable", 400)
    if any(_is_disallowed_ip(address) for address in addresses):
        raise ApiError("禁止访问本机或私有地址", "private_address", 400)
    return [str(address) for address in addresses]


def _validate_public_url(value: str) -> None:
    if not isinstance(value, str) or len(value) > 4096:
        raise ApiError("图片地址无效", "invalid_url", 400)
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise ApiError("图片地址无效", "invalid_url", 400) from exc
    if parsed.scheme.lower() not in {"http", "https"} or not hostname:
        raise ApiError("只支持公网 http/https 图片地址", "invalid_url", 400)
    if parsed.username is not None or parsed.password is not None:
        raise ApiError("图片地址不能包含账号信息", "invalid_url", 400)
    if port is None:
        port = 443 if parsed.scheme.lower() == "https" else 80
    if not 1 <= port <= 65535:
        raise ApiError("图片地址端口无效", "invalid_url", 400)
    if hostname.lower().rstrip(".") in {
        "localhost",
        "localhost.localdomain",
        "ip6-localhost",
        "ip6-loopback",
    }:
        raise ApiError("禁止访问本机或私有地址", "private_address", 400)
    _resolve_public_host(hostname, port)


def _decode_and_verify_image(data: bytes) -> tuple[str, str]:
    if not data or len(data) > MAX_IMAGE_BYTES:
        raise ApiError("图片体积超出限制", "image_too_large", 413)
    try:
        with Image.open(io.BytesIO(data)) as image:
            image_format = (image.format or "").upper()
            if image_format not in IMAGE_MIME_TYPES:
                raise ApiError("图片格式不受支持", "unsupported_image_format", 415)
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                image.verify()
        with Image.open(io.BytesIO(data)) as image:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                image.load()
            width, height = image.size
            if width <= 0 or height <= 0:
                raise ApiError("图片尺寸无效", "invalid_image", 400)
    except ApiError:
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning, OSError, ValueError) as exc:
        raise ApiError("图片内容无法验证", "invalid_image", 400) from exc
    return IMAGE_MIME_TYPES[image_format], image_format


def _read_limited(response, limit: int) -> bytes:
    content_length = response.headers.get("Content-Length")
    if content_length:
        try:
            if int(content_length) > limit:
                raise ApiError("图片体积超出限制", "image_too_large", 413)
        except ValueError as exc:
            raise ApiError("图片响应长度无效", "invalid_image_response", 502) from exc
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = response.read(min(64 * 1024, limit + 1 - total))
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            raise ApiError("图片体积超出限制", "image_too_large", 413)
        chunks.append(chunk)
    return b"".join(chunks)


def _download_image(url: str) -> tuple[bytes, str]:
    current_url = url
    tls_context = ssl.create_default_context()
    for redirect_index in range(MAX_IMAGE_REDIRECTS + 1):
        _validate_public_url(current_url)
        try:
            parsed = urlsplit(current_url)
            hostname = parsed.hostname
            if not hostname:
                raise ApiError("图片地址无效", "invalid_url", 400)
            try:
                display_host = hostname.encode("idna").decode("ascii")
            except UnicodeError as exc:
                raise ApiError("图片地址无效", "invalid_url", 400) from exc
            port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
            addresses = _resolve_public_host(display_host, port)
        except ApiError:
            raise
        except ValueError as exc:
            raise ApiError("图片地址无效", "invalid_url", 400) from exc

        request_target = parsed.path or "/"
        if parsed.query:
            request_target += "?" + parsed.query
        host_for_header = display_host
        try:
            if ipaddress.ip_address(display_host).version == 6:
                host_for_header = f"[{display_host}]"
        except ValueError:
            pass
        if port not in {80, 443}:
            host_for_header = f"{host_for_header}:{port}"
        headers = {
            "Host": host_for_header,
            "User-Agent": "time-echo-local/1.0",
            "Accept": "image/*",
            "Connection": "close",
        }

        response = None
        connection = None
        last_error: Optional[BaseException] = None
        for address in addresses:
            try:
                if parsed.scheme.lower() == "https":
                    connection = _PinnedHTTPSConnection(
                        display_host,
                        address,
                        port,
                        IMAGE_TIMEOUT_SECONDS,
                        tls_context,
                    )
                else:
                    connection = _PinnedHTTPConnection(display_host, address, port, IMAGE_TIMEOUT_SECONDS)
                connection.request("GET", request_target, headers=headers)
                response = connection.getresponse()
                break
            except (OSError, TimeoutError, ssl.SSLError, http.client.HTTPException) as exc:
                last_error = exc
                if connection is not None:
                    connection.close()
                connection = None
                response = None
        if response is None:
            raise ApiError("图片下载失败或超时", "image_download_failed", 502) from last_error

        try:
            if response.status in {301, 302, 303, 307, 308}:
                location = response.getheader("Location")
                if not location:
                    raise ApiError("图片重定向地址缺失", "image_download_failed", 502)
                if redirect_index >= MAX_IMAGE_REDIRECTS:
                    raise ApiError("图片重定向次数过多", "too_many_redirects", 502)
                current_url = urljoin(current_url, location)
                continue
            if response.status < 200 or response.status >= 300:
                raise ApiError("图片服务器返回错误", "image_download_failed", 502)
            data = _read_limited(response, MAX_IMAGE_BYTES)
        except ApiError:
            raise
        except (OSError, TimeoutError, http.client.HTTPException) as exc:
            raise ApiError("图片下载失败或超时", "image_download_failed", 502) from exc
        finally:
            response.close()
            if connection is not None:
                connection.close()

        mime, _ = _decode_and_verify_image(data)
        return data, mime
    raise ApiError("图片重定向次数过多", "too_many_redirects", 502)


def _validate_filename(filename: Any) -> str:
    if not isinstance(filename, str) or not filename or len(filename) > MAX_FILENAME_LENGTH:
        raise ApiError("文件名无效", "invalid_filename", 400)
    if filename in {".", ".."} or ".." in filename or not _SAFE_FILENAME_RE.fullmatch(filename):
        raise ApiError("文件名包含非法字符", "invalid_filename", 400)
    if any(not character.isprintable() for character in filename):
        raise ApiError("文件名包含非法字符", "invalid_filename", 400)
    if filename.endswith((".", " ")):
        raise ApiError("文件名不能以空格或句点结尾", "invalid_filename", 400)
    suffix = Path(filename).suffix.lower()
    if suffix != ".png":
        raise ApiError("保存文件必须使用 .png 扩展名", "invalid_filename", 400)
    stem = Path(filename).stem.rstrip(" .")
    if not stem:
        raise ApiError("文件名不可用", "invalid_filename", 400)
    if stem.upper() in _WINDOWS_RESERVED_NAMES:
        raise ApiError("文件名不可用", "invalid_filename", 400)
    return filename


def _decode_png_data_url(data_url: Any) -> bytes:
    if not isinstance(data_url, str) or len(data_url) > (MAX_IMAGE_BYTES * 4 // 3 + 4096):
        raise ApiError("PNG 数据无效或过大", "invalid_png", 400)
    header, separator, payload = data_url.partition(",")
    if not separator:
        raise ApiError("PNG 数据格式无效", "invalid_png", 400)
    parts = header.split(";")
    if not parts or parts[0].lower() != "data:image/png" or "base64" not in {part.lower() for part in parts[1:]}:
        raise ApiError("只接受 base64 PNG 数据", "invalid_png", 400)
    payload = re.sub(r"\s+", "", payload)
    try:
        data = base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ApiError("PNG 数据无法解码", "invalid_png", 400) from exc
    mime, image_format = _decode_and_verify_image(data)
    if mime != "image/png" or image_format != "PNG":
        raise ApiError("数据内容不是 PNG", "invalid_png", 400)
    return data


def _path_occupied(path: Path) -> bool:
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


class LocalApp:
    """服务状态和主线程动作。可以直接被测试或其它本机启动器使用。"""

    def __init__(
        self,
        *,
        static_root: Optional[os.PathLike[str] | str] = None,
        output_dir: Optional[os.PathLike[str] | str] = None,
        token: Optional[str] = None,
        folder_selector: Optional[Callable[[Optional[Path]], Optional[str | os.PathLike[str]]]] = None,
        folder_opener: Optional[Callable[[Path], None]] = None,
    ):
        self.static_root = Path(static_root or Path(__file__).resolve().parent).resolve()
        self.token = token or secrets.token_urlsafe(32)
        self.dispatcher = MainThreadDispatcher()
        self.folder_selector = folder_selector
        self.folder_opener = folder_opener
        self._folder_lock = threading.RLock()
        self._save_lock = threading.RLock()
        self._folder: Optional[Path] = None
        self._save_records: collections.OrderedDict[str, tuple[str, str, str]] = collections.OrderedDict()
        if output_dir is not None:
            folder = Path(output_dir).expanduser().resolve()
            folder.mkdir(parents=True, exist_ok=True)
            if not folder.is_dir():
                raise ValueError("output directory is not a directory")
            self._folder = folder

    @property
    def folder(self) -> Optional[Path]:
        with self._folder_lock:
            return self._folder

    @property
    def folder_string(self) -> Optional[str]:
        folder = self.folder
        return str(folder) if folder is not None else None

    def set_folder(self, value: os.PathLike[str] | str) -> Path:
        # 使用非 strict resolve：Windows 某些受控目录会拒绝 realpath 的严格查询，
        # 但目录本身仍可正常读写。is_dir() 仍会验证用户实际选中的路径。
        folder = Path(value).expanduser().resolve()
        if not folder.is_dir():
            raise ApiError("选择的路径不是目录", "invalid_folder", 400)
        with self._folder_lock:
            self._folder = folder
        return folder

    def _select_folder_on_main_thread(self) -> Optional[Path]:
        if self.folder_selector is not None:
            selected = self.folder_selector(self.folder)
        else:
            try:
                import tkinter as tk
                from tkinter import filedialog
            except ImportError as exc:  # pragma: no cover - depends on Python installation
                raise ApiError("当前环境不支持目录选择", "folder_picker_unavailable", 500) from exc
            root = tk.Tk()
            try:
                root.withdraw()
                root.attributes("-topmost", True)
                selected = filedialog.askdirectory(
                    parent=root,
                    title="选择图片保存目录",
                    initialdir=self.folder_string or str(Path.home()),
                    mustexist=True,
                )
            finally:
                root.destroy()
        if not selected:
            return None
        return self.set_folder(selected)

    def _open_folder_on_main_thread(self) -> None:
        folder = self.folder
        if folder is None:
            raise ApiError("请先选择图片保存目录", "folder_not_selected", 409)
        if not folder.is_dir():
            raise ApiError("保存目录不可用", "invalid_folder", 409)
        if self.folder_opener is not None:
            self.folder_opener(folder)
            return
        try:
            if os.name == "nt":
                os.startfile(os.fspath(folder))  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", os.fspath(folder)], close_fds=True)
            else:
                subprocess.Popen(["xdg-open", os.fspath(folder)], close_fds=True)
        except OSError as exc:
            raise ApiError("无法打开保存目录", "open_folder_failed", 500) from exc

    def process_main_thread_tasks(self, limit: int = 8) -> int:
        if threading.current_thread() is not threading.main_thread():
            raise RuntimeError("主线程窗口任务只能在主线程处理")
        processed = 0
        while processed < limit:
            request = self.dispatcher.get_nowait()
            if request is None:
                break
            try:
                if request.action == "select_folder":
                    selected = self._select_folder_on_main_thread()
                    request.result = {
                        "folder": str(selected) if selected is not None else self.folder_string,
                        **({} if selected is not None else {"cancelled": True}),
                    }
                elif request.action == "open_folder":
                    self._open_folder_on_main_thread()
                    request.result = {"folder": self.folder_string}
                else:
                    raise ApiError("未知窗口操作", "unknown_ui_action", 500)
            except BaseException as exc:  # ensure the waiting HTTP request is released
                request.error = exc
            finally:
                request.event.set()
            processed += 1
        return processed

    def create_http_server(self, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> http.server.ThreadingHTTPServer:
        if host not in {DEFAULT_HOST, "localhost"}:
            raise ValueError("本机服务只能绑定回环地址")
        server = http.server.ThreadingHTTPServer((host, port), RequestHandler)
        server.daemon_threads = True
        server.app = self  # type: ignore[attr-defined]
        return server

    def serve_main_loop(self, stop_event: threading.Event) -> None:
        while not stop_event.is_set():
            self.process_main_thread_tasks()
            stop_event.wait(0.05)

    def save_png(self, filename: str, data: bytes, request_id: Optional[str] = None) -> dict[str, str]:
        folder = self.folder
        if folder is None:
            raise ApiError("请先选择图片保存目录", "folder_not_selected", 409)
        try:
            folder = folder.resolve()
            if not folder.is_dir():
                raise ApiError("保存目录不可用", "invalid_folder", 409)
        except OSError as exc:
            raise ApiError("保存目录不可用", "invalid_folder", 409) from exc

        digest = hashlib.sha256(data).hexdigest()
        size = len(data)
        with self._save_lock:
            if request_id:
                previous = self._save_records.get(request_id)
                if previous is not None:
                    previous_path, previous_digest, previous_filename = previous
                    previous_file = Path(previous_path)
                    if previous_digest == digest and _same_file_bytes(previous_file, digest, size):
                        return {"path": str(previous_file), "filename": previous_filename}

            attempt = 1
            while True:
                candidate_name = filename if attempt == 1 else _alternate_filename(filename, attempt)
                candidate = folder / candidate_name
                if _path_occupied(candidate):
                    if attempt == 1 and _same_file_bytes(candidate, digest, size):
                        result = {"path": str(candidate), "filename": candidate_name}
                        if request_id:
                            self._remember_save(request_id, result, digest)
                        return result
                    attempt += 1
                    continue

                temporary_path: Optional[Path] = None
                try:
                    fd, temporary_name = tempfile.mkstemp(prefix=".time-echo-", suffix=".tmp", dir=folder)
                    temporary_path = Path(temporary_name)
                    with os.fdopen(fd, "wb") as stream:
                        stream.write(data)
                        stream.flush()
                        os.fsync(stream.fileno())
                    # 同目录 hard link 的建立是原子的，而且目标已存在时不会覆盖。
                    os.link(os.fspath(temporary_path), os.fspath(candidate))
                except FileExistsError:
                    attempt += 1
                    continue
                except OSError as exc:
                    raise ApiError("PNG 原子保存失败", "save_failed", 500) from exc
                finally:
                    if temporary_path is not None:
                        try:
                            temporary_path.unlink(missing_ok=True)
                        except OSError:
                            pass

                result = {"path": str(candidate), "filename": candidate_name}
                if request_id:
                    self._remember_save(request_id, result, digest)
                return result

    def _remember_save(self, request_id: str, result: dict[str, str], digest: str) -> None:
        self._save_records.pop(request_id, None)
        self._save_records[request_id] = (result["path"], digest, result["filename"])
        while len(self._save_records) > MAX_IDEMPOTENCY_RECORDS:
            self._save_records.popitem(last=False)


class RequestHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "TimeEchoLocal/1.0"

    def setup(self):
        super().setup()
        self._cors_origin: Optional[str] = None

    @property
    def app(self) -> LocalApp:
        return self.server.app  # type: ignore[attr-defined]

    @property
    def server_port(self) -> int:
        return int(self.server.server_address[1])

    def log_message(self, format: str, *args) -> None:
        # 请求体中的图片 URL 不会被记录；为避免未来改动把敏感参数写入日志，这里不记 API 请求。
        return

    def _request_host(self) -> str:
        host = self.headers.get("Host")
        if not host:
            raise ApiError("请求主机无效", "invalid_host", 403)
        return host.lower().rstrip(".")

    def _validate_api_meta(self) -> Optional[str]:
        host = self._request_host()
        allowed_hosts = {
            f"127.0.0.1:{self.server_port}",
            f"localhost:{self.server_port}",
        }
        if host not in allowed_hosts:
            raise ApiError("请求主机不受信任", "invalid_host", 403)

        origin = self.headers.get("Origin")
        if origin is None:
            self._cors_origin = None
            return None
        if origin == "null":
            self._cors_origin = "null"
            return origin
        allowed_origins = {
            f"http://127.0.0.1:{self.server_port}",
            f"http://localhost:{self.server_port}",
        }
        if origin not in allowed_origins:
            raise ApiError("请求来源不受信任", "invalid_origin", 403)
        if origin.removeprefix("http://").lower() != host:
            raise ApiError("请求来源与主机不匹配", "invalid_origin", 403)
        self._cors_origin = origin
        return origin

    def _token_is_valid(self) -> bool:
        supplied = self.headers.get(LOCAL_TOKEN_HEADER)
        return bool(supplied) and secrets.compare_digest(str(supplied), self.app.token)

    def _require_token(self) -> None:
        if not self._token_is_valid():
            raise ApiError("本机访问令牌无效或缺失", "invalid_token", 401)

    def _authorize(self) -> str:
        origin = self._validate_api_meta()
        self._require_token()
        return origin or ""

    def _send_json(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if self._cors_origin is not None:
            self.send_header("Access-Control-Allow-Origin", self._cors_origin)
            self.send_header("Vary", "Origin")
        self.end_headers()
        self.wfile.write(body)

    def _send_bytes(self, body: bytes, mime: str, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if self._cors_origin is not None:
            self.send_header("Access-Control-Allow-Origin", self._cors_origin)
            self.send_header("Vary", "Origin")
        self.end_headers()
        self.wfile.write(body)

    def _error(self, error: ApiError) -> None:
        try:
            self._send_json({"error": {"message": error.message, "code": error.code}}, error.status)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True

    def _read_json(self, max_bytes: int = MAX_JSON_BODY) -> dict[str, Any]:
        length = self.headers.get("Content-Length")
        if length is None:
            raise ApiError("请求体长度缺失", "invalid_request", 400)
        try:
            size = int(length)
        except ValueError as exc:
            raise ApiError("请求体长度无效", "invalid_request", 400) from exc
        if size < 0 or size > max_bytes:
            raise ApiError("请求体过大", "request_too_large", 413)
        raw = self.rfile.read(size)
        if len(raw) != size:
            raise ApiError("请求体不完整", "invalid_request", 400)
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ApiError("请求体不是有效 JSON", "invalid_json", 400) from exc
        if not isinstance(parsed, dict):
            raise ApiError("请求体必须是 JSON 对象", "invalid_json", 400)
        return parsed

    def _serve_static(self) -> None:
        raw_path = urlsplit(self.path).path
        if raw_path == "/" or raw_path == "":
            filename = "index.html"
        elif raw_path.startswith("/"):
            filename = unquote(raw_path[1:])
        else:
            filename = unquote(raw_path)
        if filename not in STATIC_FILES:
            self._send_json({"error": {"message": "资源不存在", "code": "not_found"}}, 404)
            return
        target = (self.app.static_root / filename).resolve()
        try:
            target.relative_to(self.app.static_root)
        except ValueError:
            self._send_json({"error": {"message": "资源不存在", "code": "not_found"}}, 404)
            return
        if not target.is_file():
            self._send_json({"error": {"message": "资源不存在", "code": "not_found"}}, 404)
            return
        body = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", STATIC_FILES[filename])
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_HEAD(self) -> None:
        try:
            if urlsplit(self.path).path.startswith("/api/"):
                raise ApiError("不支持此请求方法", "method_not_allowed", 405)
            self._serve_static()
        except ApiError as error:
            self._error(error)

    def do_GET(self) -> None:
        try:
            path = urlsplit(self.path).path
            if not path.startswith("/api/"):
                self._serve_static()
                return
            if path == "/api/session":
                origin = self._validate_api_meta()
                # HTTP 页面首次连接时，浏览器对同源 GET 通常不会发送 Origin；
                # Origin:null 则代表 file:// 页面，必须依靠启动链接里的 token。
                if origin == "null":
                    self._require_token()
                elif self.headers.get(LOCAL_TOKEN_HEADER) and not self._token_is_valid():
                    self._require_token()
                self._send_json({"token": self.app.token, "folder": self.app.folder_string, "app": APP_NAME})
                return
            if path == "/api/health":
                self._authorize()
                self._send_json({"folder": self.app.folder_string, "app": APP_NAME})
                return
            self._authorize()
            raise ApiError("API 不存在", "not_found", 404)
        except ApiError as error:
            self._error(error)
        except Exception:
            self._error(ApiError("本机服务内部错误", "internal_error", 500))

    def do_OPTIONS(self) -> None:
        try:
            path = urlsplit(self.path).path
            if not path.startswith("/api/"):
                raise ApiError("不支持此请求方法", "method_not_allowed", 405)
            self._validate_api_meta()
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.send_header("Cache-Control", "no-store")
            if self._cors_origin is not None:
                self.send_header("Access-Control-Allow-Origin", self._cors_origin)
                self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Local-Token")
                self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
                self.send_header("Access-Control-Max-Age", "300")
                self.send_header("Vary", "Origin")
            self.end_headers()
        except ApiError as error:
            self._error(error)

    def do_POST(self) -> None:
        try:
            path = urlsplit(self.path).path
            if not path.startswith("/api/"):
                raise ApiError("不支持此请求方法", "method_not_allowed", 405)
            if path == "/api/session":
                raise ApiError("不支持此请求方法", "method_not_allowed", 405)
            self._authorize()
            if path == "/api/select-folder":
                self._read_json(max_bytes=16 * 1024)
                result = self.app.dispatcher.submit("select_folder")
                self._send_json(result)
                return
            if path == "/api/open-folder":
                self._read_json(max_bytes=16 * 1024)
                result = self.app.dispatcher.submit("open_folder")
                self._send_json(result)
                return
            if path == "/api/image":
                payload = self._read_json(max_bytes=64 * 1024)
                data, mime = _download_image(payload.get("url"))
                self._send_bytes(data, mime)
                return
            if path == "/api/save":
                payload = self._read_json()
                filename = _validate_filename(payload.get("filename"))
                request_id = payload.get("requestId")
                if request_id is not None:
                    if not isinstance(request_id, str) or not request_id or len(request_id) > MAX_REQUEST_ID_LENGTH:
                        raise ApiError("requestId 无效", "invalid_request_id", 400)
                data = _decode_png_data_url(payload.get("dataUrl"))
                self._send_json(self.app.save_png(filename, data, request_id))
                return
            raise ApiError("API 不存在", "not_found", 404)
        except ApiError as error:
            self._error(error)
        except Exception:
            self._error(ApiError("本机服务内部错误", "internal_error", 500))

    def send_error(self, code, message=None, explain=None):
        # BaseHTTPRequestHandler 会对未知方法调用此方法；API 仍保持统一 JSON 格式。
        if urlsplit(self.path).path.startswith("/api/"):
            self._error(ApiError(message or "请求失败", "http_error", code))
            return
        super().send_error(code, message, explain)


def _session_url(index_path: Path, port: int, token: str) -> str:
    return f"{index_path.resolve().as_uri()}#local={port}:{token}"


def _probe_existing_instance(port: int) -> Optional[str]:
    """发现同应用实例时仅取受保护启动 token，不记录或打印 token。"""
    url = f"http://127.0.0.1:{port}/api/session"
    request = Request(
        url,
        headers={
            "Host": f"127.0.0.1:{port}",
            "Origin": f"http://127.0.0.1:{port}",
            "Accept": "application/json",
        },
    )
    try:
        with build_opener(ProxyHandler({})).open(request, timeout=1.5) as response:
            payload = json.loads(response.read().decode("utf-8"))
        if payload.get("app") == APP_NAME and isinstance(payload.get("token"), str) and payload["token"]:
            return payload["token"]
    except Exception:
        return None
    return None


def _open_browser(index_path: Path, port: int, token: str) -> None:
    webbrowser.open(_session_url(index_path, port, token), new=2)


def _status_print(message: str, *, error: bool = False) -> None:
    """兼容 pythonw：无控制台时静默处理 stdout/stderr，而不让服务退出。"""
    stream = sys.stderr if error else sys.stdout
    if stream is None:
        return
    try:
        print(message, file=stream, flush=True)
    except (OSError, AttributeError):
        pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="时光对照本机保存服务")
    parser.add_argument("--host", default=DEFAULT_HOST, choices=[DEFAULT_HOST, "localhost"])
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--output-dir", help="测试或预设的图片保存目录")
    parser.add_argument("--no-browser", action="store_true", help="启动服务但不打开浏览器")
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if not 0 <= args.port <= 65535:
        _status_print("端口范围必须是 0-65535", error=True)
        return 2
    try:
        app = LocalApp(output_dir=args.output_dir)
        httpd = app.create_http_server(args.host, args.port)
    except OSError as exc:
        if exc.errno in {98, 48, 10048}:
            existing_token = _probe_existing_instance(args.port)
            if existing_token:
                if not args.no_browser:
                    _open_browser(Path(__file__).resolve().parent / "index.html", args.port, existing_token)
                _status_print("已发现正在运行的时光对照工具，已复用现有实例。")
                return 0
            _status_print("目标端口已有其它服务，未终止该服务。", error=True)
            return 1
        _status_print("本机服务启动失败。", error=True)
        return 1

    actual_port = int(httpd.server_address[1])
    stop_event = threading.Event()
    server_thread = threading.Thread(target=httpd.serve_forever, name="time-echo-http", daemon=True)
    server_thread.start()
    if not args.no_browser:
        try:
            _open_browser(Path(__file__).resolve().parent / "index.html", actual_port, app.token)
        except Exception:
            # 浏览器打开失败不应影响已启动的本机 API。
            _status_print("浏览器打开失败，请手动打开项目中的 index.html。", error=True)
    _status_print(f"时光对照本机服务已启动（端口 {actual_port}）。")
    exit_code = 0
    try:
        app.serve_main_loop(stop_event)
    except KeyboardInterrupt:
        pass
    except Exception:
        _status_print("本机服务运行失败。", error=True)
        exit_code = 1
    finally:
        stop_event.set()
        httpd.shutdown()
        httpd.server_close()
        server_thread.join(timeout=2)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
