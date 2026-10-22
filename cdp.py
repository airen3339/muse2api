"""极简 Chrome DevTools Protocol 客户端（仅依赖 websocket-client / requests）。"""
from __future__ import annotations

import json
import time

import threading

import requests
import websocket


class CDPError(RuntimeError):
    pass


class CDP:
    def __init__(self, ws_url: str, timeout: float = 90.0, max_size: int = 256 << 20):
        self.ws = websocket.create_connection(ws_url, timeout=timeout, max_size=max_size)
        self.timeout = timeout
        self._id = 0
        self._lock = threading.Lock()

    def send(self, method: str, params: dict | None = None, timeout: float | None = None):
        # 总预算自此刻（含获取 lock 的等待时间与发送时间）用单调时钟计算。
        budget = self.timeout if timeout is None else float(timeout)
        deadline = time.monotonic() + budget
        if not self._lock.acquire(timeout=max(deadline - time.monotonic(), 0.0)):
            raise TimeoutError(f"CDP {method} 等待锁超时")
        previous_timeout = None
        have_previous_timeout = False
        try:
            previous_timeout = self.ws.gettimeout()
            have_previous_timeout = True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"CDP {method} 超时（含等待锁）")
            self.ws.settimeout(remaining)
            self._id += 1
            mid = self._id
            # 只在发送一次；断链/超时后不重发有副作用的命令。
            try:
                self.ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
            except Exception as exc:  # noqa: BLE001  # 发送失败同样类型化，不带正文
                raise CDPError(f"CDP {method}: 发送失败 ({type(exc).__name__})") from exc
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(f"CDP {method} 超时")
                self.ws.settimeout(remaining)
                try:
                    raw = self.ws.recv()
                except websocket.WebSocketTimeoutException:
                    continue
                except Exception as exc:  # noqa: BLE001  # 断链/其它接收错误立即类型化，不携带回包内容
                    raise CDPError(f"CDP {method}: 连接中断 ({type(exc).__name__})") from exc
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"CDP {method} 超时")
                if raw in (None, "", b""):
                    raise CDPError(f"CDP {method}: 连接已关闭")
                try:
                    msg = json.loads(raw)
                except (TypeError, ValueError):
                    continue  # 非 JSON 帧：跳过等待下一帧，不抛出
                if not isinstance(msg, dict) or msg.get("id") != mid:
                    continue  # This synchronous client discards unrelated frames.
                if "error" in msg:
                    raise CDPError(f"CDP {method}: 协议错误（详情已隐藏）")
                return msg
        finally:
            try:
                if have_previous_timeout:
                    self.ws.settimeout(previous_timeout)
            except Exception:
                pass  # A closed socket must never prevent lock release.
            finally:
                self._lock.release()

    def js(self, expr: str, await_promise: bool = False, timeout: float | None = None):
        r = self.send("Runtime.evaluate",
                      {"expression": expr, "returnByValue": True,
                       "awaitPromise": await_promise}, timeout)
        result = r.get("result", {})
        if result.get("exceptionDetails"):
            # 只报类型化错误，不把脚本异常正文/调用栈暴露到日志。
            raise CDPError("CDP js: 远程脚本抛出异常（详情已隐藏）")
        res = result.get("result", {})
        if "value" in res:
            return res["value"]
        if res.get("type") == "undefined":
            return None
        return r.get("result")

    def pump(self, seconds: float, on_event=None, sock_timeout: float = 2.0):
        """在 seconds 秒内持续读取事件，交给回调处理。"""
        end = time.time() + seconds
        self.ws.settimeout(sock_timeout)
        while time.time() < end:
            try:
                ev = json.loads(self.ws.recv())
            except Exception:  # noqa: BLE001
                continue
            if on_event and "method" in ev:
                on_event(ev)

    def close(self):
        with self._lock:
            try:
                self.ws.close()
            except Exception:  # noqa: BLE001
                pass


def http_json(url: str, timeout: float = 5.0):
    return requests.get(url, timeout=timeout).json()
