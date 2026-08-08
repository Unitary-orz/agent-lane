from __future__ import annotations

import base64
import hashlib
import json
import os
import socket
import tempfile
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Callable

import pytest

from agent_lane.daemon_transport import (
    DaemonSocketError,
    DaemonVersionInfo,
    UnixWebSocketConnection,
    WebSocketConnectError,
    WebSocketHandshakeError,
    WebSocketProtocolError,
    detect_local_app_transport,
    parse_daemon_version_output,
    parse_local_app_transport,
    probe_shared_daemon,
    validate_daemon_socket,
)


WEBSOCKET_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


@pytest.fixture
def short_socket_path():
    with tempfile.TemporaryDirectory(prefix="al-", dir="/tmp") as directory:
        yield Path(directory) / "s"


def read_exact(conn: socket.socket, count: int) -> bytes:
    data = bytearray()
    while len(data) < count:
        chunk = conn.recv(count - len(data))
        if not chunk:
            raise AssertionError("unexpected EOF from WebSocket client")
        data.extend(chunk)
    return bytes(data)


def read_http_request(conn: socket.socket) -> bytes:
    data = bytearray()
    while b"\r\n\r\n" not in data:
        chunk = conn.recv(4096)
        if not chunk:
            raise AssertionError("unexpected EOF during HTTP Upgrade")
        data.extend(chunk)
    return bytes(data)


def websocket_key(request: bytes) -> str:
    for line in request.decode("ascii").split("\r\n"):
        if line.casefold().startswith("sec-websocket-key:"):
            return line.split(":", 1)[1].strip()
    raise AssertionError("WebSocket request omitted Sec-WebSocket-Key")


def send_handshake(
    conn: socket.socket,
    request: bytes,
    *,
    accept: str | None = None,
) -> None:
    key = websocket_key(request)
    expected = base64.b64encode(
        hashlib.sha1((key + WEBSOCKET_GUID).encode("ascii")).digest()
    ).decode("ascii")
    response_accept = expected if accept is None else accept
    conn.sendall(
        (
            "HTTP/1.1 101 Switching Protocols\r\n"
            "Upgrade: websocket\r\n"
            "Connection: keep-alive, Upgrade\r\n"
            f"Sec-WebSocket-Accept: {response_accept}\r\n"
            "\r\n"
        ).encode("ascii")
    )


def send_server_frame(
    conn: socket.socket,
    opcode: int,
    payload: bytes,
    *,
    fin: bool = True,
) -> None:
    first = (0x80 if fin else 0) | opcode
    length = len(payload)
    if length < 126:
        header = bytes((first, length))
    elif length <= 0xFFFF:
        header = bytes((first, 126)) + length.to_bytes(2, "big")
    else:
        header = bytes((first, 127)) + length.to_bytes(8, "big")
    conn.sendall(header + payload)


def read_client_frame(
    conn: socket.socket,
) -> tuple[bool, int, bool, bytes]:
    first, second = read_exact(conn, 2)
    fin = bool(first & 0x80)
    opcode = first & 0x0F
    masked = bool(second & 0x80)
    length = second & 0x7F
    if length == 126:
        length = int.from_bytes(read_exact(conn, 2), "big")
    elif length == 127:
        length = int.from_bytes(read_exact(conn, 8), "big")
    mask = read_exact(conn, 4) if masked else b""
    payload = read_exact(conn, length)
    if masked:
        payload = bytes(
            value ^ mask[index % 4] for index, value in enumerate(payload)
        )
    return fin, opcode, masked, payload


class FakeUnixWebSocketServer:
    def __init__(
        self,
        path: Path,
        handler: Callable[[socket.socket], None],
    ) -> None:
        self.path = path
        self.handler = handler
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.listener.bind(os.fspath(path))
        self.listener.listen(1)
        self.error: BaseException | None = None
        self.thread = threading.Thread(target=self._serve, daemon=True)

    def _serve(self) -> None:
        try:
            conn, _address = self.listener.accept()
            with conn:
                conn.settimeout(3)
                self.handler(conn)
        except BaseException as exc:  # test helper must relay thread failures
            self.error = exc
        finally:
            self.listener.close()

    def __enter__(self) -> "FakeUnixWebSocketServer":
        self.thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.thread.join(timeout=4)
        if self.thread.is_alive():
            self.listener.close()
            raise AssertionError("fake WebSocket server did not stop")
        if self.error is not None:
            raise self.error


