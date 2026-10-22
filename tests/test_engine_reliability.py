"""Offline: python -B tests/test_engine_reliability.py [-v] [test names].

Set MUSE_ENGINE_SOURCE to a baseline engine.py to verify regression failures.
No app import, browser, network, real cookies, or persistent account state.
"""
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location(
    "engine_under_test", os.environ.get("MUSE_ENGINE_SOURCE", str(ROOT / "engine.py")))
engine = importlib.util.module_from_spec(spec)
spec.loader.exec_module(engine)


class Clock:
    def __init__(self):
        self.now = 0.0

    def time(self):
        return self.now

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class OfflineTest(unittest.TestCase):
    def setUp(self):
        home = self.enterContext(tempfile.TemporaryDirectory(prefix="muse-engine-test-"))
        self.enterContext(patch.dict(os.environ, {
            "MUSE2API_HOME": home, "MUSE2API_PROFILE_ROOT": home,
            "MUSE2API_KEY": "test-only",
        }))
        for target in ("socket.socket.connect", "socket.create_connection",
                       "subprocess.Popen", "requests.sessions.Session.request",
                       "websocket.create_connection"):
            self.enterContext(patch(target, side_effect=AssertionError("unexpected external I/O")))
        self.e = engine.MuseEngine(SimpleNamespace(
            profile_dir=home + "/profile", download_dir=home + "/downloads",
            cdp_port=0, chat_timeout=30))


