"""Local-only, explicit SIWC management. Nothing runs on import.

Run with PYTHONPATH=src python -m chatgpt_login --auth-dir PATH login.
The listener exists only for the five-minute browser sign-in, then closes.
"""
from __future__ import annotations

import argparse
import asyncio
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import re
import sys
import time
import socket
import threading
from contextlib import suppress
import webbrowser


from chatgpt_auth import AuthManager, AuthStore, USAGE_URL
from clerk_pipeline import ClerkError, EXTRACT_INSTRUCTIONS
from clerk_chatgpt import ChatGPTSubscriptionAdapter

_PAGE = """<!doctype html><html lang="zh"><meta charset="utf-8"><meta name="referrer" content="same-origin">
<title>OB · 连接 ChatGPT</title><body style="max-width:640px;margin:64px auto;font:18px/1.7 system-ui;padding:20px">
<h1>让 OB 使用你的 ChatGPT 订阅</h1><p>你将在 OpenAI 页面选择账号并决定是否授权订阅用量。
连接成功后仍需单独选择模型、试跑；这里不会自动发送聊天。</p>
<form method="post" action="/start"><input type="hidden" name="ticket" value="TICKET">
<button style="font-size:18px;padding:12px">Continue with ChatGPT</button></form>
<p><a href="https://chatgpt.com/#settings/Usage" rel="noreferrer">Manage usage</a></p></body></html>"""


def public_error(error):
    code = error.code if isinstance(error, ClerkError) else ""
    return code if isinstance(code, str) and re.fullmatch(r"subscription_[a-z0-9_]{1,100}", code) else "subscription_operation_failed"


def login_handler(manager, label, result):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass  # Callback targets and token hints must never enter access logs.

        def answer(self, status, body):
            data = body.encode()
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            # Keep a real Origin on the same-origin form POST. Callback/error
            # pages and the OAuth redirect still suppress referrers entirely.
            self.send_header("Referrer-Policy", "same-origin" if self.path == "/" and status == 200 else "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; form-action 'self' https://auth.openai.com; frame-ancestors 'none'; base-uri 'none'")
            self.end_headers()
            self.wfile.write(data)

        def local_host(self):
            return self.headers.get("Host") == f"127.0.0.1:{self.server.server_port}"

        def do_GET(self):
            if not self.local_host():
                return self.answer(403, "Invalid local host.")
            if self.path == "/":
                return self.answer(200, _PAGE.replace("TICKET", self.server.ticket))
            if self.path.startswith("/auth/callback?") and self.server.attempt:
                try:
                    status = manager.complete(self.server.attempt, self.path)
                    result.update(status)
                    self.server.finished = True
                    if status["plan_usage_enabled"]:
                        return self.answer(200, "<meta charset='utf-8'><h1>You’re using your ChatGPT plan</h1><p>OB 已连接。尚未发送聊天；下一步单独选择模型和试跑。</p><a href='" + USAGE_URL + "'>Manage usage</a>")
                    return self.answer(200, "<meta charset='utf-8'><p>身份已连接，但未获准使用订阅用量。不会发送整理请求。</p>")
                except Exception as error:
                    # No exception text, callback or third-party diagnostics in the page/log.
                    self.server.finished = True
                    result["error"] = public_error(error)
                    return self.answer(400, "<meta charset='utf-8'><p>授权未完成。请关闭此页，重新运行登录。</p>")
            return self.answer(404, "Not found.")

        def do_POST(self):
            from urllib.parse import parse_qs
            if not self.local_host() or self.path != "/start" or self.server.attempt:
                return self.answer(403, "Invalid request.")
            origin = self.headers.get("Origin")
            if origin and origin != f"http://127.0.0.1:{self.server.server_port}":
                return self.answer(403, "Invalid origin.")
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 512:
                    raise ValueError
                params = parse_qs(self.rfile.read(length).decode(), keep_blank_values=True)
                import hmac
                if set(params) != {"ticket"} or len(params["ticket"]) != 1 or not hmac.compare_digest(params["ticket"][0], self.server.ticket):
                    raise ValueError
            except (ValueError, UnicodeError):
                return self.answer(403, "Invalid request.")
            uri = f"http://127.0.0.1:{self.server.server_port}/auth/callback"
            self.server.attempt, url = manager.begin(label, uri)
            self.send_response(303)
            self.send_header("Location", url)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Length", "0")
            self.end_headers()
    return Handler


