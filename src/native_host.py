"""Native loopback host used by grey-kit's gated supervisor.

Direct server.py/Zeabur startup is unchanged. This entry requires an inherited
loopback listener, an owning parent, and an owner-only gate file. It never logs
request bodies or credential values. A lost supervisor or closed gate stops it.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
import signal
import socket
import stat
import threading
import time


def require(value, category):
    if not value:
        raise ValueError(category)


def gate_open(path):
    try:
        path = Path(path)
        if not path.is_absolute() or any(p.is_symlink() for p in (path, *path.parents)):
            return False
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            return (
                stat.S_ISREG(info.st_mode)
                and info.st_uid == os.getuid()
                and stat.S_IMODE(info.st_mode) == 0o600
                and info.st_nlink == 1
                and info.st_size <= 64
                and stream.read(65).strip() == b"enabled"
            )
    except OSError:
        return False


def watchdog(parent_pid, gate_file, *, interval=0.25):
    """A supervisor killed with SIGKILL must not leave a model worker alive."""
    require(os.getpgrp() == os.getpid() and os.getsid(0) == os.getpid(), "dedicated_session_required")
    while True:
        if os.getppid() != parent_pid or not gate_open(gate_file):
            # Fail closed for every member, including descendants ignoring
            # TERM. The live session leader proves this is our own group.
            os.killpg(os.getpgrp(), signal.SIGKILL)
            os._exit(75)
        time.sleep(interval)


async def inactive(*_args, **_kwargs):
    return None


def build_native_app(server, *, background=False):
    from server_app import HTTPRuntimeSettings, RuntimeLifecycle, build_http_app, merge_mcp_tool_registries

    require(type(background) is bool, "background_policy")
    settings = HTTPRuntimeSettings.from_config(server.config)
    require(settings.auth_required and settings.auth_mode == "token", "native_auth_required")
    require(not server.config.get("night_clerk", {}).get("enabled") or background, "night_clerk_gate_closed")
    require(not server.config.get("daily_continuity", {}).get("enabled") or background, "daily_continuity_gate_closed")
    require(not server.config.get("nightly_dreams", {}).get("enabled") or background, "nightly_dream_gate_closed")
    merge_mcp_tool_registries(server.mcp, server.mcp_extra)
    if not background:
        # breath may lazily start decay; suppress that route as well as lifespan.
        server.decay_engine.ensure_started = inactive
        server.decay_engine.start = inactive
    lifecycle = RuntimeLifecycle(logger=logging.getLogger("ob-native"))
    if background:
        lifecycle.decay_engine = server.decay_engine
        lifecycle.embedding_outbox = server.embedding_outbox
        lifecycle.daily_continuity = server.daily_continuity
        lifecycle.nightly_dreams = server.nightly_dreams
        from raw_archive import get_archive

        lifecycle.raw_purge = lambda: asyncio.to_thread(get_archive(server.config).purge_due)
        # Tunnel/Ollama and GitHub are separate, explicitly managed services.
    return build_http_app(
        server.mcp,
        "streamable-http",
        settings=settings,
        token_validator=server._is_valid_static_mcp_token,
        lifecycle=lifecycle,
    )


def main():
    require(os.environ.get("PYTHON_DOTENV_DISABLED") == "1", "dotenv_must_be_disabled")
    require(os.environ.get("OMBRE_BIND_HOST") == "127.0.0.1", "loopback_required")
    parent = int(os.environ["OB_NATIVE_PARENT_PID"])
    require(parent > 1 and os.getppid() == parent, "owning_parent_required")
    require(os.getpgrp() == os.getpid() and os.getsid(0) == os.getpid(), "dedicated_session_required")
    gate = os.environ["OB_NATIVE_GATE_FILE"]
    require(gate_open(gate), "gate_closed")
    background = os.environ.get("OB_NATIVE_BACKGROUND")
    require(background in ("0", "1"), "background_policy")
    listener = socket.socket(fileno=int(os.environ["OB_NATIVE_LISTENER_FD"]))
    require(
        listener.family == socket.AF_INET
        and listener.getsockname()[0] == "127.0.0.1"
        and listener.getsockname()[1] == int(os.environ["OMBRE_PORT"]),
        "loopback_listener_required",
    )
    threading.Thread(target=watchdog, args=(parent, gate), daemon=True).start()
    # No ambient dotenv loading, including on older python-dotenv versions.
    import dotenv

    dotenv.load_dotenv = lambda *_args, **_kwargs: False
    import yaml

    cfg = yaml.safe_load(Path(os.environ["OMBRE_CONFIG_PATH"]).read_text())
    require(isinstance(cfg, dict), "service_config_required")
    if background == "0":
        require(
            all(
                (cfg.get(key) or {}).get("enabled") is False
                for key in ("daily_continuity", "nightly_dreams", "night_clerk")
            ),
            "background_gate_closed",
        )
    require(
        not (cfg.get("dehydration") or {}).get("api_key") and not (cfg.get("embedding") or {}).get("api_key"),
        "model_keys_must_be_external",
    )
    import server
    import uvicorn

    if os.environ.get("OB_NATIVE_MODEL_ACCESS") == "0":
        require(
            not server.dehydrator.api_available and not server.embedding_engine.enabled, "unexpected_model_credentials"
        )
    app = build_native_app(server, background=background == "1")
    runner = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=int(os.environ["OMBRE_PORT"]), log_config=None, access_log=False)
    )
    runner.run(sockets=[listener])


if __name__ == "__main__":
    try:
        main()
    except BaseException:
        # The supervisor records exit metadata; no raw exception/body output.
        raise SystemExit(70)
