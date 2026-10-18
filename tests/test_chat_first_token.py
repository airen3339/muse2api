"""Offline regression: chat_stream first-token deadline and send confirmation.

python -B tests/test_chat_first_token.py [-v] [test names]

MUSE_ENGINE_SOURCE overrides the engine.py under test for comparisons with a
pre-fix checkout. MUSE_CONFIG_SOURCE likewise overrides config.py for the
Config defaults test. A baseline without chat_first_token_timeout is expected
to fail that defaults test.

No app import, browser, network, real cookies, or persistent account state.
"""
import importlib.util
import os
from pathlib import Path
import subprocess
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
    "engine_under_test",
    os.environ.get("MUSE_ENGINE_SOURCE", str(ROOT / "engine.py")))
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
        home = self.enterContext(tempfile.TemporaryDirectory(prefix="muse-ft-test-"))
        self.enterContext(patch.dict(os.environ, {
            "MUSE2API_HOME": home, "MUSE2API_PROFILE_ROOT": home,
            "MUSE2API_KEY": "test-only",
        }))
        for target in ("socket.socket.connect", "socket.create_connection",
                       "requests.sessions.Session.request",
                       "websocket.create_connection"):
            self.enterContext(patch(target,
                                    side_effect=AssertionError("unexpected external I/O")))
        self.e = engine.MuseEngine(SimpleNamespace(
            profile_dir=home + "/profile", download_dir=home + "/downloads",
            cdp_port=0, chat_timeout=30))


class FirstTokenTests(OfflineTest):
    def setUp(self):
        super().setUp()
        self.enterContext(patch("subprocess.Popen",
                                side_effect=AssertionError("unexpected external I/O")))
        self.clock = Clock()
        self.enterContext(patch.object(engine, "time", self.clock))
        self.e.ensure_page = Mock()
        self.e.reset_thread = Mock()
        self.e._prepare_chat_submission = Mock(return_value=1)
        self.e._chat_page_state = Mock(return_value=dict(epoch=1, users=1, assistants=0, stop=True, failed=False, connecting=False, pending=False, input_present=True, input_empty=True))
        self._send = Mock(return_value='dispatched')
        self.e._send_chat_once = self._send
        self.stop = threading.Event()

    def cfg(self, timeout=300, first_token=None):
        fields = {"chat_timeout": timeout}
        if first_token is not None:
            fields["chat_first_token_timeout"] = first_token
        fields["image_timeout"] = 240
        fields["video_timeout"] = 600
        self.e.cfg = SimpleNamespace(**fields)

    def stream(self, timeout=300, first_token=None, state=None,
               send_result="dispatched"):
        # Restart the virtual clock so the baseline _poll_chat() sample before
        # _send() cannot capture a post-first-token state in reused setups.
        self.clock.now = 0.0
        self.cfg(timeout=timeout, first_token=first_token)
        self._send.return_value = send_result
        if state is None:
            def state(now):
                return (0, "", False)
        clock = self.clock

        def poll():
            return (0, "", False) if clock.now == 0 else state(clock.now)

        self.e._poll_chat = poll
        return self.e.chat_stream({}, "synthetic prompt", timeout=timeout,
                                  stop_event=self.stop)

    def test_first_token_after_55s_returns_fully(self):
        def state(t):
            if t < 55:
                return (0, "", True)
            return (1, "55s后才出的首字已完整返回", False)

        chunks = list(self.stream(timeout=300, state=state))
        self.assertEqual(chunks, ["55s后才出的首字已完整返回"])
        self.assertGreaterEqual(self.clock.now, 55)
        # Text is observed at 55s, then held for the completed snapshot window.
        self.assertLess(self.clock.now, 56.2)
        self._send.assert_called_once()

    def test_default_first_token_cap_is_90(self):
        self.e.cfg = SimpleNamespace(chat_timeout=300, image_timeout=240,
                                     video_timeout=600)
        self.e._poll_chat = lambda: (0, "", False)
        with self.assertRaisesRegex(engine.MuseGenerationError,
                                    "等待助手首字响应超时"):
            list(self.e.chat_stream({}, "synthetic prompt", timeout=300,
                                    stop_event=self.stop))
        # No explicit cfg field must still cap at the default 90s, not 40s.
        self.assertGreaterEqual(self.clock.now, 89.9)
        self.assertLess(self.clock.now, 90.5)
        self._send.assert_called_once()

    def test_cfg_first_token_limit_20_applies(self):
        with self.assertRaisesRegex(engine.MuseGenerationError,
                                    "等待助手首字响应超时"):
            list(self.stream(timeout=300, first_token=20,
                             state=lambda t: (0, "", True)))
        self.assertGreaterEqual(self.clock.now, 19.9)
        self.assertLess(self.clock.now, 20.5)
        self._send.assert_called_once()

    def test_total_timeout_12_overrides_90(self):
        with self.assertRaisesRegex(engine.MuseGenerationError,
                                    "等待助手首字响应超时"):
            list(self.stream(timeout=12, first_token=90,
                             state=lambda t: (0, "", True)))
        self.assertGreaterEqual(self.clock.now, 11.9)
        self.assertLess(self.clock.now, 12.5)
        self._send.assert_called_once()

    def test_cancel_before_first_token_returns(self):
        def state(t):
            if t >= 3:
                self.stop.set()
            return (0, "", True)

        stream = self.stream(timeout=300, first_token=90, state=state)
        self.assertEqual(list(stream), [])
        self.assertTrue(self.stop.is_set())
        self.assertGreaterEqual(self.clock.now, 2.9)
        self.assertLess(self.clock.now, 3.2)
        self._send.assert_called_once()

    def test_send_no_button_fails_fast_after_single_send(self):
        self._send.return_value = "no-button"
        self.e._poll_chat = lambda: (0, "", False)
        with self.assertRaisesRegex(engine.MuseGenerationError,
                                    "提示词发送未确认"):
            list(self.e.chat_stream({}, "synthetic prompt", timeout=300,
                                    stop_event=self.stop))
        self._send.assert_called_once()
        # Immediate error: no first-token polling loop ran after send.
        self.assertLess(self.clock.now, 0.5)

    def test_send_accepts_explicit_dispatch_result(self):
        for ok in ("dispatched",):
            with self.subTest(send_result=ok):
                chunks = list(self.stream(
                    state=lambda t: (1, "done " + ok, False), send_result=ok))
                self.assertEqual(chunks, ["done " + ok])
                self._send.assert_called_once()
                self._send.reset_mock()


