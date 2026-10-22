"""Offline acceptance of the actual submission methods and worker retry gate.

Run with Python 3.11+; Node is neither imported nor executed by this file.
Run test_chat_submission_dom.mjs separately for real JavaScript/DOM semantics.
No production, browser, network, account state, temporary files or skips.
MUSE_ENGINE_SOURCE / MUSE_CHAT_TEST_SOURCE may select another candidate.
Failures are ordinary acceptance failures, never expectedFailure annotations.
"""
from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
import logging
import os
from pathlib import Path
import sys
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
ENGINE_SOURCE = Path(os.environ.get("MUSE_ENGINE_SOURCE", ROOT / "engine.py"))
APP_SOURCE = Path(os.environ.get("MUSE_CHAT_TEST_SOURCE", ROOT / "app.py"))
spec = importlib.util.spec_from_file_location("submission_engine_under_test", ENGINE_SOURCE)
engine = importlib.util.module_from_spec(spec)
spec.loader.exec_module(engine)


class Clock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def time(self):
        # Deadline correctness cannot depend on a forward-moving wall clock.
        return 1000000 - self.now * 100

    def sleep(self, seconds):
        self.now += seconds


def clean_state(**changes):
    state = dict(epoch=41, path_new=True, ready=True, input_present=True,
                 input_enabled=True, input_matches=False, input_empty=True,
                 bubbles=0, users=0, assistants=0, stop=False, attachments=0,
                 connecting=False, pending=False, failed=False, send_candidates=1,
                 send_enabled=True, dispatched=False, filled=False)
    state.update(changes)
    return state


class FakePage:
    """A state/transport boundary, not a Python implementation of the DOM selector.

    The actual engine JS is sent through page.js; DOM meaning is independently
    tested by the .mjs suite. Hooks model navigation, delayed UI and lost replies.
    """
    def __init__(self, clock):
        self.clock = clock
        self.state = clean_state()
        self.value = ""
        self.actions = []
        self.dispatches = 0
        self.dispatch_time = None
        self.before = None
        self.after = None
        self.local_at = 0.0
        self.poll = lambda elapsed: (0, "", False)
        self.truncate = False
        self.submit_exception = None
        self.submit_no_action = False
        self.poll_count = 0

    def js(self, expression, **kwargs):
        if expression == engine.MuseEngine._POLL_CHAT_JS:
            self.poll_count += 1
            elapsed = (self.clock.now - self.dispatch_time
                       if self.dispatch_time is not None else -1)
            count, text, stop = self.poll(elapsed)
            return json.dumps(dict(cnt=count, total=count, txt=text, stop=stop,
                                   epoch=self.state['epoch'], users=self.state['users'],
                                   path='/thread/new'))
        prefix = engine.MuseEngine._CHAT_STATE_JS.rsplit("})(%s", 1)[0] + "})("
        if not expression.startswith(prefix):
            raise AssertionError("unexpected page evaluation")
        prompt, epoch, action = json.loads("[" + expression[len(prefix):-1] + "]")
        self.actions.append(action)
        if self.before:
            self.before(self, action)
        if (self.dispatch_time is not None and self.local_at is not None
                and self.clock.now - self.dispatch_time >= self.local_at):
            self.state["users"] = max(1, self.state["users"])
        state = dict(self.state)
        state.update(input_empty=not self.value, input_matches=self.value == prompt)
        if action == "fill":
            self.value = prompt[:-1] if self.truncate else prompt
            state["filled"] = self.value == prompt
        elif action == "submit":
            if not self.submit_no_action:
                self.dispatches += 1
                self.dispatch_time = self.clock.now
                state["dispatched"] = True
            if self.submit_exception:
                raise self.submit_exception
        if self.after:
            self.after(self, action, state)
        return state

    def send(self, *args, **kwargs):
        raise AssertionError("keyboard/CDP fallback is forbidden")