class IsolationTests(OfflineTest):
    def setUp(self):
        super().setUp()
        self.browser = Mock()
        self.e.browser = self.browser
        self.page = Mock()
        self.disposed = []
        self.failure = None
        self.missing = None

        def send(method, params=None):
            if method == "Target.disposeBrowserContext":
                self.disposed.append(params["browserContextId"])
                return {"result": {}}
            if method == self.failure:
                raise RuntimeError("synthetic " + method)
            if method == self.missing:
                return {"result": {}}
            if method == "Target.createBrowserContext":
                return {"result": {"browserContextId": "isolated"}}
            if method == "Target.createTarget":
                self.assertEqual(params["browserContextId"], "isolated")
                return {"result": {"targetId": "target"}}
            raise AssertionError("unexpected CDP method " + method)

        self.browser.send.side_effect = send
        self.connect = self.enterContext(patch.object(engine, "CDP", return_value=self.page))
        # Deliberately allow a *simulated* fallback: baseline must fail because
        # isolation is lost, not because network access happens to be blocked.
        self.fallback = self.enterContext(patch("requests.put", return_value=SimpleNamespace(
            json=lambda: {"webSocketDebuggerUrl": "ws://offline/default"})))

    def assert_failed_cleanly(self, context_created, page_created=False):
        with self.assertRaises(engine.MuseGenerationError):
            self.e._open_page()
        self.fallback.assert_not_called()
        self.assertEqual(self.disposed, ["isolated"] if context_created else [])
        self.assertEqual(self.e._active_contexts, set())
        self.assertIsNone(self.e.current_context_id)
        self.assertIsNone(self.e.page)
        self.assertEqual(self.page.close.call_count, int(page_created))

    def test_context_failure_never_opens_shared_tab(self):
        self.failure = "Target.createBrowserContext"
        self.assert_failed_cleanly(False)

    def test_missing_browser_fails_closed(self):
        self.e.browser = None
        self.assert_failed_cleanly(False)
        self.connect.assert_not_called()

    def test_missing_context_fails_closed(self):
        self.missing = "Target.createBrowserContext"
        self.assert_failed_cleanly(False)
        self.connect.assert_not_called()

    def test_target_creation_failure_disposes_context(self):
        self.failure = "Target.createTarget"
        self.assert_failed_cleanly(True)
        self.connect.assert_not_called()

    def test_missing_target_disposes_context(self):
        self.missing = "Target.createTarget"
        self.assert_failed_cleanly(True)
        self.connect.assert_not_called()

    def test_connection_failure_disposes_context(self):
        self.connect.side_effect = RuntimeError("synthetic connect failure")
        self.assert_failed_cleanly(True)

    def test_each_page_setup_failure_closes_transport_and_context(self):
        for failing in ("Network.enable", "Page.enable", "Runtime.enable",
                        "Browser.setDownloadBehavior"):
            with self.subTest(command=failing):
                self.disposed.clear()
                self.page.reset_mock()

                def send(method, params=None):
                    if method == failing:
                        raise RuntimeError("synthetic setup failure")
                    return {"result": {}}

                self.page.send.side_effect = send
                self.assert_failed_cleanly(True, True)

    def test_cleanup_failure_preserves_context_until_retry_succeeds(self):
        original = RuntimeError("synthetic page setup failure")
        self.page.send.side_effect = original
        self.page.close.side_effect = RuntimeError("synthetic close failure")
        previous = self.browser.send.side_effect
        live_contexts = set()
        disposal_fails = True

        def send(method, params=None):
            result = previous(method, params)
            if method == "Target.createBrowserContext":
                live_contexts.add("isolated")
            if method == "Target.disposeBrowserContext":
                if disposal_fails:
                    raise RuntimeError("synthetic dispose failure")
                live_contexts.remove(params["browserContextId"])
            return result

        self.browser.send.side_effect = send
        with self.assertRaises(engine.MuseGenerationError) as caught:
            self.e._open_page()
        self.assertIs(caught.exception.__cause__, original)
        self.assertEqual(self.disposed, ["isolated"])
        self.page.close.assert_called_once()
        self.assertEqual(live_contexts, {"isolated"})
        self.assertEqual(self.e._active_contexts, live_contexts)
        self.assertEqual(self.e.current_context_id, "isolated")
        self.e.close_current_page()
        self.assertEqual(self.e._active_contexts, live_contexts)
        self.assertEqual(self.e.current_context_id, "isolated")
        disposal_fails = False
        self.e.close_current_page()
        self.assertEqual(live_contexts, set())
        self.assertEqual(self.e._active_contexts, set())
        self.assertIsNone(self.e.current_context_id)
        self.fallback.assert_not_called()

    def test_stop_retries_context_left_after_failed_page_cleanup(self):
        self.page.send.side_effect = RuntimeError("synthetic setup failure")
        previous = self.browser.send.side_effect
        live_contexts = set()
        failed_disposals = 0

        def send(method, params=None):
            nonlocal failed_disposals
            result = previous(method, params)
            if method == "Target.createBrowserContext":
                live_contexts.add("isolated")
            if method == "Target.disposeBrowserContext":
                failed_disposals += 1
                if failed_disposals <= 2:
                    raise RuntimeError("synthetic transient cleanup failure")
                live_contexts.remove(params["browserContextId"])
            return result

        self.browser.send.side_effect = send
        with self.assertRaises(engine.MuseGenerationError):
            self.e._open_page()
        self.e.close_current_page()
        self.assertEqual(live_contexts, {"isolated"})
        self.assertEqual(self.e._active_contexts, live_contexts)
        self.e.stop()
        self.assertEqual(live_contexts, set())
        self.assertEqual(self.e._active_contexts, set())
        self.assertIsNone(self.e.current_context_id)
        self.browser.close.assert_called_once()

    def test_real_cdp_setup_error_closes_websocket_and_disposes_context(self):
        from cdp import CDP, CDPError

        transport = Mock()

        def recv():
            command = json.loads(transport.send.call_args.args[0])
            reply = {"id": command["id"], "result": {}}
            if command["method"] == "Runtime.enable":
                reply = {"id": command["id"], "error": {
                    "code": -32000, "message": "synthetic setup failure"}}
            return json.dumps(reply)

        transport.recv.side_effect = recv
        self.connect.side_effect = CDP
        with patch("websocket.create_connection", return_value=transport):
            with self.assertRaises(engine.MuseGenerationError) as caught:
                self.e._open_page()
        self.assertIsInstance(caught.exception.__cause__, CDPError)
        self.assertIn("Runtime.enable", str(caught.exception.__cause__))
        transport.close.assert_called_once()
        self.assertEqual(self.disposed, ["isolated"])
        self.assertEqual(self.e._active_contexts, set())
        self.assertIsNone(self.e.current_context_id)
        self.fallback.assert_not_called()

    def test_successful_page_retains_context_until_caller_cleanup(self):
        self.assertIs(self.e._open_page(), self.page)
        self.assertEqual(self.e._active_contexts, {"isolated"})
        self.assertEqual(self.e.current_context_id, "isolated")
        self.assertEqual(self.disposed, [])
        self.page.close.assert_not_called()
        self.fallback.assert_not_called()
        self.assertEqual([call.args[0] for call in self.page.send.call_args_list], [
            "Network.enable", "Page.enable", "Runtime.enable", "Browser.setDownloadBehavior"])
        self.e.page = self.page
        self.e.close_current_page()
        self.assertEqual(self.disposed, ["isolated"])
        self.page.close.assert_called_once()
        self.assertEqual(self.e._active_contexts, set())
        self.assertIsNone(self.e.current_context_id)


