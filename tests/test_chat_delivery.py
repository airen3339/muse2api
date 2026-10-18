"""Offline delivery regressions against the actual FastAPI/ASGI routes.

Run with the audit environment: python -B tests/test_chat_delivery.py.
Reuse the lifecycle harness without inheriting its tests. Operational imports
are synthetic; real engine tests replace CDP with a forbidden stub and mock
only browser operations/DOM polling. No .env, account files, browser or network.
"""
from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import inspect
import json
from pathlib import Path
import sys
import threading
import time
import types
import unittest
from unittest.mock import Mock, patch

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_chat_lifecycle as lifecycle


class Clock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def time(self):
        # Wall time intentionally does not follow the generation budget.
        return 1000000 - self.now * 100

    def sleep(self, seconds):
        self.now += seconds


async def route_request(target, path, *, stream, extra=None, protocol="2.4",
                        on_send=None, disconnected=None, messages=None):
    payload = {"stream": stream, "timeout": 3,
               ("prompt" if path.endswith("completions") else "input"): "synthetic"}
    payload.update(extra or {})
    raw = json.dumps(payload).encode()
    scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": protocol},
             "http_version": "1.1", "method": "POST", "scheme": "http",
             "path": path, "raw_path": path.encode(), "query_string": b"",
             "root_path": "", "headers": [(b"content-type", b"application/json"),
             (b"authorization", b"Bearer test-only")],
             "server": ("offline", 80), "client": ("offline", 1)}
    messages = [] if messages is None else messages
    disconnected = asyncio.Event() if disconnected is None else disconnected
    requested = False

    async def receive():
        nonlocal requested
        if not requested:
            requested = True
            return {"type": "http.request", "body": raw, "more_body": False}
        await disconnected.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        messages.append(dict(message))
        if on_send is not None:
            await on_send(message)

    await asyncio.wait_for(target.app(scope, receive, send), timeout=4)
    status = next(m["status"] for m in messages if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in messages).decode()
    return status, body


def text_from(path, stream, body):
    if not stream:
        data = json.loads(body)
        if path.endswith("completions"):
            return data["choices"][0]["message"].get("content") or ""
        return data["output"][0]["content"][0]["text"]
    events = lifecycle.sse_payloads(body)
    if path.endswith("completions"):
        return "".join(choice.get("delta", {}).get("content", "")
                       for event in events for choice in event.get("choices", []))
    return "".join(event["delta"] for event in events
                   if event.get("type") == "response.output_text.delta")