class LoopbackServer(HTTPServer):
    def __init__(self, *args, **kwargs):
        self.deadline_timers = {}
        self.listener_timer = None
        super().__init__(*args, **kwargs)

    def arm_deadline(self, seconds=300):
        self.deadline = time.monotonic() + seconds
        self.listener_timer = threading.Timer(seconds, self.server_close)
        self.listener_timer.daemon = True
        self.listener_timer.start()

    def get_request(self):
        sock, address = super().get_request()
        remaining = max(0.01, self.deadline - time.monotonic())
        sock.settimeout(min(5, remaining))
        def terminate():
            with suppress(OSError):
                sock.shutdown(socket.SHUT_RDWR)
            sock.close()
        timer = threading.Timer(remaining, terminate)
        timer.daemon = True
        self.deadline_timers[sock] = timer
        timer.start()
        return sock, address

    def close_request(self, request):
        timer = self.deadline_timers.pop(request, None)
        if timer:
            timer.cancel()
        super().close_request(request)

    def server_close(self):
        if self.listener_timer:
            self.listener_timer.cancel()
        for sock, timer in list(self.deadline_timers.items()):
            timer.cancel()
            with suppress(OSError):
                sock.shutdown(socket.SHUT_RDWR)
            sock.close()
        self.deadline_timers.clear()
        super().server_close()

    def handle_error(self, *_args):
        pass  # No tracebacks or request targets from a local unauthenticated client.


def browser_login(manager, label):
    import secrets
    result = {}
    with LoopbackServer(("127.0.0.1", 0), login_handler(manager, label, result)) as server:
        server.arm_deadline()
        server.timeout = 1
        server.attempt, server.finished = None, False
        server.ticket = secrets.token_urlsafe(32)
        local_url = f"http://127.0.0.1:{server.server_port}/"
        print("在浏览器点击 Continue with ChatGPT。此本机入口五分钟后关闭：" + local_url, flush=True)
        webbrowser.open(local_url)
        while not server.finished and time.monotonic() < server.deadline:
            try:
                server.handle_request()
            except (OSError, ValueError):
                break
    if not result or "error" in result:
        raise ClerkError(result.get("error") or "subscription_login_not_completed")
    return result


async def smoke(manager, label):
    adapter = ChatGPTSubscriptionAdapter(manager, label)
    payload = {"operation": "extract", "instructions": EXTRACT_INSTRUCTIONS,
               "memory_day": "2000-01-01", "missing_context": False,
               "sources": [{"fragment_id": "synthetic-fragment", "source_id": "synthetic-message",
                            "source_sha256": "0" * 64, "at": "2000-01-01T12:00:00+08:00",
                            "speaker": "知知", "start": 0, "end": 10, "text": "虚构测试：午饭吃面。"}]}
    reply = await adapter.generate(payload)
    from clerk_pipeline import validate_facts
    validate_facts(reply, payload["sources"])
    return {"ok": True, "synthetic_only": True, "model": adapter.model, "usage": reply["usage"]}


def main(argv=None):
    parser = argparse.ArgumentParser(description="OB 独立 ChatGPT 订阅连接；不读取 Codex/Claude 登录文件")
    parser.add_argument("--auth-dir", required=True, help="Git与vault之外的独立私有目录")
    parser.add_argument("--vault", required=True, help="OB原文／记忆数据根；凭据目录必须在它之外")
    parser.add_argument("--profile", default="main", help="连接标签；换账号请用另一个标签")
    parser.add_argument("action", choices=["login", "status", "models", "select-model", "smoke", "resume", "logout"])
    parser.add_argument("model", nargs="?")
    args = parser.parse_args(argv)
    try:
        manager = AuthManager(AuthStore(args.auth_dir, create=args.action == "login", vault=args.vault))
        if args.action == "login":
            result = browser_login(manager, args.profile)
        elif args.action == "status":
            result = {"profiles": manager.store.summaries(), "manage_usage": USAGE_URL}
        elif args.action == "models":
            result = {"models": manager.models(args.profile)}
        elif args.action == "select-model":
            manager.choose_model(args.profile, args.model)
            result = {"model": args.model}
        elif args.action == "smoke":
            result = asyncio.run(smoke(manager, args.profile))
        elif args.action == "resume":
            manager.store.pause(args.profile, "")
            result = {"resumed": True}
        else:
            result = manager.sign_out(args.profile)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except Exception as error:
        # Credential/HTTP exception reprs may contain tokens or private paths.
        print(json.dumps({"ok": False, "error": public_error(error), "manage_usage": USAGE_URL}))
        return 1


if __name__ == "__main__":
    sys.exit(main())