class StreamTests(OfflineTest):
    def setUp(self):
        super().setUp()
        self.clock = Clock()
        self.enterContext(patch.object(engine, "time", self.clock))
        self.e.ensure_page = Mock()
        self.e.reset_thread = Mock()
        self.e._prepare_chat_submission = Mock(return_value=1)
        self.e._chat_page_state = Mock(return_value=dict(epoch=1, users=1, assistants=0, stop=True, failed=False, connecting=False, pending=False, input_present=True, input_empty=True))
        self.e._send_chat_once = Mock(return_value="dispatched")
        self.stop = threading.Event()

    def stream(self, state, timeout=30):
        def poll():
            return (0, "", False) if self.clock.now == 0 else state(self.clock.now)
        self.e._poll_chat = poll
        return self.e.chat_stream({}, "synthetic prompt", timeout=timeout, stop_event=self.stop)

    def test_active_six_second_pause_does_not_truncate(self):
        chunks = list(self.stream(lambda t: (
            (1, "partial", True) if t < 6 else (1, "partial COMPLETE", False))))
        self.assertEqual(chunks, ["partial COMPLETE"])
        self.assertGreaterEqual(self.clock.now, 6)
        self.assertLess(self.clock.now, 7.2)

    def test_short_pause_control_completes(self):
        chunks = list(self.stream(lambda t: (
            (1, "partial", True) if t < 4 else (1, "partial COMPLETE", False))))
        self.assertEqual(chunks, ["partial COMPLETE"])
        self.assertLess(self.clock.now, 5.2)

    def test_active_generation_reaches_deadline_as_error(self):
        stream = self.stream(lambda t: (1, "partial", True), timeout=8)
        with self.assertRaisesRegex(engine.MuseGenerationError, "等待助手回复超时"):
            list(stream)
        self.assertGreaterEqual(self.clock.now, 8)
        self.assertLess(self.clock.now, 8.1)

    def test_cancellation_during_active_pause_exits(self):
        def state(t):
            if t >= 6:
                self.stop.set()
            return 1, "partial", True
        self.assertEqual(list(self.stream(state)), [])
        self.assertTrue(self.stop.is_set())
        self.assertGreaterEqual(self.clock.now, 6)
        self.assertLess(self.clock.now, 6.1)

    def test_cancellation_before_first_token_exits(self):
        stream = self.stream(lambda t: (0, "", True))
        self.stop.set()
        self.assertEqual(list(stream), [])
        self.assertEqual(self.clock.now, 0)

    def test_completed_stable_text_returns_normally(self):
        self.assertEqual(list(self.stream(lambda t: (1, "done", False))), ["done"])
        self.assertGreaterEqual(self.clock.now, 1.0)
        self.assertLess(self.clock.now, 1.2)

    def test_no_first_token_preserves_timeout_error(self):
        with self.assertRaisesRegex(engine.MuseGenerationError, "等待助手首字响应超时"):
            list(self.stream(lambda t: (0, "", True), timeout=2))
        self.assertGreaterEqual(self.clock.now, 2)
        self.assertLess(self.clock.now, 2.1)


if __name__ == "__main__":
    unittest.main()
