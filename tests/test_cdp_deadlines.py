"""cdp.py 的超时/断链/调度行为验收（假 socket + 虚拟时钟，不发网络请求）。"""
import json
import math
import sys
import threading
import time
import traceback

from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cdp  # noqa: E402
import websocket  # noqa: E402


class FakeTime:
    def __init__(self, start=1000.0):
        self.t = start
        self.start = start

    def time(self):
        return self.t

    def monotonic(self):
        return self.t

    def advance(self, delta):
        self.t += delta
        return self.t


class FakeWS:
    step = 0.5

    def __init__(self, fake_time):
        self.fake_time = fake_time
        self.schedule = []
        self.settimeout_calls = []
        self.sent = []
        self.closed = False
        self.timeout = 90.0

    def at(self, when, value):
        self.schedule.append((when, value))
        self.schedule.sort(key=lambda item: math.inf if item[0] is None else item[0])

    def settimeout(self, value):
        self.settimeout_calls.append(value)
        self.timeout = value

    def gettimeout(self):
        return self.timeout

    def send(self, data):
        if isinstance(data, bytes):
            data = data.decode()
        self.sent.append(json.loads(data))

    def recv(self):
        if self.fake_time is not None:
            self.fake_time.advance(self.step)
        now = self.fake_time.monotonic() if self.fake_time is not None else 0.0
        for index, (when, value) in enumerate(self.schedule):
            due = math.nan if when is None or self.fake_time is None else self.fake_time.start + when
            if when is None or (self.fake_time is not None and due <= now):
                del self.schedule[index]
                if isinstance(value, Exception):
                    raise value
                return value if isinstance(value, (str, bytes)) else json.dumps(value, ensure_ascii=False)
        raise websocket.WebSocketTimeoutException()

    def close(self):
        self.closed = True


def make_client(items, timeout=90.0, fake_time=None):
    """返回 (client, socket, restore)；restore() 还原 cdp.time 与 create_connection 补丁。"""
    socket = FakeWS(fake_time)
    for when, value in items:
        socket.at(when, value)
    original = cdp.websocket.create_connection
    original_time = cdp.time
    cdp.websocket.create_connection = lambda *args, **kwargs: socket
    if fake_time is not None:
        cdp.time = SimpleNamespace(time=fake_time.time, monotonic=fake_time.monotonic)
    client = cdp.CDP("ws://example.invalid/devtools", timeout=timeout)

    def restore():
        cdp.websocket.create_connection = original
        cdp.time = original_time

    return client, socket, restore


def expect_error(func, exc_types, message):
    try:
        func()
    except exc_types:
        return True
    raise AssertionError(message)


def test_socket_deadline_no_response():
    client, socket, restore = make_client([], timeout=90.0, fake_time=FakeTime())
    try:
        expect_error(lambda: client.send("Page.navigate", {"url": "https://x.invalid"}, timeout=1.0),
                     TimeoutError, "无回包时应报 TimeoutError")
    finally:
        restore()
    assert len(socket.sent) == 1, "同一命令不得重发"
    assert socket.settimeout_calls[-1] == 90.0, "结束后必须恢复连接级超时"


def test_late_response_after_deadline():
    client, socket, restore = make_client([
        (1.5, {"id": 1, "result": {}}),
    ], timeout=90.0, fake_time=FakeTime())
    try:
        expect_error(lambda: client.send("Runtime.evaluate", {"expression": "1"}, timeout=1.0),
                     TimeoutError, "晚到回包必须报 TimeoutError")
    finally:
        restore()
    assert len(socket.sent) == 1


def test_events_before_response():
    client, socket, restore = make_client([
        (0.2, {"method": "Page.frameStartedLoading", "params": {}}),
        (0.4, {"method": "Runtime.bindingCalled", "params": {}}),
        (1.5, {"id": 1, "result": {"result": {"type": "string", "value": "ok"}}}),
    ], timeout=90.0, fake_time=FakeTime())
    try:
        result = client.send("Runtime.evaluate", {"expression": "1"}, timeout=3.0)
    finally:
        restore()
    assert result["result"]["result"]["value"] == "ok"


