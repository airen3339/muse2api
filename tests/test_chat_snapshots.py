"""Offline regression: chat_stream yields the stable final DOM snapshot once.

python -B tests/test_chat_snapshots.py [-v] [test names]

Design contract under test:
  - the first-token budget (default 90s) applies to the first page text, not
    to the yield moment; the single yield happens after the Stop button is
    gone and the same final DOM snapshot has stayed stable for about one
    second (no strict float boundary: timing assertions are coarse);
  - no body text may be yielded while the generation is still running;
  - the completion window is contiguous: Stop-button-free plus the same
    nonempty final snapshot for about one second (coarse timing bounds,
    no strict float edge); an edit arriving after a completion window has
    already held longer than one second is by contract not re-captured,
    so scenarios place edits inside the live window;
  - the output is exactly one yield, byte-for-byte the final snapshot:
    strict append, no duplicated or garbled deltas, internal newlines
    rearrangement, temporary shortenings, stop flicker and blank polls are
    absorbed; legitimate duplicate lines and code indentation survive;
  - a changing assistant-bubble count during the wait must raise an explicit
    error instead of mistaking the last bubble for the full reply;
  - a nonempty baseline sample or a poll error before sending must fail
    explicitly with no prompt sent (for_chat=False forces a fresh thread
    and the blank baseline is mandatory);
  - non-stream chat equals the final snapshot.

MUSE_ENGINE_SOURCE overrides the engine.py under test for comparisons with a
pre-fix checkout. Incremental-poll designs are expected to fail these cases.
No app import, browser, network, real cookies, or persistent account state.

Mock contract: engine imports are real and complete; external I/O,
subprocesses, browser objects and accounts are mocked. Page DOM state is
served through the _poll_chat sample interface (cnt, txt, stop) and,
incidentally, through page.js returning the same JSON payload.
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
    "engine_under_test",
    os.environ.get("MUSE_ENGINE_SOURCE", str(ROOT / "engine.py")))
engine = importlib.util.module_from_spec(spec)
spec.loader.exec_module(engine)


def lines(*parts):
    return chr(10).join(parts)


def phases(rules):
    """Piecewise timeline: rules = [(until_sec, sample), ..., (None, tail)]."""

    def sample(t):
        for until, value in rules:
            if until is None or t < until:
                return value
        return rules[-1][1]

    return sample


class Clock:
    """Virtual clock: time.sleep advances the clock, suites finish fast."""

    def __init__(self):
        self.now = 0.0

    def time(self):
        return self.now

    def monotonic(self):
        return self.now

    def perf_counter(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class OfflineTest(unittest.TestCase):
    def setUp(self):
        home = self.enterContext(tempfile.TemporaryDirectory(prefix="muse-snap-test-"))
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


class SnapshotTests(OfflineTest):
    FINAL = "完整结论：最终DOM快照就是对的。"
    REORDER_M1 = lines("要点：", "第一项", "- ")
    REORDER_M2 = lines("要点：", "第一项")
    REORDER_FINAL = lines("要点：", "第一项", "第二项")
    LAYOUT_MID = lines("第一行", "第二行")
    LAYOUT_FINAL = lines("第一行", "", "第二行", "补充行")
    SHORT_LONG1 = "第一句。第二句。草拟续写中"
    SHORT_MID = "第一句。"
    SHORT_FINAL = "第一句。第二句。最终补全。"
    EDIT_MID = "草稿内容"
    EDIT_FINAL = "草稿内容。已补充落地结论"
    DUPLICATED = lines("回放确认：已修复。", "回放确认：已修复。", "结尾行")
    CODE_FINAL = lines("```python", "    def f():", "", "        return 1", "```")

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
        self._send = Mock(return_value="dispatched")
        self.e._send_chat_once = self._send
        self.e.page = Mock()
        self.stop = threading.Event()
        self.yield_times = []
        self.script_fn = phases([(None, (0, "", False))])

    def current_sample(self):
        if self.clock.now == 0:
            return (0, "", False)
        return self.script_fn(self.clock.now)

    def stream(self, timeout=300, first_token=90, until_error=False):
        """Run chat_stream against the scripted DOM timeline.

        until_error=True consumes the generator collecting chunks while
        expecting the engine to raise; both chunks and the error return.
        """
        self.e.cfg = SimpleNamespace(chat_timeout=timeout,
                                     chat_first_token_timeout=first_token,
                                     image_timeout=240, video_timeout=600)
        self.clock.now = 0.0
        self.yield_times = []

        def poll():
            return self.current_sample()

        self.e._poll_chat = poll
        self.e.page.js.side_effect = lambda *a, **kw: json.dumps({
            "cnt": self.current_sample()[0],
            "txt": self.current_sample()[1],
            "stop": self.current_sample()[2]})

        chunks = []
        error = None

        def consume():
            for chunk in self.e.chat_stream({}, "synthetic prompt",
                                            timeout=timeout,
                                            stop_event=self.stop):
                self.yield_times.append(self.clock.now)
                chunks.append(chunk)

        if until_error:
            try:
                consume()
            except Exception as exc:  # noqa: BLE001
                error = exc
            return chunks, error
        consume()
        return chunks, error

    def assert_single_final_yield(self, chunks, error, final_text,
                                  final_after, latest):
        self.assertIsNone(error, str(error))
        self.assertEqual(chunks, [final_text])
        self.assertEqual(len(self.yield_times), 1)
        # Coarse stability window (~1s), deliberately no tight float bounds.
        self.assertGreaterEqual(self.yield_times[0], final_after)
        self.assertLess(self.yield_times[0], latest)

    def test_no_body_output_until_stable_final_snapshot(self):
        self.script_fn = phases([(1.0, (1, "", True)),
                                 (3.0, (1, self.FINAL, True)),
                                 (None, (1, self.FINAL, False))])
        chunks, error = self.stream()
        self.assert_single_final_yield(chunks, error, self.FINAL, 3.5, 7.0)

    def test_nonempty_baseline_blocks_send(self):
        self.e.cfg = SimpleNamespace(chat_timeout=300,
                                     chat_first_token_timeout=90,
                                     image_timeout=240, video_timeout=600)
        self.clock.now = 0.0
        self.e._poll_chat = lambda: (1, "遗留旧气泡", False)
        chunks = []
        error = None
        try:
            for chunk in self.e.chat_stream({}, "synthetic prompt", timeout=300,
                                            stop_event=self.stop):
                chunks.append(chunk)
        except Exception as exc:  # noqa: BLE001
            error = exc
        self.assertIsNotNone(error)
        self.assertIn("空白对话", str(error))
        self.assertEqual(chunks, [])
        self._send.assert_not_called()
        # 立刻失败：不进入任何等待循环。
        self.assertLess(self.clock.now, 0.5)

    def test_poll_error_baseline_blocks_send(self):
        self.e.cfg = SimpleNamespace(chat_timeout=300,
                                     chat_first_token_timeout=90,
                                     image_timeout=240, video_timeout=600)
        self.clock.now = 0.0
        self.e.page = Mock()
        self.e.page.js.side_effect = RuntimeError("cdp poll broken")
        chunks = []
        error = None
        try:
            for chunk in self.e.chat_stream({}, "synthetic prompt", timeout=300,
                                            stop_event=self.stop):
                chunks.append(chunk)
        except Exception as exc:  # noqa: BLE001
            error = exc
        # 真实 _poll_chat() 吞掉轮询故障并以 chat_poll_errors 上报：
        # 空白基线守卫必须在发送提示词前显式失败。
        self.assertIsNotNone(error)
        self.assertIn("空白对话", str(error))
        self.assertEqual(chunks, [])
        self._send.assert_not_called()
        self.assertLess(self.clock.now, 0.5)

    def test_strict_append_partial_prefix_never_yields_mid_body(self):
        self.script_fn = phases([(1.0, (1, "", True)),
                                 (2.0, (1, "第一句。", True)),
                                 (4.0, (1, self.SHORT_FINAL, True)),
                                 (None, (1, self.SHORT_FINAL, False))])
        chunks, error = self.stream()
        self.assert_single_final_yield(chunks, error, self.SHORT_FINAL, 4.5, 8.0)

    def test_list_dash_reorder_never_emits_garbled_delta(self):
        # 真实临时横杠穿越：前缀+第一项+\n- → 前缀+第一项+\n(横杠消失)
        # → 前缀+第一项+\n第二项。
        self.script_fn = phases([(1.0, (1, "", True)),
                                 (1.6, (1, self.REORDER_M1, True)),
                                 (2.2, (1, self.REORDER_M2, True)),
                                 (4.0, (1, self.REORDER_FINAL, True)),
                                 (None, (1, self.REORDER_FINAL, False))])
        chunks, error = self.stream()
        self.assert_single_final_yield(chunks, error, self.REORDER_FINAL, 4.5, 8.0)

    def test_internal_newline_relayout_not_broken_into_deltas(self):
        self.script_fn = phases([(1.0, (1, "", True)),
                                 (2.0, (1, self.LAYOUT_MID, True)),
                                 (4.0, (1, self.LAYOUT_FINAL, True)),
                                 (None, (1, self.LAYOUT_FINAL, False))])
        chunks, error = self.stream()
        self.assert_single_final_yield(chunks, error, self.LAYOUT_FINAL, 4.5, 8.0)

    def test_temporary_shortening_then_restore_waits_for_final(self):
        # 长 → 短 → 长：页面短暂重排为截断文本，随后恢复并补全。
        self.script_fn = phases([(1.0, (1, "", True)),
                                 (1.5, (1, self.SHORT_LONG1, True)),
                                 (2.2, (1, self.SHORT_MID, True)),
                                 (4.0, (1, self.SHORT_FINAL, True)),
                                 (None, (1, self.SHORT_FINAL, False))])
        chunks, error = self.stream()
        self.assert_single_final_yield(chunks, error, self.SHORT_FINAL, 4.5, 8.0)

    def test_substantive_edit_after_stop_vanish_is_captured(self):
        # 修改发生在第一个停止消失窗口内（0.6s），必须被最终快照捕获。
        self.script_fn = phases([(0.6, (1, self.EDIT_MID, False)),
                                 (None, (1, self.EDIT_FINAL, False))])
        chunks, error = self.stream()
        self.assert_single_final_yield(chunks, error, self.EDIT_FINAL, 1.6, 5.0)

    def test_identical_snapshot_completes_once_without_change(self):
        self.script_fn = phases([(None, (1, self.FINAL, False))])
        chunks, error = self.stream()
        self.assert_single_final_yield(chunks, error, self.FINAL, 1.0, 4.5)

    def test_stop_flicker_after_finish_does_not_break_completion(self):
        # Stop 短闪必须 < 1s：False 2-2.4 不构成完成窗口，4s 后方可提交。
        self.script_fn = phases([(1.0, (1, "", True)),
                                 (2.0, (1, self.FINAL, True)),
                                 (2.4, (1, self.FINAL, False)),
                                 (4.0, (1, self.FINAL, True)),
                                 (None, (1, self.FINAL, False))])
        chunks, error = self.stream()
        self.assert_single_final_yield(chunks, error, self.FINAL, 4.9, 9.0)

    def test_blank_poll_during_generation_resets_stability(self):
        def timeline(t):
            if t < 1.0:
                return (1, "", True)
            if t < 1.15:
                return (1, self.FINAL, False)
            # 空采占：对应轮询暂失（透明中继无内容），稳定计时必须重置。
            if t < 1.40:
                return (0, "", False)
            return (1, self.FINAL, False)

        self.script_fn = timeline
        chunks, error = self.stream()
        # Blank polls reset the ~1s stability window: completion is at least
        # one stability window after the last blank sample, and never before
        # the blankpoll-free phase; coarse bounds only.
        self.assert_single_final_yield(chunks, error, self.FINAL, 2.0, 8.0)

    def test_legitimate_duplicated_lines_are_preserved(self):
        self.script_fn = phases([(1.0, (1, "", True)),
                                 (3.0, (1, self.DUPLICATED, True)),
                                 (None, (1, self.DUPLICATED, False))])
        chunks, error = self.stream()
        self.assert_single_final_yield(chunks, error, self.DUPLICATED, 3.5, 7.0)
        self.assertEqual(chunks[0].count("回放确认：已修复。"), 2)

    def test_code_indentation_blank_lines_only_in_single_yield(self):
        self.script_fn = phases([(1.0, (1, "", True)),
                                 (3.0, (1, self.CODE_FINAL, True)),
                                 (None, (1, self.CODE_FINAL, False))])
        chunks, error = self.stream()
        self.assert_single_final_yield(chunks, error, self.CODE_FINAL, 3.5, 7.0)
        self.assertIn("    def f():", chunks[0])

    def test_cancel_during_generation_returns_no_content(self):
        def timeline(t):
            if t >= 1.5:
                self.stop.set()
            return (1, "", True)

        self.script_fn = timeline
        chunks, error = self.stream()
        self.assertIsNone(error, str(error))
        self.assertEqual(chunks, [])
        self.assertEqual(self.yield_times, [])
        self.assertLess(self.clock.now, 3.5)

    def test_bubble_count_change_during_wait_fails_explicitly(self):
        self.script_fn = phases([(1.0, (1, self.FINAL, True)),
                                 (3.0, (2, self.FINAL, True)),
                                 (None, (2, self.FINAL, False))])
        chunks, error = self.stream(timeout=300, until_error=True)
        # Explicit error, not a mistaken single-bubble reply, no body out.
        self.assertIsNotNone(error)
        self.assertEqual(chunks, [])
        self.assertEqual(self.yield_times, [])
        self.assertLess(self.clock.now, 200.0)
        self.assertNotIn("等待助手回复超时", str(error))

    def test_web_text_present_but_total_timeout_fails_explicitly(self):
        self.script_fn = phases([(1.0, (1, self.FINAL, True)),
                                 (None, (1, self.FINAL, True))])
        chunks, error = self.stream(timeout=30, until_error=True)
        self.assertIsNotNone(error)
        self.assertIn("等待助手回复超时", str(error))
        # Page text existed but the snapshot never finished: no body text.
        self.assertEqual(chunks, [])
        self.assertEqual(self.yield_times, [])
        self.assertGreaterEqual(self.clock.now, 29.5)

    def test_first_page_text_at_60s_completes_after_single_yield(self):
        late = self.FINAL + "（迟到网页文字）"
        self.script_fn = phases([(60.0, (1, "", True)),
                                 (100.0, (1, late, True)),
                                 (None, (1, late, False))])
        chunks, error = self.stream(timeout=300, first_token=90)
        self.assert_single_final_yield(chunks, error, late, 100.5, 105.0)

    def test_nonstream_chat_equals_final_snapshot(self):
        self.script_fn = phases([(1.0, (1, "", True)),
                                 (3.0, (1, self.FINAL, True)),
                                 (None, (1, self.FINAL, False))])
        chunks, error = self.stream()
        self.assertEqual(chunks, [self.FINAL])
        # 非流式接口返回最终快照原文。
        self.clock.now = 0.0
        out = self.e.chat({}, "synthetic prompt", timeout=300)
        self.assertEqual(out, self.FINAL)

    def test_final_answer_can_be_shorter_than_draft(self):
        self.script_fn = phases([(2.0, (1, "初稿很长并包含应删除的内容", True)),
                                 (None, (1, "最终短句", False))])
        chunks, error = self.stream()
        self.assert_single_final_yield(chunks, error, "最终短句", 2.9, 5.0)

    def test_actual_poll_preserves_leading_indent_and_trailing_newline(self):
        text = "    return 42" + chr(10)
        self.e.page.js.return_value = json.dumps({'cnt':1,'txt':text,'stop':False})
        self.assertEqual(self.e._poll_chat(), (1,text,False))

    def test_late_blocking_poll_cannot_commit_past_total_deadline(self):
        self.e.cfg = SimpleNamespace(chat_timeout=3, chat_first_token_timeout=2)
        samples = iter([(0, "", False), (1, "未完成", True)])
        def poll():
            try:
                return next(samples)
            except StopIteration:
                self.clock.now += 4
                return (1, "最终", False)
        self.e._poll_chat = poll
        output=[]
        with self.assertRaisesRegex(engine.MuseGenerationError, "等待助手回复超时"):
            for chunk in self.e.chat_stream({}, "synthetic",timeout=3):
                output.append(chunk)
        self.assertEqual(output, [])


if __name__ == "__main__":
    unittest.main()
