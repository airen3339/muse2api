"""Run: python -B tests/test_chat_lifecycle.py. No browser, credentials or network.

The real FastAPI routes and scheduling classes run with synthetic operational
modules, so importing the app cannot load a local .env, account store or engine.
"""
from __future__ import annotations

import asyncio
from contextlib import ExitStack
import importlib.util
import inspect
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest.mock import patch


class MuseAuthError(Exception):
    pass


class MuseGenerationError(Exception):
    pass


class SyntheticStore:
    def __init__(self, cfg):
        self.accounts = [{"id": "synthetic-a", "enabled": True,
                          "cookies": {"fixture": "not-a-credential"}}]
        self.tasks = {}
        self.marks = []

    def get_account(self, account_id):
        return next((a for a in self.accounts if a["id"] == account_id), None)

    def pick_account(self, **kwargs):
        available = [a for a in self.accounts if a["id"] not in (kwargs.get("exclude_ids") or ())]
        return (available[-1] if kwargs.get("force_rotate") else available[0]) if available else None

    def mark(self, *args):
        self.marks.append(args)


class SyntheticEngine:
    def __init__(self, cfg):
        self.current_acc_id = None
        self.worker = None
        self.calls = []
        self.timeouts = []
        self.start_calls = 0
        self.close_calls = 0
        self.start_error = None
        self.close_error = None
        self.account_errors = {}
        self.burst_count = None
        self.burst_error = None
        self.fill = threading.Event()
        self.extra_chunk = threading.Event()
        self.upstream_closed = threading.Event()

    def start(self):
        self.worker = threading.current_thread()
        self.start_calls += 1
        if self.start_error:
            raise self.start_error

    def close_current_page(self):
        self.close_calls += 1
        if self.close_error:
            raise self.close_error

    def reset_thread(self):
        pass

    def chat_stream(self, cookies, prompt, expires, timeout, *, account_id, stop_event):
        self.worker = threading.current_thread()
        self.calls.append(account_id)
        self.timeouts.append(timeout)
        try:
            if account_id in self.account_errors:
                raise self.account_errors[account_id]
            yield "synthetic-first"
            if self.burst_count is None:
                yield " synthetic-last"
                return
            if not self.fill.wait(3):
                raise AssertionError("producer gate was not released")
            for number in range(self.burst_count):
                if stop_event.is_set():
                    return
                if number == 100:
                    self.extra_chunk.set()
                yield f"chunk-{number}"
            if self.burst_error:
                raise self.burst_error
        finally:
            self.upstream_closed.set()


def sse_payloads(body):
    return [json.loads(line[6:]) for line in body.splitlines()
            if line.startswith("data: ") and line != "data: [DONE]"]


async def request(target, path, *, stream=False, timeout=1, protocol="2.0"):
    body = {"stream": stream, "timeout": timeout,
            ("prompt" if path.endswith("completions") else "input"): "synthetic"}
    raw = json.dumps(body).encode()
    scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": protocol}, "http_version": "1.1",
             "method": "POST", "scheme": "http", "path": path, "raw_path": path.encode(),
             "query_string": b"", "root_path": "",
             "headers": [(b"content-type", b"application/json"),
                         (b"authorization", b"Bearer test-only")],
             "server": ("offline", 80), "client": ("offline", 1)}
    sent = False
    messages = []

    async def receive():
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": raw, "more_body": False}
        await asyncio.Event().wait()

    async def send(message):
        messages.append(message)

    await asyncio.wait_for(target.app(scope, receive, send), timeout=4)
    status = next(m["status"] for m in messages if m["type"] == "http.response.start")
    return status, b"".join(m.get("body", b"") for m in messages).decode()


