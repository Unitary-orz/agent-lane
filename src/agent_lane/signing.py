"""agent-lane managed SSH signing for Codex turns."""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any


SIGNING_HOME_ENV = "AGENT_LANE_SIGNING_HOME"
PRIVATE_KEY_NAME = "codex-agent-signing"
PUBLIC_KEY_NAME = f"{PRIVATE_KEY_NAME}.pub"
GIT_PROGRAM = "/usr/bin/ssh-keygen"
GIT_SIGNING_CONFIG = (
    ("commit.gpgsign", "true"),
    ("gpg.format", "ssh"),
    ("gpg.ssh.program", GIT_PROGRAM),
)
# Signing values are supplied through "shell_environment_policy.set" by the
# app-server adapter. Do not replace Codex's native inherit/include policy.
CODEX_SIGNING_CONFIG_OVERRIDES: list[str] = []


@dataclass(frozen=True)
class SigningPaths:
    home: Path
    keys_dir: Path
    private_key: Path
    public_key: Path
    socket: Path
    env_file: Path


@dataclass(frozen=True)
class SigningInjection:
    metadata: dict[str, Any]
    extra_env: dict[str, str]
    config_overrides: list[str]


@dataclass(frozen=True)
class ThreadSigningProbe:
    command: str
    marker: str
    receipt_path: Path


def signing_home() -> Path:
    raw = os.environ.get(SIGNING_HOME_ENV)
    if raw:
        return Path(raw).expanduser()
    return Path.home() / ".agent-lane" / "signing"


def signing_paths(home: Path | None = None) -> SigningPaths:
    base = (home or signing_home()).expanduser()
    keys_dir = base / "keys"
    return SigningPaths(
        home=base,
        keys_dir=keys_dir,
        private_key=keys_dir / PRIVATE_KEY_NAME,
        public_key=keys_dir / PUBLIC_KEY_NAME,
        socket=base / "agent.sock",
        env_file=base / "agent.env",
    )


def init_signing(*, generate: bool) -> dict[str, Any]:
    if not generate:
        raise ValueError("signing init currently requires --generate")
    paths = signing_paths()
    if paths.private_key.exists() or paths.public_key.exists():
        raise ValueError(f"signing key already exists: {paths.private_key}")
    paths.keys_dir.mkdir(parents=True, exist_ok=True)
    paths.home.mkdir(parents=True, exist_ok=True)
    os.chmod(paths.home, 0o700)
    os.chmod(paths.keys_dir, 0o700)
    _run(
        [
            "ssh-keygen",
            "-t",
            "ed25519",
            "-N",
            "",
            "-C",
            "agent-lane signing",
            "-f",
            str(paths.private_key),
        ],
        timeout=30,
    )
    os.chmod(paths.private_key, 0o600)
    os.chmod(paths.public_key, 0o644)
    ensure_agent_ready(paths)
    return signing_status(paths)


def signing_status(paths: SigningPaths | None = None) -> dict[str, Any]:
    paths = paths or signing_paths()
    running = agent_running(paths)
    return {
        "signing_home": str(paths.home),
        "public_key_path": str(paths.public_key),
        "private_key_exists": paths.private_key.exists(),
        "public_key_exists": paths.public_key.exists(),
        "fingerprint": fingerprint(paths.public_key) if paths.public_key.exists() else None,
        "agent_running": running,
        "key_loaded": key_loaded(paths) if running and paths.public_key.exists() else False,
    }