def test_websocket_handshake_masks_client_text_and_receives_json(
    short_socket_path,
):
    socket_path = short_socket_path
    observed: dict[str, object] = {}

    def handler(conn: socket.socket) -> None:
        request = read_http_request(conn)
        observed["request"] = request
        send_handshake(conn, request)
        fin, opcode, masked, payload = read_client_frame(conn)
        observed["frame"] = (fin, opcode, masked, payload)
        send_server_frame(conn, 0x1, b'{"ok":true}')

    with FakeUnixWebSocketServer(socket_path, handler):
        connection = UnixWebSocketConnection.connect(socket_path)
        assert connection._socket.gettimeout() is None
        connection.send_json({"method": "initialize"})
        assert connection.recv_json() == {"ok": True}
        connection.close()

    assert b"GET / HTTP/1.1\r\n" in observed["request"]
    fin, opcode, masked, payload = observed["frame"]
    assert (fin, opcode, masked) == (True, 0x1, True)
    assert json.loads(payload) == {"method": "initialize"}


def test_websocket_reassembles_fragments_and_answers_ping(short_socket_path):
    socket_path = short_socket_path
    observed: dict[str, object] = {}

    def handler(conn: socket.socket) -> None:
        request = read_http_request(conn)
        send_handshake(conn, request)
        send_server_frame(conn, 0x1, b"hel", fin=False)
        send_server_frame(conn, 0x9, b"probe")
        send_server_frame(conn, 0x0, b"lo")
        observed["pong"] = read_client_frame(conn)

    with FakeUnixWebSocketServer(socket_path, handler):
        connection = UnixWebSocketConnection.connect(socket_path)
        assert connection.recv_text() == "hello"
        connection.close()

    fin, opcode, masked, payload = observed["pong"]
    assert (fin, opcode, masked, payload) == (True, 0xA, True, b"probe")


def test_websocket_clean_close_is_acknowledged_and_returns_none(
    short_socket_path,
):
    socket_path = short_socket_path
    observed: dict[str, object] = {}
    close_payload = (1000).to_bytes(2, "big") + b"done"

    def handler(conn: socket.socket) -> None:
        request = read_http_request(conn)
        send_handshake(conn, request)
        send_server_frame(conn, 0x8, close_payload)
        observed["close"] = read_client_frame(conn)

    with FakeUnixWebSocketServer(socket_path, handler):
        connection = UnixWebSocketConnection.connect(socket_path)
        assert connection.recv_text() is None
        assert connection.closed is True

    fin, opcode, masked, payload = observed["close"]
    assert (fin, opcode, masked, payload) == (
        True,
        0x8,
        True,
        close_payload,
    )


def test_websocket_rejects_invalid_handshake(short_socket_path):
    socket_path = short_socket_path

    def handler(conn: socket.socket) -> None:
        request = read_http_request(conn)
        send_handshake(conn, request, accept="not-the-right-value")

    with FakeUnixWebSocketServer(socket_path, handler):
        with pytest.raises(WebSocketHandshakeError, match="Accept"):
            UnixWebSocketConnection.connect(socket_path)


def test_websocket_wraps_unix_connect_failure(short_socket_path):
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(os.fspath(short_socket_path))
    listener.close()

    with pytest.raises(WebSocketConnectError, match="could not connect"):
        UnixWebSocketConnection.connect(short_socket_path)


def test_websocket_enforces_message_size_limit(short_socket_path):
    socket_path = short_socket_path

    def handler(conn: socket.socket) -> None:
        request = read_http_request(conn)
        send_handshake(conn, request)
        send_server_frame(conn, 0x1, b"123456")

    with FakeUnixWebSocketServer(socket_path, handler):
        connection = UnixWebSocketConnection.connect(
            socket_path,
            max_message_bytes=5,
        )
        with pytest.raises(WebSocketProtocolError, match="limit"):
            connection.recv_text()
        connection.close()


def test_validate_daemon_socket_rejects_non_socket_and_wrong_owner(
    short_socket_path,
):
    regular_file = short_socket_path.parent / "regular"
    regular_file.write_text("not a socket", encoding="utf-8")

    with pytest.raises(DaemonSocketError, match="not a Unix socket"):
        validate_daemon_socket(regular_file)

    socket_path = short_socket_path
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(os.fspath(socket_path))
    try:
        with pytest.raises(DaemonSocketError, match="unexpected user"):
            validate_daemon_socket(socket_path, expected_uid=os.getuid() + 1)
    finally:
        listener.close()


def test_parse_and_validate_daemon_version_output(tmp_path):
    socket_path = tmp_path / "daemon.sock"
    output = json.dumps(
        {
            "socketPath": str(socket_path),
            "cliVersion": "0.144.2",
            "appServerVersion": "0.144.2",
        }
    )

    info = parse_daemon_version_output(f"diagnostic\n{output}\n")

    assert info == DaemonVersionInfo(
        cli_version="0.144.2",
        app_server_version="0.144.2",
        socket_path=socket_path,
    )
