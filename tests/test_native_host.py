"""Native host policy and orphan-group checks; generated data only."""

import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import native_host as host


@unittest.skipUnless(os.name == "posix", "Native host requires POSIX process groups")
class NativeHostTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(
            prefix="ob-host-tests-", dir="/private/tmp" if sys.platform == "darwin" else "/tmp"
        )
        self.gate = Path(self.temp.name) / "gate"
        self.gate.write_text("enabled\n")
        self.gate.chmod(0o600)

    def tearDown(self):
        self.temp.cleanup()

    def test_gate_rejects_permissions_links_and_oversized_input(self):
        self.assertTrue(host.gate_open(self.gate))
        self.gate.chmod(0o644)
        self.assertFalse(host.gate_open(self.gate))
        self.gate.chmod(0o600)
        self.gate.write_bytes(b"enabled" + b" " * 100)
        self.assertFalse(host.gate_open(self.gate))
        self.gate.write_text("enabled")
        link = self.gate.with_name("link")
        link.symlink_to(self.gate)
        self.assertFalse(host.gate_open(link))
        os.link(self.gate, self.gate.with_name("hardlink"))
        self.assertFalse(host.gate_open(self.gate))

    def make_server(self, **overrides):
        config = {
            "mcp_require_auth": True,
            "mcp_auth_mode": "token",
            "daily_continuity": {"enabled": False},
            "nightly_dreams": {"enabled": False},
            "night_clerk": {"enabled": False},
        }
        config.update(overrides)
        return SimpleNamespace(
            config=config,
            decay_engine=SimpleNamespace(start=lambda: None, ensure_started=lambda: None),
            embedding_outbox=object(),
            daily_continuity=object(),
            nightly_dreams=object(),
            mcp=object(),
            mcp_extra=object(),
            _is_valid_static_mcp_token=lambda _token: True,
        )

    def factory(self):
        class Lifecycle:
            def __init__(self, **kwargs):
                self.__dict__.update(kwargs)

        settings = SimpleNamespace(
            from_config=lambda cfg: SimpleNamespace(
                auth_required=cfg["mcp_require_auth"], auth_mode=cfg["mcp_auth_mode"]
            )
        )
        return SimpleNamespace(
            HTTPRuntimeSettings=settings,
            RuntimeLifecycle=Lifecycle,
            merge_mcp_tool_registries=lambda *_args: None,
            build_http_app=lambda *args, **kwargs: kwargs["lifecycle"],
        )

    def test_background_false_disables_lifespan_and_lazy_decay(self):
        server = self.make_server()
        with patch.dict(sys.modules, {"server_app": self.factory()}):
            lifecycle = host.build_native_app(server, background=False)
        self.assertIs(server.decay_engine.start, host.inactive)
        self.assertIs(server.decay_engine.ensure_started, host.inactive)
        self.assertFalse(hasattr(lifecycle, "decay_engine"))
        self.assertFalse(hasattr(lifecycle, "daily_continuity"))

    def test_background_true_only_installs_explicit_business_services(self):
        server = self.make_server()
        with patch.dict(
            sys.modules, {"server_app": self.factory(), "raw_archive": SimpleNamespace(get_archive=lambda _cfg: None)}
        ):
            lifecycle = host.build_native_app(server, background=True)
        self.assertIs(lifecycle.decay_engine, server.decay_engine)
        self.assertIs(lifecycle.daily_continuity, server.daily_continuity)
        self.assertTrue(callable(lifecycle.raw_purge))
        self.assertFalse(hasattr(lifecycle, "start_tunnel"))

    def test_background_and_auth_mismatch_refused(self):
        cases = [self.make_server(mcp_require_auth=False), self.make_server(mcp_auth_mode="oauth")]
        cases += [
            self.make_server(**{key: {"enabled": True}})
            for key in ("daily_continuity", "nightly_dreams", "night_clerk")
        ]
        for server in cases:
            with self.subTest(), patch.dict(sys.modules, {"server_app": self.factory()}), self.assertRaises(ValueError):
                host.build_native_app(server, background=False)

    def test_watchdog_refuses_to_signal_foreign_process_group(self):
        with (
            patch.object(host.os, "getpgrp", return_value=-1),
            patch.object(host.os, "killpg", side_effect=AssertionError),
        ):
            with self.assertRaisesRegex(ValueError, "dedicated_session_required"):
                host.watchdog(0, self.gate)

    def test_main_refuses_bad_session_before_starting_watchdog_or_server(self):
        env = {"PYTHON_DOTENV_DISABLED": "1", "OMBRE_BIND_HOST": "127.0.0.1", "OB_NATIVE_PARENT_PID": str(os.getppid())}
        with (
            patch.dict(os.environ, env, clear=True),
            patch.object(host.os, "getpgrp", return_value=-1),
            patch.object(host.threading, "Thread", side_effect=AssertionError("must not start")),
        ):
            with self.assertRaisesRegex(ValueError, "dedicated_session_required"):
                host.main()

    def test_real_watchdog_kills_own_group_with_term_ignoring_descendant(self):
        read_fd, write_fd = os.pipe()
        worker = (
            "import os,signal,time\nsignal.signal(signal.SIGTERM,signal.SIG_IGN)\n"
            + f'os.write({write_fd},b"R")\nos.write(int(os.environ["ACK_FD"]),b"A")\ntime.sleep(60)\n'
        )
        leader = (
            "import os,subprocess,sys\nimport native_host\nr,w=os.pipe()\n"
            + f'p=subprocess.Popen([sys.executable,"-c",{worker!r}],pass_fds=({write_fd},w),env=dict(os.environ,ACK_FD=str(w)))\n'
            + f"os.close(w)\nos.read(r,1)\nnative_host.watchdog(0,{str(self.gate)!r},interval=0.01)\n"
        )
        env = dict(os.environ, PYTHONPATH=str(Path(host.__file__).parent))
        process = subprocess.Popen(
            [sys.executable, "-B", "-c", leader],
            env=env,
            pass_fds=(write_fd,),
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        os.close(write_fd)
        cleaned = False
        try:
            self.assertTrue(select.select([read_fd], [], [], 5)[0])
            self.assertEqual(os.read(read_fd, 1), b"R")
            process.wait(timeout=5)
            self.assertEqual(process.returncode, -signal.SIGKILL)
            self.assertTrue(select.select([read_fd], [], [], 3)[0])
            self.assertEqual(os.read(read_fd, 1), b"")
            cleaned = True
        finally:
            if not cleaned:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            process.wait(timeout=5)
            os.close(read_fd)


if __name__ == "__main__":
    unittest.main()