def test_broken_link_immediate_cdp_error():
    client, socket, restore = make_client([
        (None, OSError("connection dropped")),
    ], timeout=90.0, fake_time=FakeTime())
    try:
        expect_error(lambda: client.send("Runtime.evaluate", {"expression": "1+1"}, timeout=5.0),
                     cdp.CDPError, "断链应类型化为 CDPError")
    finally:
        restore()


def test_closed_exception_raises_cdp_error():
    client, socket, restore = make_client([
        (0.2, {"method": "Page.loadEventFired", "params": {}}),
        (None, websocket.WebSocketConnectionClosedException()),
    ], timeout=90.0, fake_time=FakeTime())
    try:
        expect_error(lambda: client.send("Page.navigate", {"url": "https://x.invalid"}, timeout=3.0),
                     cdp.CDPError, "连接关闭必须报 CDPError")
    finally:
        restore()


def test_broken_link_error_has_no_payload():
    client, socket, restore = make_client([
        (0.2, {"id": 99, "result": {"secretPayload": "leet"}}),
        (None, OSError("connection dropped")),
    ], timeout=90.0, fake_time=FakeTime())
    try:
        try:
            client.send("Runtime.evaluate", {}, timeout=3.0)
        except cdp.CDPError as exc:
            assert "leet" not in str(exc), "CDPError 不得携带回包内容"
        else:
            raise AssertionError("断链必须报 CDPError")
    finally:
        restore()


def test_exception_details_raises_without_body():
    client, socket, restore = make_client([
        (0.2, {"id": 1, "result": {"result": {"type": "object"},
                                   "exceptionDetails": {"exception": {"description": "let secret = 42"}}}}),
    ], timeout=90.0, fake_time=FakeTime())
    try:
        try:
            client.js("let secret = 42; secret", timeout=3.0)
        except cdp.CDPError as exc:
            assert "secret" not in str(exc), "异常正文不得暴露给调用方"
        else:
            raise AssertionError("exceptionDetails 存在时 js() 必须报错")
    finally:
        restore()


def test_js_undefined_returns_none():
    client, socket, restore = make_client([
        (0.2, {"id": 1, "result": {"result": {"type": "undefined"}}}),
    ], timeout=90.0, fake_time=FakeTime())
    try:
        value = client.js("undefined", timeout=3.0)
    finally:
        restore()
    assert value is None


def test_socket_timeout_restored_after_success():
    client, socket, restore = make_client([
        (0.2, {"id": 1, "result": {}}),
    ], timeout=90.0, fake_time=FakeTime())
    try:
        client.send("Page.navigate", {"url": "https://x.invalid"}, timeout=5.0)
    finally:
        restore()
    assert socket.settimeout_calls[-1] == 90.0, "成功路径也要恢复连接级超时"


def test_socket_timeout_restored_after_error():
    client, socket, restore = make_client([
        (None, OSError("broken pipe")),
    ], timeout=90.0, fake_time=FakeTime())
    try:
        expect_error(lambda: client.send("Runtime.evaluate", {}, timeout=2.0),
                     cdp.CDPError, "断链应报 CDPError")
    finally:
        restore()
    assert socket.settimeout_calls[-1] == 90.0, "错误路径也要恢复连接级超时"


def test_invalid_json_frame_is_skipped():
    client, socket, restore = make_client([
        (0.2, "not-json{{{"),
        (0.4, {"id": 1, "result": {"result": {"type": "object", "value": {"a": 1}}}}),
    ], timeout=90.0, fake_time=FakeTime())
    try:
        result = client.send("Runtime.evaluate", {"expression": "1"}, timeout=3.0)
    finally:
        restore()
    assert result["result"]["result"]["value"] == {"a": 1}