def test_probe_shared_daemon_accepts_version_difference_and_validates_socket(
    short_socket_path,
):
    socket_path = short_socket_path
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(os.fspath(socket_path))
    calls: list[tuple[object, object]] = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "socketPath": str(socket_path),
                    "cliVersion": "99.0.0",
                    "appServerVersion": "99.1.0",
                }
            ),
            stderr="",
        )

    try:
        info = probe_shared_daemon(
            "/opt/codex",
            run_command=fake_run,
        )
    finally:
        listener.close()

    assert info.socket_path == socket_path
    assert info.cli_version == "99.0.0"
    assert info.app_server_version == "99.1.0"
    assert calls == [
        (
            ["/opt/codex", "app-server", "daemon", "version"],
            {
                "capture_output": True,
                "text": True,
                "timeout": 2.5,
                "check": False,
            },
        )
    ]


def test_detect_local_app_transport_uses_latest_matching_pid_log(tmp_path):
    log_root = tmp_path / "logs"
    day = log_root / "2026" / "07" / "16"
    day.mkdir(parents=True)
    first = day / "codex-desktop-a-111-t0-i1-000000-0.log"
    second = day / "codex-desktop-a-111-t0-i1-000000-1.log"
    other_pid = day / "codex-desktop-a-222-t0-i1-000000-0.log"
    first.write_text(
        "\n".join(
            [
                "2026-07-16T01:00:00.000Z info [AppServerConnection] "
                "app_server_connection.state_changed hostId=local "
                "hasConnection=true initialized=true next=connected "
                "transport=stdio",
                "2026-07-16T01:01:00.000Z info [AppServerConnection] "
                "app_server_connection.state_changed hostId=remote-machine "
                "hasConnection=true initialized=true next=connected "
                "transport=websocket",
            ]
        ),
        encoding="utf-8",
    )
    second.write_text(
        "2026-07-16T02:00:00.000Z info [AppServerConnection] "
        "app_server_connection.state_changed hostId=local "
        "hasConnection=true initialized=true next=connected "
        "transport=websocket\n",
        encoding="utf-8",
    )
    other_pid.write_text(
        "2026-07-16T03:00:00.000Z info [AppServerConnection] "
        "app_server_connection.state_changed hostId=local "
        "hasConnection=true initialized=true next=connected "
        "transport=stdio\n",
        encoding="utf-8",
    )

    observation = detect_local_app_transport(log_root, pid=111)

    assert observation is not None
    assert observation.transport == "websocket"
    assert observation.connected is True
    assert observation.state == "connected"
    assert observation.log_path == second
    assert observation.line_number == 1
    assert observation.timestamp == "2026-07-16T02:00:00.000Z"


def test_parse_local_app_transport_ignores_remote_and_unrelated_lines():
    assert (
        parse_local_app_transport(
            "2026-07-16T01:00:00.000Z info [AppServerConnection] "
            "app_server_connection.state_changed hostId=local "
            "hasConnection=true initialized=true next=connected "
            "transport=websocket"
        )
        == "websocket"
    )
    assert (
        parse_local_app_transport(
            "2026-07-16T01:00:00.000Z info [AppServerConnection] "
            "app_server_connection.state_changed hostId=remote "
            "hasConnection=true initialized=true next=connected "
            "transport=websocket"
        )
        is None
    )
    assert (
        parse_local_app_transport(
            "2026-07-16T01:00:00.000Z info [electron-message-handler] "
            "hostId=local transport=stdio"
        )
        is None
    )
    assert (
        parse_local_app_transport(
            "2026-07-16T01:00:00.000Z info [electron-message-handler] "
            "user_text=[AppServerConnection] "
            "app_server_connection.state_changed hostId=local "
            "hasConnection=true initialized=true next=connected "
            "transport=websocket"
        )
        is None
    )
    assert (
        parse_local_app_transport(
            "2026-07-16T01:00:00.000Z info [AppServerConnection] "
            "app_server_connection.state_changed hostId=local "
            "hasConnection=true initialized=true next=connected "
            "transport=stdio transport=websocket"
        )
        is None
    )


def test_detect_local_app_transport_records_latest_disconnect(tmp_path):
    log_root = tmp_path / "logs"
    day = log_root / "2026" / "07" / "16"
    day.mkdir(parents=True)
    path = day / "codex-desktop-a-111-t0-i1-000000-0.log"
    path.write_text(
        "\n".join(
            [
                "2026-07-16T01:00:00.000Z info [AppServerConnection] "
                "app_server_connection.state_changed hostId=local "
                "hasConnection=true initialized=true next=connected "
                "transport=websocket",
                "2026-07-16T01:01:00.000Z info [AppServerConnection] "
                "app_server_connection.state_changed hostId=local "
                "hasConnection=false initialized=false next=disconnected "
                "transport=websocket",
            ]
        ),
        encoding="utf-8",
    )

    observation = detect_local_app_transport(log_root, pid=111)

    assert observation is not None
    assert observation.transport == "websocket"
    assert observation.connected is False
    assert observation.state == "disconnected"
