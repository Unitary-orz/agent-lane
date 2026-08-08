"""Read-only shared daemon discovery and WebSocket-over-Unix transport.

The managed Codex daemon exposes app-server JSON-RPC as WebSocket text
messages over a Unix-domain socket.  This module deliberately owns neither
daemon startup nor shutdown: callers may inspect a running daemon and connect
to it, but lifecycle changes stay outside this boundary.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import socket
import stat
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Literal, Mapping, Sequence


_WEBSOCKET_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
_APP_TRANSPORTS = frozenset({"stdio", "websocket"})
_LOG_PREFIX_PATTERN = re.compile(
    r"^(?P<timestamp>\S+)\s+"
    r"(?:trace|debug|info|warn|error)\s+"
    r"\[AppServerConnection\]\s+"
    r"(?P<message>.*)$"
)
_LOG_FIELD_PATTERN = re.compile(
    r"\b([A-Za-z][A-Za-z0-9_]*)=(\"[^\"]*\"|\S+)"
)


class DaemonTransportError(RuntimeError):
    """Base failure for shared daemon discovery or transport."""


class DaemonProbeError(DaemonTransportError):
    """The running daemon could not be inspected safely."""


class DaemonSocketError(DaemonProbeError):
    """The daemon socket is absent or fails local ownership/type checks."""


class DaemonVersionError(DaemonProbeError):
    """The daemon version response is malformed or incompatible."""


class WebSocketError(DaemonTransportError):
    """Base failure for the WebSocket-over-Unix connection."""


class WebSocketHandshakeError(WebSocketError):
    """The peer rejected or returned an invalid HTTP Upgrade response."""


class WebSocketProtocolError(WebSocketError):
    """The peer sent a WebSocket frame that violates the protocol."""


class WebSocketConnectionClosed(WebSocketError):
    """The underlying socket closed without a WebSocket close frame."""


class WebSocketConnectError(WebSocketError):
    """The Unix socket could not complete the WebSocket connection."""


@dataclass(frozen=True)
class DaemonVersionInfo:
    """Identity reported by ``codex app-server daemon version``."""

    cli_version: str
    app_server_version: str
    socket_path: Path


@dataclass(frozen=True)
class AppTransportObservation:
    """Last local App transport declaration found in one process's logs."""

    transport: Literal["stdio", "websocket"]
    connected: bool
    state: str
    log_path: Path
    line_number: int
    timestamp: str | None


RunCommand = Callable[..., subprocess.CompletedProcess[str]]


def parse_daemon_version_output(output: str | bytes) -> DaemonVersionInfo:
    """Parse the JSON emitted by ``codex app-server daemon version``.

    Codex currently emits one compact JSON object.  Scanning non-empty lines
    from the end also tolerates diagnostic prefixes without accepting an
    unrelated nested object.
    """

    if isinstance(output, bytes):
        try:
            text = output.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise DaemonVersionError(
                "daemon version output is not valid UTF-8"
            ) from exc
    else:
        text = output

    payload: object | None = None
    for line in reversed(text.splitlines()):
        candidate = line.strip()
        if not candidate:
            continue
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            payload = parsed
            break
    if not isinstance(payload, dict):
        raise DaemonVersionError("daemon version output did not contain JSON")

    cli_version = payload.get("cliVersion")
    app_server_version = payload.get("appServerVersion")
    socket_path = payload.get("socketPath")
    if not isinstance(cli_version, str) or not cli_version.strip():
        raise DaemonVersionError("daemon version response omitted cliVersion")
    if not isinstance(app_server_version, str) or not app_server_version.strip():
        raise DaemonVersionError(
            "daemon version response omitted appServerVersion"
        )
    if not isinstance(socket_path, str) or not socket_path.strip():
        raise DaemonVersionError("daemon version response omitted socketPath")

    path = Path(socket_path).expanduser()
    if not path.is_absolute():
        raise DaemonVersionError("daemon socketPath must be absolute")
    return DaemonVersionInfo(
        cli_version=cli_version.strip(),
        app_server_version=app_server_version.strip(),
        socket_path=path,
    )