class SubmissionTests(unittest.TestCase):
    def setUp(self):
        for target in ("socket.socket.connect", "socket.create_connection",
                       "requests.sessions.Session.request", "websocket.create_connection",
                       "subprocess.Popen"):
            self.enterContext(patch(target, side_effect=AssertionError("external I/O forbidden")))
        self.clock = Clock()
        self.enterContext(patch.object(engine, "time", self.clock))
        self.e = engine.MuseEngine.__new__(engine.MuseEngine)
        self.e.cfg = SimpleNamespace(chat_timeout=300, chat_first_token_timeout=90)
        self.page = FakePage(self.clock)
        self.e.page = self.page
        self.e.ensure_page = Mock()
        self.e.reset_thread = Mock()
        self.stop = threading.Event()

    def stream(self, **kwargs):
        return self.e.chat_stream({}, "synthetic complete prompt", stop_event=self.stop, **kwargs)

    def require_rejection_without_output(self):
        output, error = [], None
        try:
            output.extend(self.stream())
        except engine.MuseGenerationError as exc:
            error = exc
        self.assertEqual(output, [], "untrusted conversation text was committed")
        self.assertIsInstance(error, engine.MuseGenerationError)

    def test_prepare_waits_for_ready_then_returns_epoch(self):
        def before(page, action):
            page.state["ready"] = self.clock.now >= 1.0
        self.page.before = before
        self.assertEqual(self.e._prepare_chat_submission(), 41)
        self.assertGreaterEqual(self.clock.now, 1)
        self.assertEqual(self.page.dispatches, 0)

    def test_each_dirty_precondition_prevents_fill_and_dispatch(self):
        cases = [dict(ready=False), dict(path_new=False), dict(input_present=False, input_enabled=False),
                 dict(input_enabled=False), dict(bubbles=1, users=1), dict(bubbles=1, assistants=1),
                 dict(attachments=1), dict(stop=True), dict(connecting=True),
                 dict(pending=True), dict(failed=True)]
        for changes in cases:
            with self.subTest(changes=changes):
                self.clock.now = 0
                self.page.state = clean_state(**changes)
                self.page.actions.clear()
                with self.assertRaises(engine.MuseGenerationError):
                    list(self.stream())
                self.assertEqual(self.page.dispatches, 0)
                self.assertNotIn("fill", self.page.actions)
                self.assertGreaterEqual(self.clock.now, 12)
                self.assertLess(self.clock.now, 12.3)

    def test_prepare_rejects_existing_draft(self):
        self.page.value = "do not overwrite"
        with self.assertRaises(engine.MuseGenerationError):
            self.e._prepare_chat_submission()
        self.assertEqual(self.page.value, "do not overwrite")
        self.assertEqual(self.page.dispatches, 0)

    def test_prepare_transport_error_does_not_send(self):
        self.page.before = lambda *args: (_ for _ in ()).throw(TimeoutError("state unavailable"))
        with self.assertRaises(TimeoutError):
            list(self.stream())
        self.assertFalse(self.e.chat_submission_started)
        self.assertEqual(self.page.dispatches, 0)

    def test_page_state_rejects_malformed_result(self):
        self.page.js = Mock(return_value={"ready": True})
        with self.assertRaisesRegex(engine.MuseGenerationError, "无法读取"):
            self.e._chat_page_state()

    def test_complete_unicode_multiline_prompt_is_preserved(self):
        prompt = "傳送 引号\" ' $() \\ \n    return 1\n" + "完整" * 60000
        self.assertEqual(self.e._send_chat_once(prompt, 41), "dispatched")
        self.assertEqual(self.page.value, prompt)
        self.assertEqual(self.page.dispatches, 1)
        self.assertTrue(self.e.chat_submission_started)

    def test_truncated_input_never_submits(self):
        self.page.truncate = True
        with self.assertRaisesRegex(engine.MuseGenerationError, "完整提示词"):
            self.e._send_chat_once("complete", 41)
        self.assertEqual(self.page.dispatches, 0)
        self.assertFalse(self.e.chat_submission_started)

    def test_same_length_input_replacement_never_submits(self):
        def after(page, action, state):
            if action == "fill":
                page.value = "wrong!!!"
        self.page.after = after
        with self.assertRaisesRegex(engine.MuseGenerationError, "输入状态"):
            self.e._send_chat_once("correct!", 41)
        self.assertEqual(self.page.dispatches, 0)

    def test_epoch_change_after_fill_never_submits(self):
        def after(page, action, state):
            if action == "fill":
                page.state["epoch"] = 42
        self.page.after = after
        with self.assertRaisesRegex(engine.MuseGenerationError, "页面或输入"):
            self.e._send_chat_once("complete", 41)
        self.assertEqual(self.page.dispatches, 0)

    def test_ambiguous_buttons_never_submit(self):
        self.page.state.update(send_candidates=2, send_enabled=False)
        with self.assertRaisesRegex(engine.MuseGenerationError, "唯一定位"):
            self.e._send_chat_once("complete", 41)
        self.assertEqual(self.page.dispatches, 0)

    def test_missing_or_disabled_button_never_uses_keyboard(self):
        for candidates in (0, 1):
            with self.subTest(candidates=candidates):
                self.clock.now = 0
                self.page.state.update(send_candidates=candidates, send_enabled=False)
                with self.assertRaisesRegex(engine.MuseGenerationError, "不会使用快捷键"):
                    self.e._send_chat_once("complete", 41)
                self.assertEqual(self.page.dispatches, 0)
                self.assertFalse(self.e.chat_submission_started)
                self.assertLess(self.clock.now, 3.2)

    def test_explicit_submit_no_action_clears_submission_flag(self):
        self.page.submit_no_action = True
        with self.assertRaisesRegex(engine.MuseGenerationError, "点击前"):
            self.e._send_chat_once("complete", 41)
        self.assertFalse(self.e.chat_submission_started)
        self.assertEqual(self.page.actions.count("submit"), 1)

    def test_lost_submit_reply_keeps_unknown_and_never_resends(self):
        original = TimeoutError("submit response lost")
        self.page.submit_exception = original
        with self.assertRaises(TimeoutError) as seen:
            list(self.stream())
        self.assertIs(seen.exception, original)
        self.assertTrue(self.e.chat_submission_started)
        self.assertEqual(self.page.dispatches, 1)
        self.assertEqual(self.page.actions.count("submit"), 1)

    def test_submit_exception_preserves_unknown_without_keyboard_fallback(self):
        self.page.submit_exception = RuntimeError("JavaScript dispatch threw")
        with self.assertRaises(RuntimeError):
            self.e._send_chat_once("complete", 41)
        self.assertTrue(self.e.chat_submission_started)
        self.assertEqual(self.page.dispatches, 1)
        self.assertEqual(self.page.actions.count("submit"), 1)

    def test_cancel_before_entry_performs_no_preparation(self):
        self.stop.set()
        self.assertEqual(list(self.stream()), [])
        self.e.ensure_page.assert_not_called()
        self.assertEqual(self.page.dispatches, 0)

    def test_cancel_after_ensure_does_not_reset_or_send(self):
        self.e.ensure_page.side_effect = lambda *a, **kw: self.stop.set()
        self.assertEqual(list(self.stream()), [])
        self.e.reset_thread.assert_not_called()
        self.assertEqual(self.page.dispatches, 0)

    def test_cancel_after_reset_does_not_fill_or_send(self):
        self.e.reset_thread.side_effect = lambda **kw: self.stop.set()
        self.assertEqual(list(self.stream()), [])
        self.assertEqual(self.page.actions, [])

    def test_cancel_during_ready_wait_never_sends(self):
        self.page.state["ready"] = False
        self.page.after = lambda *a: self.stop.set()
        with self.assertRaises(engine.MuseGenerationError):
            list(self.stream())
        self.assertEqual(self.page.dispatches, 0)

    def test_cancel_after_fill_never_submits(self):
        self.page.after = lambda page, action, state: self.stop.set() if action == "fill" else None
        with self.assertRaises(engine.MuseGenerationError):
            self.e._send_chat_once("complete", 41, self.stop)
        self.assertEqual(self.page.dispatches, 0)

    def test_cancel_after_final_inspection_never_submits(self):
        def after(page, action, state):
            if action == "inspect" and page.value:
                self.stop.set()
        self.page.after = after
        with self.assertRaises(engine.MuseGenerationError):
            self.e._send_chat_once("complete", 41, self.stop)
        self.assertEqual(self.page.dispatches, 0)

    def test_cancel_after_dispatch_never_resubmits(self):
        self.page.after = lambda page, action, state: self.stop.set() if action == "submit" else None
        self.assertEqual(list(self.stream()), [])
        self.assertTrue(self.e.chat_submission_started)
        self.assertEqual(self.page.dispatches, 1)

    def test_no_local_evidence_fails_at_ten_seconds_not_ninety(self):
        self.page.local_at = None
        with self.assertRaisesRegex(engine.MuseGenerationError, "未观察到用户消息"):
            list(self.stream())
        self.assertGreaterEqual(self.clock.now, 10)
        self.assertLess(self.clock.now, 10.7)
        self.assertEqual(self.page.dispatches, 1)
        self.assertTrue(self.e.chat_submission_started)

    def test_local_message_at_nine_seconds_does_not_restart_first_token_budget(self):
        self.page.local_at = 9
        with self.assertRaisesRegex(engine.MuseGenerationError, "首字响应超时"):
            list(self.stream())
        self.assertGreaterEqual(self.clock.now, 90)
        self.assertLess(self.clock.now, 90.2)
        self.assertEqual(self.page.dispatches, 1)

    def test_first_text_at_fifty_five_seconds_returns_final_snapshot(self):
        self.page.poll = lambda t: (1, "final", False) if t >= 55 else (0, "", False)
        self.assertEqual(list(self.stream()), ["final"])
        self.assertGreaterEqual(self.clock.now, 56)
        self.assertLess(self.clock.now, 56.3)

    def test_short_total_budget_still_wins(self):
        with self.assertRaisesRegex(engine.MuseGenerationError, "首字响应超时"):
            list(self.stream(timeout=4))
        self.assertGreaterEqual(self.clock.now, 4)
        self.assertLess(self.clock.now, 4.2)

    def test_late_first_text_keeps_total_budget_at_three_hundred(self):
        self.page.poll = lambda t: (1, "partial", True) if t >= 89 else (0, "", False)
        with self.assertRaisesRegex(engine.MuseGenerationError, "等待助手回复超时"):
            list(self.stream())
        self.assertGreaterEqual(self.clock.now, 300)
        self.assertLess(self.clock.now, 300.2)
        self.assertTrue(self.e.chat_response_started)

    def test_final_buffer_absorbs_rewrites_empty_poll_and_stop_flicker(self):
        final = "  heading\n\n    return 1\nsame\nsame\n"
        def poll(t):
            if t < 0: return 0, "", False
            if t < 1: return 1, "draft\n- ", True
            if t < 1.4: return 1, "draft", False
            if t < 1.7: return 1, "", False
            if t < 2: return 1, final, True
            if t < 2.3: return 1, final, False
            if t < 2.6: return 1, final, True
            return 1, final, False
        self.page.poll = poll
        output = list(self.stream())
        self.assertEqual(output, [final])
        self.assertGreaterEqual(self.clock.now, 3.6)
        self.assertTrue(self.e.chat_response_started)
        self.assertEqual(self.page.dispatches, 1)

    def test_multiple_user_messages_fail_without_resending(self):
        def after(page, action, state):
            if action == "submit": page.state["users"] = 2
        self.page.after = after
        with self.assertRaisesRegex(engine.MuseGenerationError, "多个用户消息"):
            list(self.stream())
        self.assertEqual(self.page.dispatches, 1)

    def test_epoch_change_without_text_is_unknown(self):
        def after(page, action, state):
            if action == "submit": page.state["epoch"] = 42
        self.page.after = after
        with self.assertRaisesRegex(engine.MuseGenerationError, "页面已重新加载"):
            list(self.stream())
        self.assertTrue(self.e.chat_submission_started)
        self.assertEqual(self.page.dispatches, 1)

    def test_epoch_change_with_immediate_text_must_not_commit_foreign_answer(self):
        def poll(t):
            if t < 0: return 0, "", False
            self.page.state["epoch"] = 42
            return 1, "foreign page answer", False
        self.page.poll = poll
        self.require_rejection_without_output()

    def test_epoch_change_while_buffered_must_not_commit_foreign_snapshot(self):
        def poll(t):
            if t < 0: return 0, "", False
            if t < 1: return 1, "own answer", True
            self.page.state["epoch"] = 42
            return 1, "foreign page answer", False
        self.page.poll = poll
        self.require_rejection_without_output()

    def test_multiple_users_with_immediate_text_must_not_commit(self):
        def poll(t):
            if t < 0: return 0, "", False
            self.page.state["users"] = 2
            return 1, "ambiguous conversation", False
        self.page.poll = poll
        self.require_rejection_without_output()