def stop_agent(paths: SigningPaths | None = None) -> dict[str, Any]:
    paths = paths or signing_paths()
    env = _read_agent_env(paths)
    stopped = False
    if env.get("SSH_AGENT_PID") and env.get("SSH_AUTH_SOCK"):
        subprocess.run(
            ["ssh-agent", "-k"],
            env={**os.environ, **env},
            text=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        stopped = True
    for path in (paths.socket, paths.env_file):
        try:
            path.unlink()
        except FileNotFoundError:
            pass
    return {"stopped": stopped, "signing_home": str(paths.home)}


def prepare_agent_signing() -> SigningInjection:
    paths = signing_paths()
    ensure_agent_ready(paths)
    probe_signing(paths)
    return SigningInjection(
        metadata=signing_metadata(paths),
        extra_env=signing_env(paths),
        config_overrides=CODEX_SIGNING_CONFIG_OVERRIDES.copy(),
    )


def signing_metadata(paths: SigningPaths | None = None, *, mode: str = "agent") -> dict[str, Any]:
    if mode == "off":
        return {"mode": "off"}
    paths = paths or signing_paths()
    return {
        "mode": "agent",
        "backend": "ssh-agent",
        "public_key_path": str(paths.public_key),
        "fingerprint": fingerprint(paths.public_key),
        "git_program": GIT_PROGRAM,
    }


def signing_env(paths: SigningPaths | None = None) -> dict[str, str]:
    paths = paths or signing_paths()
    pairs = [*GIT_SIGNING_CONFIG, ("user.signingkey", str(paths.public_key))]
    env = {"SSH_AUTH_SOCK": str(paths.socket), "GIT_CONFIG_COUNT": str(len(pairs))}
    for index, (key, value) in enumerate(pairs):
        env[f"GIT_CONFIG_KEY_{index}"] = key
        env[f"GIT_CONFIG_VALUE_{index}"] = value
    return env


def thread_signing_probe_command(
    paths: SigningPaths | None = None,
) -> tuple[str, str]:
    """Build a fixed probe for the Codex thread's effective shell environment."""

    paths = paths or signing_paths()
    marker = f"AGENT_LANE_SIGNING_OK:{uuid.uuid4().hex}"
    return _build_thread_signing_probe_command(paths, marker), marker


def thread_signing_probe(
    paths: SigningPaths | None = None,
) -> ThreadSigningProbe:
    """Build a shell probe with a local one-time success receipt."""

    paths = paths or signing_paths()
    receipt_dir = paths.home / "probe-receipts"
    receipt_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(receipt_dir, 0o700)
    nonce = uuid.uuid4().hex
    marker = f"AGENT_LANE_SIGNING_OK:{nonce}"
    receipt_path = receipt_dir / f"{nonce}.ok"
    return ThreadSigningProbe(
        command=_build_thread_signing_probe_command(
            paths,
            marker,
            receipt_path=receipt_path,
        ),
        marker=marker,
        receipt_path=receipt_path,
    )


def _build_thread_signing_probe_command(
    paths: SigningPaths,
    marker: str,
    *,
    receipt_path: Path | None = None,
) -> str:
    expected = signing_env(paths)
    values = {
        "socket": shlex.quote(expected["SSH_AUTH_SOCK"]),
        "program": shlex.quote(GIT_PROGRAM),
        "public_key": shlex.quote(str(paths.public_key)),
        "marker": shlex.quote(marker),
    }
    cleanup = (
        "cleanup_agent_lane_probe() { "
        'rm -f "$probe_dir/message" "$probe_dir/message.sig"; '
        'rmdir "$probe_dir"; }'
    )
    receipt_commands: list[str] = []
    if receipt_path is not None:
        receipt_temp_path = Path(f"{receipt_path}.tmp")
        values["receipt"] = shlex.quote(str(receipt_path))
        values["receipt_temp"] = shlex.quote(str(receipt_temp_path))
        cleanup = (
            "cleanup_agent_lane_probe() { "
            'rm -f "$probe_dir/message" "$probe_dir/message.sig" '
            f'{values["receipt_temp"]}; '
            'rmdir "$probe_dir"; }'
        )
        receipt_commands = [
            (
                f"printf '%s\\n' {values['marker']} "
                f"> {values['receipt_temp']}"
            ),
            f"mv {values['receipt_temp']} {values['receipt']}",
        ]

    return "\n".join(
        [
            "set -eu",
            f'test "${{SSH_AUTH_SOCK:-}}" = {values["socket"]}',
            'test "$(git config --get commit.gpgsign)" = "true"',
            'test "$(git config --get gpg.format)" = "ssh"',
            (
                'test "$(git config --get gpg.ssh.program)" = '
                f'{values["program"]}'
            ),
            (
                'test "$(git config --get user.signingkey)" = '
                f'{values["public_key"]}'
            ),
            'probe_dir="$(mktemp -d)"',
            cleanup,
            "trap cleanup_agent_lane_probe EXIT",
            (
                "printf '%s\\n' 'agent-lane Codex thread signing probe' "
                '> "$probe_dir/message"'
            ),
            (
                f'{values["program"]} -Y sign -n git '
                f'-f {values["public_key"]} "$probe_dir/message" >/dev/null'
            ),
            'test -s "$probe_dir/message.sig"',
            *receipt_commands,
            f"printf '%s\\n' {values['marker']}",
        ]
    )


def ensure_agent_ready(paths: SigningPaths | None = None) -> None:
    paths = paths or signing_paths()
    if not paths.private_key.exists() or not paths.public_key.exists():
        raise ValueError(
            "agent signing key is not initialized; run "
            "agent-lane signing init --generate"
        )
    paths.home.mkdir(parents=True, exist_ok=True)
    os.chmod(paths.home, 0o700)
    if not agent_running(paths):
        _start_agent(paths)
    if not key_loaded(paths):
        _run(["ssh-add", str(paths.private_key)], env=_agent_env(paths), timeout=30)


def agent_running(paths: SigningPaths | None = None) -> bool:
    paths = paths or signing_paths()
    result = subprocess.run(
        ["ssh-add", "-l"],
        env=_agent_env(paths),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    return result.returncode in (0, 1)


def key_loaded(paths: SigningPaths | None = None) -> bool:
    paths = paths or signing_paths()
    if not paths.public_key.exists():
        return False
    expected = _public_key_identity(paths.public_key)
    result = subprocess.run(
        ["ssh-add", "-L"],
        env=_agent_env(paths),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if result.returncode != 0:
        return False
    return expected in {_public_key_identity_text(line) for line in result.stdout.splitlines()}


def probe_signing(paths: SigningPaths | None = None) -> None:
    paths = paths or signing_paths()
    with tempfile.TemporaryDirectory(prefix="agent-lane-signing-probe.") as tmp:
        message = Path(tmp) / "message.txt"
        message.write_text("agent-lane signing probe\n", encoding="utf-8")
        _run(
            [
                GIT_PROGRAM,
                "-Y",
                "sign",
                "-n",
                "git",
                "-f",
                str(paths.public_key),
                str(message),
            ],
            env=_agent_env(paths),
            timeout=30,
        )
        signature = Path(str(message) + ".sig")
        if not signature.exists() or "BEGIN SSH SIGNATURE" not in signature.read_text(
            encoding="utf-8"
        ):
            raise ValueError("agent signing preflight did not produce an SSH signature")


def signing_smoke_test() -> dict[str, Any]:
    paths = signing_paths()
    ensure_agent_ready(paths)
    with tempfile.TemporaryDirectory(prefix="agent-lane-signing-smoke.") as tmp:
        repo = Path(tmp)
        env = {**os.environ, **signing_env(paths)}
        _run(["git", "init", "-q"], cwd=repo, env=env, timeout=30)
        _run(["git", "config", "user.name", "Agent Lane Signing Test"], cwd=repo, env=env)
        _run(
            ["git", "config", "user.email", "agent-lane-signing@example.invalid"],
            cwd=repo,
            env=env,
        )
        (repo / "README.md").write_text("agent-lane signing smoke\n", encoding="utf-8")
        _run(["git", "add", "README.md"], cwd=repo, env=env)
        _run(["git", "commit", "-m", "test: verify agent-lane signing"], cwd=repo, env=env)
        commit = _run(["git", "cat-file", "-p", "HEAD"], cwd=repo, env=env).stdout
    return {
        "signed": "gpgsig -----BEGIN SSH SIGNATURE-----" in commit,
        "fingerprint": fingerprint(paths.public_key),
        "public_key_path": str(paths.public_key),
    }


def fingerprint(public_key: Path) -> str:
    output = _run(["ssh-keygen", "-lf", str(public_key)], timeout=30).stdout.strip()
    match = re.search(r"\bSHA256:[^\s]+", output)
    if not match:
        raise ValueError(f"could not parse signing key fingerprint: {output}")
    return match.group(0)


def _start_agent(paths: SigningPaths) -> None:
    try:
        paths.socket.unlink()
    except FileNotFoundError:
        pass
    paths.home.mkdir(parents=True, exist_ok=True)
    result = _run(["ssh-agent", "-a", str(paths.socket)], timeout=30)
    env = _parse_agent_output(result.stdout)
    if not env.get("SSH_AUTH_SOCK") or not env.get("SSH_AGENT_PID"):
        raise ValueError("ssh-agent did not return SSH_AUTH_SOCK and SSH_AGENT_PID")
    paths.env_file.write_text(
        "".join(f"{key}={value}\n" for key, value in sorted(env.items())),
        encoding="utf-8",
    )
    os.chmod(paths.env_file, 0o600)


def _agent_env(paths: SigningPaths) -> dict[str, str]:
    return {**os.environ, "SSH_AUTH_SOCK": str(paths.socket)}


def _read_agent_env(paths: SigningPaths) -> dict[str, str]:
    if not paths.env_file.exists():
        return {}
    env: dict[str, str] = {}
    for line in paths.env_file.read_text(encoding="utf-8").splitlines():
        key, _, value = line.partition("=")
        if key and value:
            env[key] = value
    return env


def _parse_agent_output(output: str) -> dict[str, str]:
    env: dict[str, str] = {}
    for name in ("SSH_AUTH_SOCK", "SSH_AGENT_PID"):
        match = re.search(rf"{name}=([^;]+);", output)
        if match:
            env[name] = match.group(1)
    return env


def _public_key_identity(path: Path) -> str:
    return _public_key_identity_text(path.read_text(encoding="utf-8"))


def _public_key_identity_text(text: str) -> str:
    parts = text.strip().split()
    return " ".join(parts[:2])


def _run(
    command: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    timeout: float = 30,
) -> subprocess.CompletedProcess[str]:
    if not command or not shutil.which(command[0]):
        raise ValueError(f"required command is not available: {command[0] if command else ''}")
    result = subprocess.run(
        command,
        cwd=str(cwd) if cwd else None,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
    )
    if result.returncode != 0:
        raise ValueError(
            f"{' '.join(command)} failed with exit {result.returncode}: "
            f"{(result.stderr or result.stdout).strip()}"
        )
    return result