class ChatDeliveryTests(unittest.TestCase):
    paths = lifecycle.ChatLifecycleTests.paths
    assert_released = lifecycle.ChatLifecycleTests.assert_released
    assert_failure = lifecycle.ChatLifecycleTests.assert_failure
    tearDown = lifecycle.ChatLifecycleTests.tearDown

    def setUp(self):
        lifecycle.ChatLifecycleTests.setUp(self)
        self.enterContext(patch("subprocess.Popen", side_effect=AssertionError("browser forbidden")))
        self.streams = []
        self.intervals = []
        self.gates = []
        real_safe = self.target.safe_chat_stream

        def tracked_safe(*args, **kwargs):
            interval = kwargs.get("heartbeat_interval")
            self.intervals.append(interval)
            if interval is not None:
                # Exercise the actual queue heartbeat path, with a shorter wait.
                kwargs["heartbeat_interval"] = 0.01
            stream = real_safe(*args, **kwargs)
            self.streams.append(stream)
            return stream

        self.enterContext(patch.object(self.target, "safe_chat_stream", side_effect=tracked_safe))
        self.addCleanup(self.release_gates)

    def release_gates(self):
        for gate in self.gates:
            gate.set()

    def gate(self):
        event = threading.Event()
        self.gates.append(event)
        return event

    def install_chunks(self, chunks, gates=(), failure=None):
        closed = threading.Event()
        self.engine.upstream_closed = closed

        def upstream(*args, account_id, stop_event, **kwargs):
            self.engine.worker = threading.current_thread()
            self.engine.calls.append(account_id)
            try:
                self.engine.chat_response_started = True
                for index, chunk in enumerate(chunks):
                    if index < len(gates) and not gates[index].wait(2):
                        raise AssertionError("test delivery gate was not released")
                    if stop_event.is_set():
                        return
                    yield chunk
                if failure is not None:
                    raise failure
            finally:
                closed.set()

        self.enterContext(patch.object(self.engine, "chat_stream", side_effect=upstream))
        return closed

    def install_real_engine(self):
        cdp = types.ModuleType("cdp")
        cdp.CDP = cdp.http_json = Mock(side_effect=AssertionError("CDP/network forbidden"))
        source = Path(__file__).resolve().parents[1] / "engine.py"
        spec = importlib.util.spec_from_file_location("_muse_delivery_real_engine", source)
        module = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, cdp=cdp):
            spec.loader.exec_module(module)
        cfg = types.SimpleNamespace(profile_dir=self.target.CFG.profile_root,
                                    chat_timeout=300, chat_first_token_timeout=90)
        engine = module.MuseEngine(cfg)
        engine.worker, engine.calls = None, []
        engine.start_calls = engine.close_calls = 0
        clock = Clock()
        self.enterContext(patch.object(module, "time", clock))
        self.enterContext(patch.object(self.target, "MuseAuthError", module.MuseAuthError))
        self.enterContext(patch.object(self.target, "MuseGenerationError", module.MuseGenerationError))
        self.enterContext(patch.object(self.target, "engine", engine))
        self.engine = engine

        def start():
            engine.worker = threading.current_thread()
            engine.start_calls += 1

        def ensure_page(*args, account_id=None, **kwargs):
            engine.calls.append(account_id)

        def close_page():
            engine.close_calls += 1

        engine.start = Mock(side_effect=start)
        engine.ensure_page = Mock(side_effect=ensure_page)
        engine.reset_thread = Mock()
        engine._prepare_chat_submission = Mock(return_value=1)
        engine._chat_page_state = Mock(return_value=dict(epoch=1, users=1, assistants=0, stop=True, failed=False, connecting=False, pending=False, input_present=True, input_empty=True))
        engine.close_current_page = Mock(side_effect=close_page)
        def submit(*args, **kwargs):
            engine.chat_submission_started = True
            return "dispatched"
        engine._send_chat_once = Mock(side_effect=submit)
        return module, engine, clock

    def assert_no_success(self, body):
        self.assertNotIn("[DONE]", body)
        events = lifecycle.sse_payloads(body)
        self.assertFalse(any(choice.get("finish_reason") for event in events
                             for choice in event.get("choices", [])))
        self.assertFalse(any(event.get("type") in ("response.completed",
                             "response.output_text.done", "response.output_item.done")
                             for event in events))
        self.assertFalse(any("usage" in event for event in events))

    def assert_success_tail(self, path, body):
        events = lifecycle.sse_payloads(body)
        if path.endswith("completions"):
            self.assertEqual(body.count("data: [DONE]"), 1)
            finishes = [choice["finish_reason"] for event in events
                        for choice in event.get("choices", []) if choice.get("finish_reason")]
            self.assertEqual(finishes, ["stop"])
        else:
            self.assertEqual(sum(event.get("type") == "response.completed" for event in events), 1)

    def test_real_engine_final_snapshot_agrees_across_routes_and_modes(self):
        module, engine, clock = self.install_real_engine()
        final = "  合法重复 重复\n    code = '✓'\n" + "尾巴🙂" * 180 + "\n"
        commits = []
        actual_stream = engine.chat_stream

        def track_commits(*args, **kwargs):
            for chunk in actual_stream(*args, **kwargs):
                commits.append(chunk)
                yield chunk

        engine.chat_stream = track_commits
        for path in self.paths:
            for stream in (False, True):
                with self.subTest(path=path, stream=stream):
                    commits.clear()
                    samples = iter([(0, "", False), (1, "old draft", True),
                                    (1, "rewritten draft", True)])
                    engine._poll_chat = Mock(side_effect=lambda: next(samples, (1, final, False)))
                    engine._send_chat_once.reset_mock()
                    with self.assertLogs(module.log, level="INFO") as logs:
                        status, body = asyncio.run(route_request(self.target, path, stream=stream))
                    self.assertEqual(status, 200)
                    self.assertEqual(text_from(path, stream, body), final)
                    self.assertEqual(commits, [final], "engine must commit exactly once")
                    self.assertNotIn("old draft", body)
                    self.assertNotIn("rewritten draft", body)
                    self.assertIn(hashlib.sha256(final.encode()).hexdigest(), "\n".join(logs.output))
                    self.assertNotIn(final, "\n".join(logs.output))
                    engine._send_chat_once.assert_called_once()
                    self.assertIn(unittest.mock.call(for_chat=False), engine.reset_thread.call_args_list)
                    if stream:
                        self.assert_success_tail(path, body)
                        if path.endswith("responses"):
                            events = lifecycle.sse_payloads(body)
                            done = next(e for e in events if e.get("type") == "response.output_text.done")
                            item = next(e for e in events if e.get("type") == "response.output_item.done")
                            complete = events[-1]["response"]
                            self.assertEqual(done["text"], final)
                            self.assertEqual(item["item"]["content"][0]["text"], final)
                            self.assertEqual(complete["output"][0]["content"][0]["text"], final)
                    self.assertEqual(self.intervals[-1], 5.0 if stream else None)
                    self.assert_released()

    def test_real_engine_rejects_old_or_multiple_bubbles_without_partial_success(self):
        module, engine, clock = self.install_real_engine()
        cases = [("old baseline", [(1, "old reply", False)], "空白对话", False),
                 ("multiple on first observation", [(0, "", False), (2, "last only", False)],
                  "当前助手回复", True),
                 ("bubble count changes later", [(0, "", False), (1, "partial", True),
                                                  (2, "last only", False)], "气泡发生切换", True)]
        for name, sequence, message, sent in cases:
            with self.subTest(case=name):
                samples = iter(sequence)
                engine._poll_chat = Mock(side_effect=lambda: next(samples, sequence[-1]))
                engine._send_chat_once.reset_mock()
                status, body = asyncio.run(route_request(self.target, self.paths[0], stream=True))
                self.assert_failure(self.paths[0], True, status, body, message=message)
                self.assert_no_success(body)
                self.assertEqual(text_from(self.paths[0], True, body), "")
                self.assertEqual(engine._send_chat_once.call_count, int(sent))
                self.assert_released()

    def test_real_engine_rejects_baseline_stop_before_sending(self):
        module, engine, clock = self.install_real_engine()
        for path in self.paths:
            for stream in (False, True):
                with self.subTest(path=path, stream=stream):
                    engine._poll_chat = Mock(return_value=(0, "", True))
                    engine._send_chat_once.reset_mock()
                    engine.calls.clear()
                    status, body = asyncio.run(route_request(self.target, path, stream=stream))
                    self.assert_failure(path, stream, status, body, message="无法确认空白对话")
                    engine._send_chat_once.assert_not_called()
                    engine._poll_chat.assert_called_once()
                    self.assertEqual(engine.calls, ["synthetic-a"])
                    self.assertEqual(clock.now, 0)
                    if stream:
                        self.assertEqual(text_from(path, True, body), "")
                        self.assert_no_success(body)
                    self.assert_released()

    def test_real_engine_stop_and_blank_reset_completion_window(self):
        module, engine, clock = self.install_real_engine()
        timeline = []

        def poll():
            t = clock.now
            if t == 0:
                return 0, "", False
            if t < 0.9:
                sample = (1, "provisional", False)
            elif t < 1.3:
                sample = (1, "provisional", True)
            elif t < 2.0:
                sample = (1, "provisional", False)
            elif t < 2.2:
                sample = (0, "", False)
            else:
                sample = (1, "final rewrite", False)
            timeline.append((t, sample))
            return sample

        engine._poll_chat = poll
        status, body = asyncio.run(route_request(self.target, self.paths[1], stream=True, extra={"timeout": 5}))
        self.assertEqual(status, 200)
        self.assertEqual(text_from(self.paths[1], True, body), "final rewrite")
        self.assertGreaterEqual(clock.now, 3.2)
        self.assertTrue(any(sample[2] for _, sample in timeline))
        self.assertTrue(any(not sample[1] for _, sample in timeline))
        self.assert_released()

    def test_real_engine_first_observed_before_90_can_commit_after_120(self):
        module, engine, clock = self.install_real_engine()

        def poll():
            # Before sending, the thread must be empty and not generating.
            if clock.now == 0:
                return 0, "", False
            if clock.now < 55:
                return 0, "", True
            if clock.now < 120:
                return 1, "not yet final", True
            return 1, "final after 120 seconds", False

        # Use a harmless page for the existing Still-sending diagnostic probe.
        # page is thread local, so install it in the worker's ensure_page call.
        def ensure_page(*args, account_id=None, **kwargs):
            engine.calls.append(account_id)
            engine.page = types.SimpleNamespace(js=lambda *args: "")

        engine.ensure_page.side_effect = ensure_page
        engine._poll_chat = poll
        status, body = asyncio.run(route_request(self.target, self.paths[0], stream=True, extra={"timeout": 150}))
        self.assertEqual(status, 200)
        self.assertEqual(text_from(self.paths[0], True, body), "final after 120 seconds")
        self.assertGreaterEqual(clock.now, 121)
        self.assertLess(clock.now, 122)
        self.assertEqual(engine.calls, ["synthetic-a"])
        engine._send_chat_once.assert_called_once()
        self.assert_released()

    def test_heartbeats_are_comments_only_on_real_sse_routes(self):
        text = "保留正文 空白\n" + "x" * 600
        for path in self.paths:
            with self.subTest(path=path):
                gate = self.gate()
                closed = self.install_chunks([text], [gate])
                comments = []

                async def on_send(message):
                    if message.get("body", b"").startswith(b":"):
                        comments.append(message["body"])
                        gate.set()

                status, body = asyncio.run(route_request(self.target, path, stream=True, on_send=on_send))
                self.assertEqual(status, 200)
                self.assertEqual(comments, [b": keep-alive\n\n"])
                self.assertEqual(text_from(path, True, body), text)
                self.assert_success_tail(path, body)
                self.assertTrue(closed.is_set())
                self.assert_released()

    def test_tool_buffer_and_text_modes_ignore_wait_events(self):
        self.target.CFG.tool_protocol = True
        tools = [{"type": "function", "function": {"name": "lookup", "parameters": {"type": "object"}}}]
        cases = [("tool", ['```json\n{"name":"lookup","arguments":', '{"q":"重复 重复"}}\n```\n执行说明']),
                 ("text", ["普通文本", " 重复 重复\n"]),
                 ("invalid", ['{"not_a_tool":', 'true}'])]
        for mode, chunks in cases:
            with self.subTest(mode=mode):
                gates = [self.gate(), self.gate()]
                self.install_chunks(chunks, gates)
                comments = []

                async def on_send(message):
                    if message.get("body", b"").startswith(b":"):
                        comments.append(message["body"])
                        gates[min(len(comments) - 1, 1)].set()

                original_parse = self.target.parse_tool_calls
                with patch.object(self.target, "parse_tool_calls", wraps=original_parse) as parsed:
                    status, body = asyncio.run(route_request(self.target, self.paths[0], stream=True,
                                                extra={"tools": tools}, on_send=on_send))
                    self.assertEqual(status, 200)
                    self.assertEqual(len(comments), 2)
                    if mode != "text":
                        self.assertEqual(parsed.call_args.args[0], "".join(chunks))
                events = lifecycle.sse_payloads(body)
                self.assertEqual(body.count("data: [DONE]"), 1)
                # Compare to the actual non-stream route with the same final text.
                self.install_chunks(["".join(chunks)])
                status2, body2 = asyncio.run(route_request(self.target, self.paths[0], stream=False, extra={"tools": tools}))
                self.assertEqual(status2, 200)
                data = json.loads(body2)["choices"][0]
                self.assertEqual(text_from(self.paths[0], True, body), data["message"].get("content") or "")
                if mode == "tool":
                    deltas = [call for event in events for choice in event.get("choices", [])
                              for call in choice["delta"].get("tool_calls", [])]
                    self.assertEqual(deltas[0]["function"]["name"], "lookup")
                    args = "".join(call["function"].get("arguments", "") for call in deltas)
                    self.assertEqual(json.loads(args), {"q": "重复 重复"})
                    self.assertEqual(args, data["message"]["tool_calls"][0]["function"]["arguments"])
                    self.assertEqual(data["finish_reason"], "tool_calls")
                else:
                    self.assertEqual(text_from(self.paths[0], True, body), "".join(chunks))
                self.assert_released()

    def test_safe_wait_event_is_none_and_never_occupies_data_queue(self):
        gate = self.gate()
        self.install_chunks(["completed answer"], [gate])
        stream = self.target.safe_chat_stream({}, "synthetic", None, 3, "synthetic-a", heartbeat_interval=0.01)
        try:
            self.assertIsNone(next(stream))
            self.assertEqual(stream.gi_frame.f_locals["q"].qsize(), 0)
            gate.set()
            self.assertEqual(list(stream), ["completed answer"])
        finally:
            gate.set()
            stream.close()
        self.assert_released()

    def test_default_nonstream_wait_has_no_none_events(self):
        gate, ready = self.gate(), threading.Event()
        chunks, errors = [], []
        self.install_chunks(["only text"], [gate])
        stream = self.target.safe_chat_stream({}, "synthetic", None, 3, "synthetic-a")

        def read():
            ready.set()
            try:
                chunks.extend(stream)
            except BaseException as exc:
                errors.append(exc)

        reader = threading.Thread(target=read, daemon=True)
        reader.start()
        try:
            self.assertTrue(ready.wait(1))
            time.sleep(0.12)
            self.assertEqual(chunks, [], "default stream emitted a heartbeat")
            gate.set()
            reader.join(1)
            self.assertFalse(reader.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(chunks, ["only text"])
        finally:
            gate.set()
            reader.join(1)
        self.assert_released()

    def exercise_real_retry(self, observed, submitted=False):
        module, engine, clock = self.install_real_engine()
        self.target.store.accounts.append({"id": "synthetic-b", "enabled": True,
                                           "cookies": {"fixture": "not-a-credential"}})
        for path in self.paths:
            for stream in (False, True):
                for error_class, code in ((module.MuseGenerationError, 502), (module.MuseAuthError, 401)):
                    with self.subTest(path=path, stream=stream, observed=observed, error=error_class.__name__):
                        current = {"account": None, "polls": 0}
                        failure = error_class("synthetic-buffered-failure")
                        flags = []
                        engine.calls.clear()

                        def ensure_page(*args, account_id=None, **kwargs):
                            engine.calls.append(account_id)
                            current.update(account=account_id, polls=0)
                            flags.append(engine.chat_response_started)
                            if account_id == "synthetic-a" and not observed and not submitted:
                                raise failure

                        def poll():
                            current["polls"] += 1
                            n = current["polls"]
                            if n == 1:
                                return 0, "", False
                            if current["account"] == "synthetic-a":
                                if not observed:
                                    self.assertFalse(engine.chat_response_started)
                                    raise failure
                                if n == 2:
                                    return 1, "observed but NOT yielded", True
                                flags.append(engine.chat_response_started)
                                raise failure
                            return 1, "completed on alternate", False

                        engine.ensure_page.side_effect = ensure_page
                        engine._poll_chat = poll
                        status, body = asyncio.run(route_request(self.target, path, stream=stream))
                        if observed or submitted:
                            self.assert_failure(path, stream, status, body, code=code, message=str(failure))
                            self.assertEqual(engine.calls, ["synthetic-a"])
                            self.assertEqual(flags, [False, True] if observed else [False])
                            self.assertNotIn("observed but NOT yielded", body)
                            if stream:
                                self.assert_no_success(body)
                                self.assertEqual(text_from(path, True, body), "")
                        else:
                            self.assertEqual(status, 200)
                            self.assertEqual(engine.calls, ["synthetic-a", "synthetic-b"])
                            self.assertEqual(flags, [False, False])
                            self.assertEqual(text_from(path, stream, body), "completed on alternate")
                        self.assert_released()

    def test_real_observed_text_without_yield_forbids_account_retry(self):
        self.exercise_real_retry(observed=True)

    def test_real_before_submission_failure_still_retries_one_alternate(self):
        self.exercise_real_retry(observed=False)

    def test_real_submitted_without_text_forbids_account_retry(self):
        self.exercise_real_retry(observed=False, submitted=True)

    def test_default_pacing_is_lossless_256_without_sleep(self):
        text = "代码🙂\n" * 20001
        with patch.object(self.target.time, "sleep", side_effect=AssertionError("artificial typing sleep")):
            pieces = list(self.target._pace_text(text))
        self.assertEqual("".join(pieces), text)
        self.assertTrue(all(len(piece) == 256 for piece in pieces[:-1]))
        self.assertLessEqual(len(pieces[-1]), 256)
        self.assertEqual(inspect.signature(self.target._pace_text).parameters["delay"].default, 0)

    def test_no_text_cancel_during_buffer_has_no_success_tail(self):
        for path in self.paths:
            for protocol in ("2.0", "2.4"):
                for observed in (False, True):
                    with self.subTest(path=path, protocol=protocol, observed=observed):
                        closed, ready = threading.Event(), threading.Event()
                        signals = []

                        def upstream(*args, stop_event, **kwargs):
                            self.engine.worker = threading.current_thread()
                            signals.append(stop_event)
                            self.engine.chat_response_started = observed
                            ready.set()
                            try:
                                if not stop_event.wait(2):
                                    raise AssertionError("cancel did not reach buffered producer")
                                return
                                yield "unreachable"
                            finally:
                                closed.set()

                        async def exercise():
                            disconnected = asyncio.Event()

                            async def on_send(message):
                                if message.get("body", b"").startswith(b":"):
                                    self.assertTrue(ready.is_set())
                                    disconnected.set()

                            return await route_request(self.target, path, stream=True, protocol=protocol,
                                                       disconnected=disconnected, on_send=on_send,
                                                       extra={"stream_options": {"include_usage": True}})

                        with patch.object(self.engine, "chat_stream", side_effect=upstream):
                            status, body = asyncio.run(exercise())
                        self.assertEqual(status, 200)
                        self.assertIn(": keep-alive\n\n", body)
                        self.assertEqual(text_from(path, True, body), "")
                        self.assert_no_success(body)
                        self.assertTrue(signals and signals[0].is_set())
                        self.assert_released()
                        self.assertTrue(closed.is_set())
                        self.assertTrue(all(inspect.getgeneratorstate(s) == "GEN_CLOSED" for s in self.streams))

    def test_more_than_100_fragments_drain_or_fail_without_losing_terminal(self):
        chunks = [f"part-{index:03d}|" for index in range(205)]
        for path in self.paths:
            for fail in (False, True):
                with self.subTest(path=path, fail=fail):
                    failure = lifecycle.MuseGenerationError("synthetic-after-205") if fail else None
                    closed = self.install_chunks(chunks, failure=failure)
                    status, body = asyncio.run(route_request(self.target, path, stream=True))
                    self.assertEqual(text_from(path, True, body), "".join(chunks))
                    if fail:
                        self.assert_failure(path, True, status, body, message=str(failure))
                        self.assert_no_success(body)
                    else:
                        self.assert_success_tail(path, body)
                    self.assert_released()
                    self.assertTrue(closed.is_set())

    def test_full_queue_disconnect_releases_without_draining(self):
        for path in self.paths:
            with self.subTest(path=path):
                closed = threading.Event()
                produced = []

                def upstream(*args, stop_event, **kwargs):
                    self.engine.worker = threading.current_thread()
                    self.engine.chat_response_started = True
                    try:
                        for n in range(205):
                            if stop_event.is_set():
                                return
                            produced.append(n)
                            yield f"piece-{n}|"
                    finally:
                        closed.set()

                async def exercise():
                    disconnected = asyncio.Event()
                    captured = []
                    checked = False

                    async def on_send(message):
                        nonlocal checked
                        raw = message.get("body", b"")
                        if checked or b"piece-0|" not in raw:
                            return
                        checked = True
                        deadline = asyncio.get_running_loop().time() + 1
                        while True:
                            handoff = self.streams[-1].gi_frame.f_locals["q"]
                            if handoff.qsize() == 100 and len(produced) >= 102:
                                break
                            self.assertLess(asyncio.get_running_loop().time(), deadline)
                            await asyncio.sleep(0.001)
                        self.assertTrue(self.engine.worker.is_alive())
                        captured.append(handoff)
                        disconnected.set()
                        # Model an ASGI send stalled by backpressure; receive
                        # must still cancel it, without draining the queue.
                        await asyncio.Event().wait()

                    result = await route_request(self.target, path, stream=True, on_send=on_send,
                                                 disconnected=disconnected)
                    self.assertTrue(checked)
                    return result, captured[0]

                with patch.object(self.engine, "chat_stream", side_effect=upstream):
                    (status, body), handoff = asyncio.run(exercise())
                self.assertEqual(status, 200)
                self.assert_no_success(body)
                self.assert_released()
                self.assertTrue(closed.is_set())
                self.assertEqual(handoff.qsize(), 100)
                self.assertGreaterEqual(len(produced), 102)
                self.assertLess(len(produced), 205)
                self.install_chunks(["resource reused"])
                status2, body2 = asyncio.run(route_request(self.target, path, stream=False))
                self.assertEqual(status2, 200)
                self.assertEqual(text_from(path, False, body2), "resource reused")
                self.assert_released()


if __name__ == "__main__":
    unittest.main(verbosity=2)
