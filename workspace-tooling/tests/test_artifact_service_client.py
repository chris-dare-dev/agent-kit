from __future__ import annotations

import json
import socketserver
import sys
import tempfile
import threading
import unittest
from contextlib import contextmanager
from pathlib import Path
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPT_DIR))

import artifact_service_client as client  # noqa: E402


class ReplyingUnixHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        request_line = self.rfile.readline().decode("ascii", errors="strict").strip()
        headers: dict[str, str] = {}
        while True:
            line = self.rfile.readline()
            if line in (b"\r\n", b"\n", b""):
                break
            name, value = line.decode("ascii", errors="strict").split(":", 1)
            headers[name.casefold()] = value.strip()
        length = int(headers.get("content-length", "0"))
        body = self.rfile.read(length)
        self.server.requests.append(  # type: ignore[attr-defined]
            {
                "request_line": request_line,
                "headers": headers,
                "body": body,
            }
        )
        status = self.server.response_status  # type: ignore[attr-defined]
        response_body = self.server.response_body  # type: ignore[attr-defined]
        reason = "OK" if status == 200 else "Unavailable"
        self.wfile.write(
            (
                f"HTTP/1.1 {status} {reason}\r\n"
                "Content-Type: application/json\r\n"
                f"Content-Length: {len(response_body)}\r\n"
                "Connection: close\r\n"
                "\r\n"
            ).encode("ascii")
            + response_body
        )


class ReplyingUnixServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True

    def __init__(self, socket_path: Path, *, status: int, body: bytes):
        self.requests: list[dict[str, Any]] = []
        self.response_status = status
        self.response_body = body
        super().__init__(str(socket_path), ReplyingUnixHandler)


@contextmanager
def running_reply_server(*, status: int = 200, body: bytes | None = None):
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        root.chmod(0o700)
        socket_path = root / "artifact-memory.sock"
        server = ReplyingUnixServer(
            socket_path,
            status=status,
            body=body if body is not None else b'{"ok":true}\n',
        )
        socket_path.chmod(0o600)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield socket_path, server
        finally:
            server.shutdown()
            server.server_close()
            socket_path.unlink(missing_ok=True)
            thread.join(timeout=5)


class ArtifactServiceClientTests(unittest.TestCase):
    def test_posts_json_over_private_uds_without_bearer_header(self) -> None:
        with running_reply_server() as (socket_path, server):
            result = client.post_json(
                socket_path=socket_path,
                route="/v1/search",
                payload={"query": "artifact ownership"},
            )

        self.assertEqual(result, {"ok": True})
        self.assertEqual(server.requests[0]["request_line"], "POST /v1/search HTTP/1.1")
        self.assertNotIn("authorization", server.requests[0]["headers"])
        self.assertEqual(
            json.loads(server.requests[0]["body"]),
            {"query": "artifact ownership"},
        )

    def test_rejects_non_socket_before_connecting(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            root.chmod(0o700)
            socket_path = root / "artifact-memory.sock"
            socket_path.write_text("not a socket", encoding="utf-8")
            socket_path.chmod(0o600)

            with self.assertRaisesRegex(
                client.ArtifactServiceError,
                "service socket is unavailable",
            ):
                client.post_json(
                    socket_path=socket_path,
                    route="/v1/status",
                    payload={},
                )

    def test_rejects_oversized_response(self) -> None:
        oversized = b"{" + b'"payload":"' + b"x" * client.MAX_RESPONSE_BYTES + b'"}'
        with running_reply_server(body=oversized) as (socket_path, _server):
            with self.assertRaisesRegex(
                client.ArtifactServiceError,
                "response exceeds 2 MiB",
            ):
                client.post_json(
                    socket_path=socket_path,
                    route="/v1/status",
                    payload={},
                )


if __name__ == "__main__":
    unittest.main()