def validate_daemon_socket(
    socket_path: str | os.PathLike[str],
    *,
    expected_uid: int | None = None,
) -> os.stat_result:
    """Require a non-symlink Unix socket owned by the current user."""

    path = Path(socket_path)
    owner = os.getuid() if expected_uid is None else int(expected_uid)
    try:
        details = path.lstat()
    except FileNotFoundError as exc:
        raise DaemonSocketError(f"daemon socket does not exist: {path}") from exc
    except OSError as exc:
        raise DaemonSocketError(
            f"could not inspect daemon socket {path}: {exc}"
        ) from exc
    if not stat.S_ISSOCK(details.st_mode):
        raise DaemonSocketError(
            f"daemon socket path is not a Unix socket: {path}"
        )
    if details.st_uid != owner:
        raise DaemonSocketError(
            "daemon socket is owned by an unexpected user: "
            f"path={path}, expected_uid={owner}, actual_uid={details.st_uid}"
        )
    return details


def probe_shared_daemon(
    codex_bin: str = "codex",
    *,
    timeout: float = 2.5,
    expected_uid: int | None = None,
    run_command: RunCommand = subprocess.run,
) -> DaemonVersionInfo:
    """Inspect a running managed daemon without starting or stopping it.

    This parses the daemon's reported identities and verifies socket safety.
    Version differences remain diagnostic; the caller performs a real
    WebSocket upgrade and app-server ``initialize`` exchange before exposing
    the connection.
    """

    command = [codex_bin, "app-server", "daemon", "version"]
    try:
        completed = run_command(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise DaemonProbeError(
            f"could not execute daemon version probe: {exc}"
        ) from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        if len(detail) > 500:
            detail = detail[-500:]
        suffix = f": {detail}" if detail else ""
        raise DaemonProbeError(
            f"daemon version probe exited with {completed.returncode}{suffix}"
        )
    info = parse_daemon_version_output(completed.stdout)
    validate_daemon_socket(info.socket_path, expected_uid=expected_uid)
    return info


def parse_local_app_transport(
    line: str,
) -> Literal["stdio", "websocket"] | None:
    """Extract a verified connected local transport from one state line."""

    parsed = _parse_local_app_state(line)
    if parsed is None or not parsed[1]:
        return None
    return parsed[0]


def detect_local_app_transport(
    log_root: str | os.PathLike[str],
    *,
    pid: int | None = None,
) -> AppTransportObservation | None:
    """Return the last local transport declaration in ChatGPT App logs.

    Supplying the running App PID prevents an old process or a remote host log
    from being mistaken for the current local connection.
    """

    root = Path(log_root)
    try:
        paths = [
            path
            for path in root.rglob("*.log")
            if pid is None or f"-{int(pid)}-t" in path.name
        ]
    except OSError as exc:
        raise DaemonProbeError(f"could not enumerate App logs: {exc}") from exc

    def file_key(path: Path) -> tuple[int, str]:
        try:
            modified = path.stat().st_mtime_ns
        except OSError:
            modified = -1
        return modified, str(path)

    observation: AppTransportObservation | None = None
    observation_key: tuple[str, int, str, int] | None = None
    for path in sorted(paths, key=file_key):
        try:
            modified = path.stat().st_mtime_ns
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                for line_number, line in enumerate(handle, start=1):
                    parsed = _parse_local_app_state(line)
                    if parsed is None:
                        continue
                    transport, connected, state = parsed
                    first = line.split(maxsplit=1)[0] if line else ""
                    timestamp = first if first.endswith("Z") else None
                    key = (
                        timestamp or "",
                        modified,
                        str(path),
                        line_number,
                    )
                    if observation_key is not None and key < observation_key:
                        continue
                    observation_key = key
                    observation = AppTransportObservation(
                        transport=transport,
                        connected=connected,
                        state=state,
                        log_path=path,
                        line_number=line_number,
                        timestamp=timestamp,
                    )
        except OSError:
            continue
    return observation


def _parse_local_app_state(
    line: str,
) -> tuple[Literal["stdio", "websocket"], bool, str] | None:
    prefix = _LOG_PREFIX_PATTERN.match(line)
    if prefix is None:
        return None
    message = prefix.group("message")
    marker = "app_server_connection.state_changed"
    if message != marker and not message.startswith(marker + " "):
        return None
    fields: dict[str, str] = {}
    for match in _LOG_FIELD_PATTERN.finditer(message[len(marker) :]):
        key, value = match.groups()
        if key in fields:
            return None
        fields[key] = (
            value[1:-1]
            if value.startswith('"') and value.endswith('"')
            else value
        )
    transport = fields.get("transport")
    state = fields.get("next")
    if (
        fields.get("hostId") != "local"
        or transport not in _APP_TRANSPORTS
        or not state
    ):
        return None
    connected = (
        state == "connected"
        and fields.get("hasConnection") == "true"
        and fields.get("initialized") == "true"
    )
    return transport, connected, state  # type: ignore[return-value]


class UnixWebSocketConnection:
    """Minimal RFC 6455 client over an already trusted Unix socket path."""

    def __init__(
        self,
        sock: socket.socket,
        *,
        max_message_bytes: int = 16 * 1024 * 1024,
        initial_bytes: bytes = b"",
    ) -> None:
        if max_message_bytes < 1:
            raise ValueError("max_message_bytes must be positive")
        self._socket = sock
        self._max_message_bytes = int(max_message_bytes)
        self._buffer = bytearray(initial_bytes)
        self._send_lock = threading.Lock()
        self._closed = False
        self._close_sent = False

    @classmethod
    def connect(
        cls,
        socket_path: str | os.PathLike[str],
        *,
        resource: str = "/",
        host: str = "localhost",
        timeout: float = 5.0,
        max_header_bytes: int = 64 * 1024,
        max_message_bytes: int = 16 * 1024 * 1024,
        expected_uid: int | None = None,
    ) -> "UnixWebSocketConnection":
        """Validate, connect, and complete an HTTP WebSocket Upgrade."""

        if not resource.startswith("/"):
            raise ValueError("resource must start with '/'")
        if "\r" in resource or "\n" in resource:
            raise ValueError("resource may not contain newlines")
        if not host or "\r" in host or "\n" in host:
            raise ValueError("host must be a non-empty HTTP header value")
        if max_header_bytes < 256:
            raise ValueError("max_header_bytes must be at least 256")

        path = Path(socket_path)
        validate_daemon_socket(path, expected_uid=expected_uid)
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        try:
            sock.connect(os.fspath(path))
            key = base64.b64encode(secrets.token_bytes(16)).decode("ascii")
            request = (
                f"GET {resource} HTTP/1.1\r\n"
                f"Host: {host}\r\n"
                "Upgrade: websocket\r\n"
                "Connection: Upgrade\r\n"
                f"Sec-WebSocket-Key: {key}\r\n"
                "Sec-WebSocket-Version: 13\r\n"
                "\r\n"
            ).encode("ascii")
            sock.sendall(request)
            header, remainder = _read_http_upgrade(
                sock,
                max_header_bytes=max_header_bytes,
            )
            _validate_http_upgrade(header, key)
            # The timeout bounds only connect/Upgrade. JSON-RPC turns can be
            # legitimately quiet for much longer, so steady-state reads must
            # remain blocking like the existing stdio transport.
            sock.settimeout(None)
        except DaemonTransportError:
            sock.close()
            raise
        except OSError as exc:
            sock.close()
            raise WebSocketConnectError(
                f"could not connect to the daemon Unix socket: {exc}"
            ) from exc
        except Exception:
            sock.close()
            raise
        return cls(
            sock,
            max_message_bytes=max_message_bytes,
            initial_bytes=remainder,
        )

    @property
    def closed(self) -> bool:
        return self._closed

    def fileno(self) -> int:
        return self._socket.fileno()

    def send_text(self, text: str) -> None:
        if not isinstance(text, str):
            raise TypeError("WebSocket text payload must be str")
        self._send_frame(0x1, text.encode("utf-8"))

    def send_json(self, value: Mapping[str, Any] | Sequence[Any]) -> None:
        self.send_text(json.dumps(value, ensure_ascii=False))

    def recv_text(self) -> str | None:
        """Receive one complete text message, answering ping frames inline.

        A clean WebSocket close returns ``None``.  An unframed EOF raises
        :class:`WebSocketConnectionClosed` so callers can distinguish it.
        """

        fragments = bytearray()
        fragmented = False
        while True:
            fin, opcode, payload = self._recv_frame()
            if opcode >= 0x8:
                if not fin:
                    raise WebSocketProtocolError(
                        "control frames may not be fragmented"
                    )
                if len(payload) > 125:
                    raise WebSocketProtocolError(
                        "control frame payload exceeds 125 bytes"
                    )
                if opcode == 0x8:
                    if len(payload) == 1:
                        raise WebSocketProtocolError(
                            "close frame payload may not be one byte"
                        )
                    if not self._close_sent:
                        self._send_frame(0x8, payload)
                    self._closed = True
                    try:
                        self._socket.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                    self._socket.close()
                    return None
                if opcode == 0x9:
                    self._send_frame(0xA, payload)
                    continue
                if opcode == 0xA:
                    continue
                raise WebSocketProtocolError(
                    f"unsupported control opcode: {opcode}"
                )

            if opcode == 0x1:
                if fragmented:
                    raise WebSocketProtocolError(
                        "received a new text frame during fragmentation"
                    )
                fragments.extend(payload)
                _enforce_message_limit(
                    len(fragments),
                    self._max_message_bytes,
                )
                if not fin:
                    fragmented = True
                    continue
            elif opcode == 0x0:
                if not fragmented:
                    raise WebSocketProtocolError(
                        "received a continuation frame without a message"
                    )
                fragments.extend(payload)
                _enforce_message_limit(
                    len(fragments),
                    self._max_message_bytes,
                )
                if not fin:
                    continue
                fragmented = False
            elif opcode == 0x2:
                raise WebSocketProtocolError(
                    "binary WebSocket messages are not supported"
                )
            else:
                raise WebSocketProtocolError(
                    f"unsupported data opcode: {opcode}"
                )

            try:
                return fragments.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise WebSocketProtocolError(
                    "text message is not valid UTF-8"
                ) from exc

    def recv_json(self) -> dict[str, Any] | list[Any] | None:
        text = self.recv_text()
        if text is None:
            return None
        try:
            value = json.loads(text)
        except json.JSONDecodeError as exc:
            raise WebSocketProtocolError(
                "WebSocket text message is not valid JSON"
            ) from exc
        if not isinstance(value, (dict, list)):
            raise WebSocketProtocolError(
                "WebSocket JSON message must be an object or array"
            )
        return value

    def close(self, *, code: int = 1000, reason: str = "") -> None:
        if self._socket.fileno() < 0:
            return
        if not 0 <= code <= 0xFFFF:
            raise ValueError("close code must fit in 16 bits")
        reason_bytes = reason.encode("utf-8")
        payload = code.to_bytes(2, "big") + reason_bytes
        if len(payload) > 125:
            raise ValueError("close reason is too long")
        try:
            if not self._closed and not self._close_sent:
                try:
                    self._send_frame(0x8, payload)
                except WebSocketConnectionClosed:
                    pass
        finally:
            self._closed = True
            try:
                self._socket.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self._socket.close()

    def __enter__(self) -> "UnixWebSocketConnection":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        if self._closed:
            raise WebSocketConnectionClosed("WebSocket connection is closed")
        first = 0x80 | opcode
        length = len(payload)
        if length < 126:
            header = bytes((first, 0x80 | length))
        elif length <= 0xFFFF:
            header = bytes((first, 0x80 | 126)) + length.to_bytes(2, "big")
        else:
            header = bytes((first, 0x80 | 127)) + length.to_bytes(8, "big")
        mask = secrets.token_bytes(4)
        masked = bytes(
            value ^ mask[index % 4] for index, value in enumerate(payload)
        )
        with self._send_lock:
            try:
                self._socket.sendall(header + mask + masked)
            except OSError as exc:
                self._closed = True
                raise WebSocketConnectionClosed(
                    f"could not send WebSocket frame: {exc}"
                ) from exc
        if opcode == 0x8:
            self._close_sent = True

    def _recv_frame(self) -> tuple[bool, int, bytes]:
        first, second = self._read_exact(2)
        fin = bool(first & 0x80)
        if first & 0x70:
            raise WebSocketProtocolError(
                "WebSocket extension bits were set without negotiation"
            )
        opcode = first & 0x0F
        masked = bool(second & 0x80)
        if masked:
            raise WebSocketProtocolError("server WebSocket frames must not be masked")
        length = second & 0x7F
        if length == 126:
            length = int.from_bytes(self._read_exact(2), "big")
            if length < 126:
                raise WebSocketProtocolError(
                    "WebSocket frame used a non-minimal 16-bit length"
                )
        elif length == 127:
            encoded = self._read_exact(8)
            if encoded[0] & 0x80:
                raise WebSocketProtocolError(
                    "WebSocket frame length exceeds 63 bits"
                )
            length = int.from_bytes(encoded, "big")
            if length <= 0xFFFF:
                raise WebSocketProtocolError(
                    "WebSocket frame used a non-minimal 64-bit length"
                )
        if opcode >= 0x8 and length > 125:
            raise WebSocketProtocolError(
                "control frame payload exceeds 125 bytes"
            )
        if length > self._max_message_bytes:
            raise WebSocketProtocolError(
                "WebSocket frame exceeds the configured message limit"
            )
        return fin, opcode, self._read_exact(length)

    def _read_exact(self, count: int) -> bytes:
        while len(self._buffer) < count:
            try:
                chunk = self._socket.recv(max(4096, count - len(self._buffer)))
            except OSError as exc:
                self._closed = True
                raise WebSocketConnectionClosed(
                    f"could not read WebSocket frame: {exc}"
                ) from exc
            if not chunk:
                self._closed = True
                raise WebSocketConnectionClosed(
                    "WebSocket peer closed the Unix socket"
                )
            self._buffer.extend(chunk)
        result = bytes(self._buffer[:count])
        del self._buffer[:count]
        return result


def _read_http_upgrade(
    sock: socket.socket,
    *,
    max_header_bytes: int,
) -> tuple[bytes, bytes]:
    data = bytearray()
    marker = b"\r\n\r\n"
    while marker not in data:
        if len(data) >= max_header_bytes:
            raise WebSocketHandshakeError(
                "WebSocket Upgrade response headers are too large"
            )
        try:
            chunk = sock.recv(min(4096, max_header_bytes - len(data)))
        except OSError as exc:
            raise WebSocketHandshakeError(
                f"could not read WebSocket Upgrade response: {exc}"
            ) from exc
        if not chunk:
            raise WebSocketHandshakeError(
                "peer closed during WebSocket Upgrade"
            )
        data.extend(chunk)
    end = data.index(marker) + len(marker)
    return bytes(data[:end]), bytes(data[end:])


def _validate_http_upgrade(header: bytes, key: str) -> None:
    try:
        text = header.decode("iso-8859-1")
    except UnicodeDecodeError as exc:  # pragma: no cover - codec is total
        raise WebSocketHandshakeError(
            "WebSocket Upgrade response headers are invalid"
        ) from exc
    lines = text.split("\r\n")
    status = lines[0].split()
    if len(status) < 2 or status[1] != "101":
        raise WebSocketHandshakeError(
            f"WebSocket Upgrade returned invalid status: {lines[0]!r}"
        )
    headers: dict[str, list[str]] = {}
    for line in lines[1:]:
        if not line:
            continue
        if ":" not in line:
            raise WebSocketHandshakeError(
                f"malformed WebSocket Upgrade header: {line!r}"
            )
        name, value = line.split(":", 1)
        headers.setdefault(name.strip().casefold(), []).append(value.strip())

    def tokens(name: str) -> set[str]:
        values = headers.get(name, [])
        return {
            token.strip().casefold()
            for value in values
            for token in value.split(",")
            if token.strip()
        }

    if "websocket" not in tokens("upgrade"):
        raise WebSocketHandshakeError(
            "WebSocket Upgrade response omitted Upgrade: websocket"
        )
    if "upgrade" not in tokens("connection"):
        raise WebSocketHandshakeError(
            "WebSocket Upgrade response omitted Connection: Upgrade"
        )
    accepts = headers.get("sec-websocket-accept", [])
    expected = base64.b64encode(
        hashlib.sha1((key + _WEBSOCKET_GUID).encode("ascii")).digest()
    ).decode("ascii")
    if len(accepts) != 1 or not secrets.compare_digest(accepts[0], expected):
        raise WebSocketHandshakeError(
            "WebSocket Upgrade returned an invalid Sec-WebSocket-Accept"
        )


def _enforce_message_limit(size: int, maximum: int) -> None:
    if size > maximum:
        raise WebSocketProtocolError(
            "WebSocket message exceeds the configured message limit"
        )
