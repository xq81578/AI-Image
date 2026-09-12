from __future__ import annotations

import base64
from http.client import HTTPConnection
import io
import json
from pathlib import Path
import shutil
import threading
import unittest

from PIL import Image

import server


class LocalSaveServerTests(unittest.TestCase):
    def setUp(self) -> None:
        test_root = Path(__file__).resolve().parent.parent / "workspace-test-dir"
        test_root.mkdir(parents=True, exist_ok=True)
        self.output_dir = test_root / self._testMethodName
        shutil.rmtree(self.output_dir, ignore_errors=True)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.app = server.LocalApp(output_dir=self.output_dir)
        self.httpd = self.app.create_http_server(port=0)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.httpd.server_address[1]
        self.origin = f"http://127.0.0.1:{self.port}"

    def tearDown(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2)
        shutil.rmtree(self.output_dir, ignore_errors=True)

    def request(
        self,
        method: str,
        path: str,
        payload: dict | None = None,
        *,
        token: str | None = None,
        origin: str | None = None,
        host: str | None = None,
    ) -> tuple[int, dict[str, str], bytes]:
        connection = HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers = {
            "Host": host or f"127.0.0.1:{self.port}",
            "Origin": self.origin if origin is None else origin,
        }
        body = None
        if token is not None:
            headers[server.LOCAL_TOKEN_HEADER] = token
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
            headers["Content-Length"] = str(len(body))
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        result = response.read()
        response_headers = {key.lower(): value for key, value in response.getheaders()}
        status = response.status
        connection.close()
        return status, response_headers, result

    @staticmethod
    def png_data(color: tuple[int, int, int]) -> tuple[bytes, str]:
        output = io.BytesIO()
        Image.new("RGB", (8, 8), color).save(output, format="PNG")
        data = output.getvalue()
        data_url = "data:image/png;base64," + base64.b64encode(data).decode("ascii")
        return data, data_url

    def test_session_and_strict_auth_metadata(self) -> None:
        status, headers, body = self.request("GET", "/api/session")
        self.assertEqual(status, 200)
        session = json.loads(body)
        self.assertEqual(session["app"], server.APP_NAME)
        self.assertEqual(headers["access-control-allow-origin"], self.origin)

        token = session["token"]
        status, _, body = self.request("GET", "/api/health")
        self.assertEqual(status, 401)
        self.assertEqual(json.loads(body)["error"]["code"], "invalid_token")

        status, _, body = self.request("GET", "/api/health", token="bad-token")
        self.assertEqual(status, 401)
        self.assertEqual(json.loads(body)["error"]["code"], "invalid_token")

        status, _, body = self.request("GET", "/api/health", token=token)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["folder"], str(self.output_dir.resolve()))

        status, _, body = self.request("GET", "/api/health", token=token, origin="https://attacker.invalid")
        self.assertEqual(status, 403)
        self.assertEqual(json.loads(body)["error"]["code"], "invalid_origin")

        status, _, body = self.request("GET", "/api/health", token=token, host="evil.example")
        self.assertEqual(status, 403)
        self.assertEqual(json.loads(body)["error"]["code"], "invalid_host")

        status, _, body = self.request("GET", "/api/session", origin="null")
        self.assertEqual(status, 401)
        status, headers, body = self.request("GET", "/api/session", origin="null", token=token)
        self.assertEqual(status, 200)
        self.assertEqual(headers["access-control-allow-origin"], "null")
        self.assertEqual(json.loads(body)["token"], token)

    def test_save_is_png_atomic_and_request_idempotent(self) -> None:
        _, _, session_body = self.request("GET", "/api/session")
        token = json.loads(session_body)["token"]
        first_data, first_url = self.png_data((25, 80, 120))
        second_data, second_url = self.png_data((180, 40, 80))

        request = {"filename": "怀旧照片.png", "dataUrl": first_url, "requestId": "job-1"}
        status, _, body = self.request("POST", "/api/save", request, token=token)
        self.assertEqual(status, 200)
        first_result = json.loads(body)
        first_path = Path(first_result["path"])
        self.assertTrue(first_path.is_file())
        self.assertEqual(first_path.read_bytes(), first_data)

        status, _, body = self.request("POST", "/api/save", request, token=token)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), first_result)

        changed_request = {**request, "dataUrl": second_url}
        status, _, body = self.request("POST", "/api/save", changed_request, token=token)
        self.assertEqual(status, 200)
        second_result = json.loads(body)
        self.assertNotEqual(second_result["filename"], first_result["filename"])
        self.assertTrue(Path(second_result["path"]).is_file())
        self.assertEqual(first_path.read_bytes(), first_data)
        self.assertEqual(Path(second_result["path"]).read_bytes(), second_data)

        status, _, body = self.request("POST", "/api/save", changed_request, token=token)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), second_result)
        self.assertFalse(list(self.output_dir.glob(".time-echo-*.tmp")))

    def test_filename_token_and_png_validation(self) -> None:
        _, _, session_body = self.request("GET", "/api/session")
        token = json.loads(session_body)["token"]
        _, data_url = self.png_data((1, 2, 3))

        for filename in ("../outside.png", "folder/file.png", "CON.png", "bad?.png", "image.jpg"):
            status, _, body = self.request(
                "POST",
                "/api/save",
                {"filename": filename, "dataUrl": data_url},
                token=token,
            )
            self.assertEqual(status, 400)
            self.assertEqual(json.loads(body)["error"]["code"], "invalid_filename")

        status, _, body = self.request(
            "POST",
            "/api/save",
            {"filename": "valid.png", "dataUrl": data_url},
            token="wrong",
        )
        self.assertEqual(status, 401)
        self.assertEqual(json.loads(body)["error"]["code"], "invalid_token")

        status, _, body = self.request(
            "POST",
            "/api/save",
            {"filename": "valid.png", "dataUrl": "data:image/png;base64,not-png"},
            token=token,
        )
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body)["error"]["code"], "invalid_png")

    def test_private_image_addresses_are_rejected_before_network_access(self) -> None:
        _, _, session_body = self.request("GET", "/api/session")
        token = json.loads(session_body)["token"]
        for url in (
            "http://127.0.0.1:1/image.png",
            "http://localhost/image.png",
            "http://224.0.0.1/image.png",
            "file:///C:/secret.png",
        ):
            status, _, body = self.request("POST", "/api/image", {"url": url}, token=token)
            self.assertIn(status, (400, 413))
            error = json.loads(body)["error"]
            self.assertIn(error["code"], {"private_address", "invalid_url"})

    def test_folder_selection_is_dispatched_to_main_thread(self) -> None:
        selected = self.output_dir / "selected"
        selected.mkdir()
        app = server.LocalApp(folder_selector=lambda _current: selected)
        result: list[object] = []

        def submit() -> None:
            result.append(app.dispatcher.submit("select_folder", timeout=2))

        worker = threading.Thread(target=submit)
        worker.start()
        self.assertEqual(app.process_main_thread_tasks(), 1)
        worker.join(timeout=2)
        self.assertEqual(result, [{"folder": str(selected.resolve())}])


if __name__ == "__main__":
    unittest.main()