class RetryGateTests(unittest.TestCase):
    """Execute the actual app safe_chat_stream, with no app imports or disk state."""
    def setUp(self):
        for target in ("socket.socket.connect", "socket.create_connection",
                       "requests.sessions.Session.request", "websocket.create_connection",
                       "subprocess.Popen"):
            self.enterContext(patch(target, side_effect=AssertionError("external I/O forbidden")))
        self.e = engine.MuseEngine.__new__(engine.MuseEngine)
        self.calls = []
        self.pool_calls = []
        self.releases = []
        self.e.start = Mock()
        self.e.reset_thread = Mock()
        self.e.close_current_page = Mock()
        self.lock = threading.Semaphore(1)
        def acquire(**kwargs):
            self.pool_calls.append(kwargs)
            return dict(id="b" if kwargs.get("rotate") else "a", cookies={})
        pool = SimpleNamespace(acquire=acquire, release=self.releases.append)
        tree = ast.parse(APP_SOURCE.read_text())
        function = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "safe_chat_stream")
        module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), function], type_ignores=[])
        env = dict(threading=threading, time=time, engine=self.e, ACCOUNT_POOL=pool,
                   GEN_LOCK=self.lock, MuseGenerationError=engine.MuseGenerationError,
                   MuseAuthError=engine.MuseAuthError, log=logging.getLogger("submission-worker-test"),
                   store=SimpleNamespace(mark=Mock()), _renew_and_persist=lambda *a, **kw: None,
                   _sync_cookies=lambda *a: None)
        exec(compile(ast.fix_missing_locations(module), str(APP_SOURCE), "exec"), env)
        self.safe_stream = env["safe_chat_stream"]

    def run_failure(self, *, submitted=False, observed=False, auth=False):
        error = engine.MuseAuthError("synthetic") if auth else engine.MuseGenerationError("synthetic")
        def stream(*args, account_id, **kwargs):
            self.calls.append(account_id)
            self.e.chat_submission_started = submitted
            self.e.chat_response_started = observed
            raise error
            yield "unreachable"
        self.e.chat_stream = stream
        with self.assertRaises(type(error)) as seen:
            list(self.safe_stream({}, "synthetic", None, 1, "a"))
        self.assertIs(seen.exception, error)
        self.assertTrue(self.lock.acquire(blocking=False))
        self.lock.release()
        self.assertEqual(sorted(self.releases), sorted(set(self.calls)))

    def test_unknown_submitted_without_text_never_rotates(self):
        self.run_failure(submitted=True)
        self.assertEqual(self.calls, ["a"])
        self.assertFalse(any(c.get("rotate") for c in self.pool_calls))

    def test_auth_error_after_submission_never_rotates(self):
        self.run_failure(submitted=True, auth=True)
        self.assertEqual(self.calls, ["a"])

    def test_observed_buffered_text_never_rotates(self):
        self.run_failure(observed=True)
        self.assertEqual(self.calls, ["a"])

    def test_proven_not_submitted_allows_only_one_alternate(self):
        self.run_failure()
        self.assertEqual(self.calls, ["a", "b"])
        self.assertEqual(sum(bool(c.get("rotate")) for c in self.pool_calls), 1)


if __name__ == "__main__":
    print("ENGINE_SHA256=" + hashlib.sha256(ENGINE_SOURCE.read_bytes()).hexdigest(), flush=True)
    print("APP_SHA256=" + hashlib.sha256(APP_SOURCE.read_bytes()).hexdigest(), flush=True)
    unittest.main(verbosity=2)