class ConfigDefaultsTest(OfflineTest):
    """Config field defaults and env override, isolated via a temp MUSE2API_HOME
    subprocess (no real .env is loaded: HOME file lives in the temp dir).
    """

    def test_cfg_default_and_env_override(self):
        config_source = os.environ.get("MUSE_CONFIG_SOURCE",
                                       str(ROOT / "config.py"))
        config_dir = os.path.dirname(os.path.abspath(config_source))
        cases = [
            ({}, 90),
            ({"MUSE2API_CHAT_FIRST_TOKEN_TIMEOUT": "20"}, 20),
        ]
        checker = ("import os\n"
                   "import sys\n"
                   "sys.path.insert(0, os.environ['MUSE_CONFIG_DIR'])\n"
                   "import config\n"
                   "print(config.CFG.chat_first_token_timeout)\n")
        home = self.enterContext(tempfile.TemporaryDirectory(
            prefix="muse-cfg-server-"))
        check = os.path.join(home, "check_config.py")
        with open(check, "w", encoding="utf-8") as f:
            f.write(checker)
        for env_extra, expected in cases:
            with self.subTest(env=env_extra):
                env = dict(os.environ)
                # A user-level MUSE2API_CHAT_FIRST_TOKEN_TIMEOUT must not leak
                # into the default-value case inside this test run.
                env.pop("MUSE2API_CHAT_FIRST_TOKEN_TIMEOUT", None)
                env["MUSE2API_HOME"] = home
                env["MUSE2API_PROFILE_ROOT"] = home
                env["MUSE2API_KEY"] = "test-only"
                env["MUSE_CONFIG_DIR"] = config_dir
                env.update(env_extra)
                out = subprocess.run([sys.executable, "-B", check],
                                     capture_output=True, text=True,
                                     cwd=home, timeout=120, env=env)
                self.assertEqual(out.returncode, 0, out.stderr)
                self.assertEqual(int(out.stdout.strip()), expected)


if __name__ == "__main__":
    unittest.main()