def test_lock_wait_inside_budget_real_clock():
    client, socket, restore = make_client([], timeout=90.0, fake_time=None)
    try:
        assert client._lock.acquire()
        try:
            holder = threading.Thread(target=lambda: (time.sleep(0.3), client._lock.release()))
            holder.daemon = True
            holder.start()
            started = time.monotonic()
            expect_error(lambda: client.send("Page.navigate", {"url": "https://x.invalid"}, timeout=0.5),
                         (TimeoutError, cdp.CDPError), "锁等待+无回包应在预算内报错")
            elapsed = time.monotonic() - started
            assert elapsed < 3.0, f"锁等待应计入总预算，实际耗时 {elapsed:.2f}s"
            assert socket.settimeout_calls[-1] == 90.0, "结束后必须恢复连接级超时"
        finally:
            restore()
    finally:
        restore()


def test_receive_crosses_deadline_with_matching_reply():
    clock = FakeTime()
    client, socket, restore = make_client([(None, {'id': 1, 'result': {}})], fake_time=clock)
    socket.step = 90.0
    try:
        expect_error(lambda: client.send('Runtime.evaluate', {}, timeout=1), TimeoutError, 'late matching reply accepted')
        assert len(socket.sent) == 1
    finally:
        restore()


def test_restore_actual_previous_socket_timeout():
    client, socket, restore = make_client([(None, {'id': 1, 'result': {}})], fake_time=FakeTime())
    socket.timeout = 2.25  # A previous pump may have changed it.
    try:
        client.send('Runtime.evaluate', {}, timeout=1)
        assert socket.timeout == 2.25
    finally:
        restore()


def test_restore_failure_never_leaks_lock():
    client, socket, restore = make_client([(None, OSError('closed'))], fake_time=FakeTime())
    original = socket.settimeout
    def setting(value):
        if value == 90.0:
            raise OSError('closed socket')
        original(value)
    socket.settimeout = setting
    try:
        expect_error(lambda: client.send('Runtime.evaluate', {}, timeout=1), cdp.CDPError, 'closed should fail')
        assert not client._lock.locked(), 'restoration failure leaked lock'
    finally:
        restore()


def test_empty_recv_is_closed_not_timeout():
    client, socket, restore = make_client([(None, '')], fake_time=FakeTime())
    try:
        expect_error(lambda: client.send('Runtime.evaluate', {}, timeout=1), cdp.CDPError, 'EOF was swallowed')
    finally:
        restore()


def test_protocol_error_does_not_include_payload():
    client, socket, restore = make_client([(None, {'id': 1, 'error': {'code': -32000, 'message': 'sensitive-value'}})], fake_time=FakeTime())
    try:
        try:
            client.send('Runtime.evaluate', {}, timeout=1)
        except cdp.CDPError as exc:
            assert 'sensitive-value' not in str(exc)
        else:
            raise AssertionError('protocol error accepted')
    finally:
        restore()


def test_lock_budget_expires_before_any_send():
    client, socket, restore = make_client([], fake_time=None)
    client._lock.acquire()
    try:
        started = time.monotonic()
        expect_error(lambda: client.send('Runtime.evaluate', {}, timeout=0.03), TimeoutError, 'lock timeout ignored')
        assert time.monotonic() - started < 0.15
        assert not socket.sent and not socket.settimeout_calls
    finally:
        client._lock.release()
        restore()


def run_all():
    module = sys.modules[__name__]
    failures = []
    for name in sorted(dir(module)):
        if not name.startswith("test_") or not callable(getattr(module, name)):
            continue
        try:
            getattr(module, name)()
            print(f"PASS {name}")
        except Exception:
            failures.append(name)
            print(f"FAIL {name}")
            traceback.print_exc()
    return failures


if __name__ == "__main__":
    raise SystemExit(1 if run_all() else 0)