class ChatLifecycleTests(unittest.TestCase):
    paths = ("/v1/chat/completions", "/v1/responses")

    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        home = self.stack.enter_context(tempfile.TemporaryDirectory(prefix="muse-chat-test-"))
        self.stack.enter_context(patch.dict(os.environ, MUSE2API_HOME=home,
                                           MUSE2API_PROFILE_ROOT=home, MUSE2API_KEY="test-only"))
        for name in ("create_connection",):
            self.stack.enter_context(patch.object(socket, name, side_effect=AssertionError("network forbidden")))
        self.stack.enter_context(patch.object(socket.socket, "connect",
                                              side_effect=AssertionError("network forbidden")))
        cfg = types.SimpleNamespace(base_dir=home, profile_root=home, ensure_dirs=lambda: None,
                                    cors_origins="", max_concurrency=1, api_key="test-only",
                                    chat_timeout=1, tool_protocol=False)
        config = types.ModuleType("config")
        config.CFG = cfg
        engine = types.ModuleType("engine")
        engine.ESSENTIAL_COOKIES = ()
        engine.MuseAuthError, engine.MuseGenerationError = MuseAuthError, MuseGenerationError
        engine.MuseEngine = SyntheticEngine
        store = types.ModuleType("store")
        store.Store = SyntheticStore
        store.account_expiry = store.min_expiry = lambda *args, **kwargs: None
        self.stack.enter_context(patch.dict(sys.modules, config=config, engine=engine, store=store))
        source = Path(os.environ.get("MUSE_CHAT_TEST_SOURCE") or
                      Path(__file__).resolve().parents[1] / "app.py")
        spec = importlib.util.spec_from_file_location("_muse_chat_test_target", source)
        self.target = importlib.util.module_from_spec(spec)
        self.stack.enter_context(patch.dict(sys.modules, {spec.name: self.target}))
        spec.loader.exec_module(self.target)
        self.target._renew_and_persist = lambda *args, **kwargs: None
        self.target._sync_cookies = lambda *args, **kwargs: None
        self.engine = self.target.engine
        self.worker_errors = []
        self.stack.enter_context(patch.object(threading, "excepthook",
                                              side_effect=self.worker_errors.append))

    def tearDown(self):
        self.assertEqual(self.worker_errors, [], "worker failures must be delivered to the caller")

    def assert_released(self):
        if self.engine.worker:
            self.engine.worker.join(0.5)
            self.assertFalse(self.engine.worker.is_alive(), "chat worker did not exit")
        self.assertEqual(self.target.ACCOUNT_POOL.busy_accounts, set())
        self.assertTrue(self.target.GEN_LOCK.acquire(blocking=False))
        self.target.GEN_LOCK.release()
        account = self.target.ACCOUNT_POOL.acquire(account_id="synthetic-a", timeout=0.1)
        self.target.ACCOUNT_POOL.release(account["id"])

    def assert_success(self, path, stream, status, body):
        self.assertEqual(status, 200)
        expected = "synthetic-first synthetic-last"
        if not stream:
            data = json.loads(body)
            if path.endswith("completions"):
                self.assertEqual(data["choices"][0]["message"]["content"], expected)
                self.assertEqual(data["choices"][0]["finish_reason"], "stop")
            else:
                self.assertEqual(data["status"], "completed")
                self.assertEqual(data["output"][0]["content"][0]["text"], expected)
        else:
            data = sse_payloads(body)
            if path.endswith("completions"):
                self.assertEqual("".join(item["choices"][0]["delta"].get("content", "") for item in data), expected)
                self.assertEqual(data[-1]["choices"][0]["finish_reason"], "stop")
                self.assertIn("data: [DONE]", body)
            else:
                self.assertEqual(data[-1]["type"], "response.completed")
                self.assertEqual(data[-1]["response"]["output"][0]["content"][0]["text"], expected)

    def assert_failure(self, path, stream, status, body, code=502, message=None):
        if not stream:
            self.assertEqual(status, code)
            data = json.loads(body)
            self.assertEqual(data["error"]["code"], code)
            if message:
                self.assertIn(message, data["error"]["message"])
        else:
            self.assertEqual(status, 200)  # SSE headers have already been sent.
            data = sse_payloads(body)
            if path.endswith("completions"):
                self.assertEqual(data[-1]["error"]["code"], code)
                if message:
                    self.assertIn(message, data[-1]["error"]["message"])
                self.assertNotIn("data: [DONE]", body)
                self.assertFalse(any(item.get("choices", [{}])[0].get("finish_reason") for item in data))
            else:
                self.assertEqual(data[-1]["type"], "response.failed")
                self.assertNotIn("response.completed", body)

    def test_routes_ordinary_success(self):
        for path in self.paths:
            for stream in (False, True):
                for protocol in ("2.0", "2.4"):
                    with self.subTest(path=path, stream=stream, protocol=protocol):
                        result = asyncio.run(request(self.target, path, stream=stream, protocol=protocol))
                        self.assert_success(path, stream, *result)
                        self.assertEqual(self.engine.timeouts[-1], 1)
                        self.assert_released()

    def test_routes_account_timeout(self):
        for path in self.paths:
            for stream in (False, True):
                with self.subTest(path=path, stream=stream):
                    self.target.ACCOUNT_POOL.acquire(account_id="synthetic-a", timeout=0.1)
                    started = time.monotonic()
                    try:
                        result = asyncio.run(request(self.target, path, stream=stream))
                        self.assert_failure(path, stream, *result, message="等待空闲账号超时")
                        self.assertGreaterEqual(time.monotonic() - started, 0.9)
                        self.assertLess(time.monotonic() - started, 2)
                        self.assertEqual(self.engine.start_calls, 0)
                    finally:
                        self.target.ACCOUNT_POOL.release("synthetic-a")
                    self.assert_released()

    def test_routes_initial_acquisition_error(self):
        for path in self.paths:
            for stream in (False, True):
                with self.subTest(path=path, stream=stream):
                    with patch.object(self.target.ACCOUNT_POOL, "acquire",
                                      side_effect=MuseAuthError("synthetic-account-disabled")):
                        result = asyncio.run(request(self.target, path, stream=stream))
                    self.assert_failure(path, stream, *result, code=401, message="synthetic-account-disabled")
                    self.assert_released()

    def test_routes_initialization_error(self):
        self.engine.start_error = MuseGenerationError("synthetic-init-failure")
        for path in self.paths:
            for stream in (False, True):
                with self.subTest(path=path, stream=stream):
                    self.assert_failure(path, stream, *asyncio.run(request(self.target, path, stream=stream)),
                                        message="synthetic-init-failure")
                    self.assert_released()

    def test_routes_session_initialization_error(self):
        for path in self.paths:
            for stream in (False, True):
                with self.subTest(path=path, stream=stream):
                    with patch.object(self.target, "_renew_and_persist",
                                      side_effect=MuseGenerationError("synthetic-renew-failure")):
                        result = asyncio.run(request(self.target, path, stream=stream))
                    self.assert_failure(path, stream, *result, message="synthetic-renew-failure")
                    self.assertEqual(self.engine.start_calls, 0)
                    self.assert_released()

    def test_routes_cleanup_error(self):
        self.engine.close_error = MuseGenerationError("synthetic-cleanup-failure")
        for path in self.paths:
            for stream in (False, True):
                with self.subTest(path=path, stream=stream):
                    self.assert_failure(path, stream, *asyncio.run(request(self.target, path, stream=stream)),
                                        message="synthetic-cleanup-failure")
                    self.assert_released()

    def test_retry_and_original_error_semantics(self):
        original = MuseAuthError("synthetic-primary-auth-failure")
        self.engine.account_errors["synthetic-a"] = original
        self.engine.close_error = MuseGenerationError("synthetic-secondary-cleanup-failure")
        with self.assertRaises(MuseAuthError) as caught:
            list(self.target.safe_chat_stream({}, "synthetic", None, 1, "synthetic-a"))
        self.assertIs(caught.exception, original)
        self.assert_released()
        self.engine.close_error = None
        self.target.store.accounts.append({"id": "synthetic-b", "enabled": True,
                                          "cookies": {"fixture": "not-a-credential"}})
        result = list(self.target.safe_chat_stream({}, "synthetic", None, 1, "synthetic-a"))
        self.assertEqual("".join(result), "synthetic-first synthetic-last")
        self.assertEqual(self.engine.calls[-2:], ["synthetic-a", "synthetic-b"])
        self.assert_released()

    def burst_stream(self, count, error=None):
        self.engine.burst_count, self.engine.burst_error = count, error
        self.engine.fill.clear()
        stream = self.target.safe_chat_stream({}, "synthetic", None, 1, "synthetic-a")
        self.assertEqual(next(stream), "synthetic-first")
        self.addCleanup(stream.close)
        handoff = stream.gi_frame.f_locals["q"]
        self.engine.fill.set()
        return stream, handoff

    def assert_route_reuse(self):
        self.engine.burst_count = None
        self.engine.burst_error = None
        self.assert_success(self.paths[0], False, *asyncio.run(request(self.target, self.paths[0])))
        self.assert_released()

    def test_cancel_full_data_queue(self):
        stream, handoff = self.burst_stream(101)
        self.assertTrue(self.engine.extra_chunk.wait(1))
        self.assertEqual(handoff.qsize(), 100)
        stopped = stream.gi_frame.f_locals["stop_event"]
        stream.close()
        self.assertTrue(stopped.is_set())
        self.assert_released()
        self.assertTrue(self.engine.upstream_closed.is_set())
        self.assertEqual(handoff.qsize(), 100, "cleanup must not depend on draining the queue")
        self.assert_route_reuse()

    def test_full_queue_completion_and_error(self):
        for fail in (False, True):
            for cancel in (False, True):
                with self.subTest(fail=fail, cancel=cancel):
                    failure = MuseGenerationError("synthetic-full-queue-failure") if fail else None
                    stream, handoff = self.burst_stream(100, failure)
                    self.engine.worker.join(0.5)
                    self.assertFalse(self.engine.worker.is_alive(), "terminal delivery blocked on full queue")
                    self.assertEqual(handoff.qsize(), 100)
                    self.assert_released()
                    if cancel:
                        stream.close()
                    else:
                        chunks = []
                        if fail:
                            with self.assertRaises(MuseGenerationError) as caught:
                                for chunk in stream:
                                    chunks.append(chunk)
                            self.assertIs(caught.exception, failure)
                        else:
                            chunks = list(stream)
                        self.assertEqual(chunks, [f"chunk-{n}" for n in range(100)])
                    self.assert_route_reuse()

    def start_waiting_reader(self):
        stream = self.target.safe_chat_stream({}, "synthetic", None, 1, "synthetic-a")
        result = []
        reader = threading.Thread(target=lambda: result.extend(stream), daemon=True)
        reader.start()
        self.addCleanup(stream.close)
        deadline = time.monotonic() + 1
        while "stop_event" not in stream.gi_frame.f_locals:
            self.assertLess(time.monotonic(), deadline, "reader did not start")
            time.sleep(0.001)
        return stream, reader, result

    def test_cancel_waiting_for_account(self):
        self.target.ACCOUNT_POOL.acquire(account_id="synthetic-a", timeout=0.1)
        acquired = threading.Event()
        real_acquire = self.target.ACCOUNT_POOL.acquire

        def track_acquire(**kwargs):
            acquired.set()
            return real_acquire(**kwargs)

        try:
            with patch.object(self.target.ACCOUNT_POOL, "acquire", side_effect=track_acquire):
                stream, reader, result = self.start_waiting_reader()
                self.assertTrue(acquired.wait(1))
                stream.gi_frame.f_locals["stop_event"].set()
                reader.join(0.5)
                self.assertFalse(reader.is_alive())
                self.assertEqual(result, [])
                self.assertEqual(self.target.ACCOUNT_POOL.busy_accounts, {"synthetic-a"})
                self.assertEqual(self.engine.start_calls, 0)
        finally:
            self.target.ACCOUNT_POOL.release("synthetic-a")
        self.assert_released()
        self.assert_route_reuse()

    def test_cancel_waiting_for_slot(self):
        self.target.GEN_LOCK.acquire()
        try:
            stream, reader, result = self.start_waiting_reader()
            deadline = time.monotonic() + 1
            while "synthetic-a" not in self.target.ACCOUNT_POOL.busy_accounts:
                self.assertLess(time.monotonic(), deadline, "account was not leased")
                time.sleep(0.001)
            stream.gi_frame.f_locals["stop_event"].set()
            reader.join(0.5)
            self.assertFalse(reader.is_alive())
            self.assertEqual(result, [])
            self.assertEqual(self.target.ACCOUNT_POOL.busy_accounts, set())
            self.assertFalse(self.target.GEN_LOCK.acquire(blocking=False), "released another owner's slot")
            self.assertEqual(self.engine.start_calls, 0)
        finally:
            self.target.GEN_LOCK.release()
        self.assert_released()
        self.assert_route_reuse()

    def test_slot_wait_has_a_deadline(self):
        self.target.GEN_LOCK.acquire()
        started = time.monotonic()
        try:
            result = asyncio.run(request(self.target, self.paths[0]))
            self.assert_failure(self.paths[0], False, *result, message="等待浏览器队列超时")
            self.assertGreaterEqual(time.monotonic() - started, 0.9)
            self.assertLess(time.monotonic() - started, 2)
            self.assertEqual(self.engine.start_calls, 0)
            self.assertEqual(self.target.ACCOUNT_POOL.busy_accounts, set())
        finally:
            self.target.GEN_LOCK.release()
        self.assert_released()
        self.assert_route_reuse()

    def test_routes_errors_after_first_chunk(self):
        for path in self.paths:
            for stream in (False, True):
                for code, failure in ((401, MuseAuthError("synthetic-after-first-auth")),
                                      (502, MuseGenerationError("synthetic-after-first-generation"))):
                    with self.subTest(path=path, stream=stream, code=code):
                        self.engine.burst_count = 0
                        self.engine.burst_error = failure
                        self.engine.fill.set()
                        before = len(self.engine.calls)
                        result = asyncio.run(request(self.target, path, stream=stream))
                        self.assert_failure(path, stream, *result, code=code, message=str(failure))
                        self.assertEqual(len(self.engine.calls) - before, 1, "must not retry after text")
                        if stream:
                            events = sse_payloads(result[1])
                            if path.endswith("completions"):
                                text = "".join(item.get("choices", [{}])[0].get("delta", {}).get("content", "")
                                               for item in events)
                            else:
                                text = "".join(item.get("delta", "") for item in events)
                            self.assertEqual(text, "synthetic-first")
                        self.assert_released()

    async def disconnect_request(self, path, protocol, stage, *, receive_disconnect=False):
        from starlette.requests import ClientDisconnect

        target = self.target
        ready = threading.Event()
        upstream_closed = threading.Event()
        signals = []
        streams = []
        messages = []
        real_stream = target.safe_chat_stream
        real_account = target.ACCOUNT_POOL.acquire
        real_slot = target.GEN_LOCK.acquire

        def tracked_stream(*args, **kwargs):
            signals.append(kwargs["stop_event"])
            stream = real_stream(*args, **kwargs)
            streams.append(stream)
            return stream

        def paused_upstream(*args, stop_event, **kwargs):
            self.engine.worker = threading.current_thread()
            try:
                yield "X"
                ready.set()
                if not stop_event.wait(3):
                    raise AssertionError("disconnect did not reach paused upstream")
            finally:
                upstream_closed.set()

        def account_wait(**kwargs):
            ready.set()
            return real_account(**kwargs)

        def slot_wait(*args, **kwargs):
            ready.set()
            return real_slot(*args, **kwargs)

        async def wait_ready():
            deadline = asyncio.get_running_loop().time() + 1
            while not ready.is_set():
                self.assertLess(asyncio.get_running_loop().time(), deadline, "request did not reach test stage")
                await asyncio.sleep(0.001)

        raw = json.dumps({"stream": True, "timeout": 3,
                          ("prompt" if path.endswith("completions") else "input"): "synthetic"}).encode()
        scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": protocol},
                 "http_version": "1.1", "method": "POST", "scheme": "http",
                 "path": path, "raw_path": path.encode(), "query_string": b"", "root_path": "",
                 "headers": [(b"content-type", b"application/json"),
                             (b"authorization", b"Bearer test-only")],
                 "server": ("offline", 80), "client": ("offline", 1)}
        sent = False
        disconnected = asyncio.Event()

        async def receive():
            nonlocal sent
            if not sent:
                sent = True
                return {"type": "http.request", "body": raw, "more_body": False}
            await disconnected.wait()
            return {"type": "http.disconnect"}

        async def send(message):
            messages.append(message)
            if protocol == "2.4" and not receive_disconnect and (
                    stage == "send-start" or b'"X"' in message.get("body", b"")):
                if stage != "send-start":
                    await wait_ready()
                disconnected.set()
                raise OSError("synthetic closed socket")

        held_account = stage == "account"
        held_slot = stage == "slot"
        if held_account:
            real_account(account_id="synthetic-a", timeout=0.1)
        if held_slot:
            real_slot()
        task = None
        try:
            with ExitStack() as patches:
                patches.enter_context(patch.object(target, "safe_chat_stream", side_effect=tracked_stream))
                patches.enter_context(patch.object(self.engine, "chat_stream", side_effect=paused_upstream))
                if held_account:
                    patches.enter_context(patch.object(target.ACCOUNT_POOL, "acquire", side_effect=account_wait))
                if held_slot:
                    patches.enter_context(patch.object(target.GEN_LOCK, "acquire", side_effect=slot_wait))
                task = asyncio.create_task(target.app(scope, receive, send))
                if protocol == "2.0" or receive_disconnect:
                    await wait_ready()
                    # Allow the HTTP iterator to enter its pending next() call.
                    await asyncio.sleep(0.05)
                    disconnected.set()
                started = asyncio.get_running_loop().time()
                if protocol == "2.4" and not receive_disconnect:
                    with self.assertRaises(ClientDisconnect):
                        await asyncio.wait_for(task, 0.5)
                else:
                    await asyncio.wait_for(task, 0.5)
                self.assertTrue(signals and signals[0].is_set())
                deadline = asyncio.get_running_loop().time() + 0.5
                while (any(t.name == "muse-chat-worker" for t in threading.enumerate()) or
                       any(inspect.getgeneratorstate(s) not in ("GEN_CLOSED", "GEN_CREATED") for s in streams)):
                    self.assertLess(asyncio.get_running_loop().time(), deadline, "chat worker retained after disconnect")
                    await asyncio.sleep(0.001)
                self.assertLess(asyncio.get_running_loop().time() - started, 1)
                self.assertTrue(all(inspect.getgeneratorstate(s) in ("GEN_CLOSED", "GEN_CREATED")
                                    for s in streams))
                self.assertFalse(any(t.get_name() in ("muse-chat-send", "muse-chat-receive")
                                     for t in asyncio.all_tasks()))
                self.assertEqual(target.ACCOUNT_POOL.busy_accounts, {"synthetic-a"} if held_account else set())
                self.assertEqual(target.GEN_LOCK.locked(), held_slot)
                if stage == "paused":
                    self.assertTrue(upstream_closed.is_set())
                else:
                    self.assertEqual(self.engine.start_calls, 0)
                payload = b"".join(m.get("body", b"") for m in messages)
                self.assertNotIn(b"[DONE]", payload)
                self.assertNotIn(b"response.completed", payload)
        finally:
            for signal in signals:
                signal.set()
            if task and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            if held_account:
                target.ACCOUNT_POOL.release("synthetic-a")
            if held_slot:
                target.GEN_LOCK.release()
        self.assert_released()

    def test_routes_http_disconnect(self):
        for path in self.paths:
            for stage in ("paused", "account", "slot"):
                with self.subTest(path=path, stage=stage):
                    self.engine.worker = None
                    self.engine.start_calls = 0
                    asyncio.run(self.disconnect_request(path, "2.0", stage))
                    self.assert_route_reuse()

    def test_routes_send_oserror(self):
        for path in self.paths:
            for stage in ("paused", "send-start"):
                with self.subTest(path=path, stage=stage):
                    self.engine.worker = None
                    self.engine.start_calls = 0
                    asyncio.run(self.disconnect_request(path, "2.4", stage))
                    self.assert_route_reuse()

    def test_routes_asgi24_receive_disconnect(self):
        for path in self.paths:
            for stage in ("paused", "account", "slot"):
                with self.subTest(path=path, stage=stage):
                    self.engine.worker = None
                    self.engine.start_calls = 0
                    asyncio.run(self.disconnect_request(path, "2.4", stage, receive_disconnect=True))
                    self.assert_route_reuse()

    def test_disconnect_racing_error_is_delivered(self):
        from starlette.requests import ClientDisconnect

        async def exercise(failure, side):
            response = self.target._ChatStreamingResponse((chunk for chunk in ()), threading.Event())
            # Both tasks complete in the same event-loop turn. A disconnect must
            # not conceal an error that has already occurred in either task.
            async def send_stream(send):
                if side == "send":
                    raise failure

            async def listen(receive):
                if side == "receive":
                    raise failure

            response.stream_response = send_stream
            response.listen_for_disconnect = listen
            with self.assertRaises(ClientDisconnect if isinstance(failure, OSError) and side == "send"
                                   else type(failure)):
                await response({}, None, None)
            self.assertTrue(response._stop_event.is_set())
            self.assertFalse(any(t.get_name() in ("muse-chat-send", "muse-chat-receive")
                                 for t in asyncio.all_tasks()))

        # The response owns a generator, as the route normally supplies.
        for side in ("send", "receive"):
            for failure in (RuntimeError("synthetic-task-error"), OSError("synthetic-io-error")):
                with self.subTest(side=side, error=type(failure).__name__):
                    asyncio.run(exercise(failure, side))


if __name__ == "__main__":
    unittest.main(verbosity=2)
