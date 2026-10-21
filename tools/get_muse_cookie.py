#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""muse2api Cookie Helper — import your muse.ai login cookies into the account pool in one step.

Why is this script needed?
    All 4 core muse.ai cookies (hatch_sess / hatch_gw / hatch_vml /
    hatch_native_auth_device) are marked httpOnly, so document.cookie in the page
    cannot read them. They can only be read at the browser level (Chrome DevTools Protocol).
    That's why a bookmarklet or a one-liner in the console won't work; a script has to drive the browser.

How does it work?
    1. Launches a Chrome window with a separate, temporary profile (your everyday browser is untouched)
    2. You log in to muse.ai in that window
    3. The script polls the cookies and uploads them to muse2api as soon as they appear
    4. The window closes, done

Usage:
    python get_muse_cookie.py --base http://your-server-ip:18610 --key m2a_xxx
    python get_muse_cookie.py --base http://your-server-ip:18610 --key m2a_xxx --label acc-01

You can also set environment variables to avoid typing them every time:
    set MUSE2API_BASE=http://your-server-ip:18610     (Windows)
    set MUSE2API_KEY=m2a_xxx
    export MUSE2API_BASE=...                          (macOS / Linux)
    export MUSE2API_KEY=...

Depends only on the Python standard library (Python 3.8+); no pip install needed.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

SITE = "https://muse.ai/"
CDP_PORT = 9333
ESSENTIAL = ("hatch_sess", "hatch_gw", "hatch_vml", "hatch_native_auth_device")
DOMAIN_HINT = "muse.ai"


# ---------------------------------------------------------------- Output
def say(msg=""):
    try:
        print(msg, flush=True)
    except UnicodeEncodeError:                     # legacy Windows console
        print(msg.encode("utf-8", "replace").decode("utf-8", "replace"), flush=True)


def init_console():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")   # Python 3.7+
        except Exception:                          # noqa: BLE001
            pass


# ---------------------------------------------------------------- Minimal WebSocket
class WS:
    """Minimal WebSocket client just for CDP (RFC6455, text frames)."""

    def __init__(self, url: str, timeout: float = 15.0):
        assert url.startswith("ws://"), url
        rest = url[5:]
        hostport, _, path = rest.partition("/")
        path = "/" + path
        host, _, port = hostport.partition(":")
        port = int(port or 80)

        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.sock.settimeout(timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        req = (f"GET {path} HTTP/1.1\r\n"
               f"Host: {host}:{port}\r\n"
               "Upgrade: websocket\r\n"
               "Connection: Upgrade\r\n"
               f"Sec-WebSocket-Key: {key}\r\n"
               "Sec-WebSocket-Version: 13\r\n\r\n")
        self.sock.sendall(req.encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("WebSocket handshake was disconnected")
            buf += chunk
        status = buf.split(b"\r\n", 1)[0]
        if b" 101" not in status:
            raise ConnectionError(f"WebSocket handshake failed: {status!r}")
        self._id = 0

    # --- Send ---
    def _frame(self, opcode: int, payload: bytes):
        head = bytearray([0x80 | opcode])
        n = len(payload)
        if n < 126:
            head.append(0x80 | n)
        elif n < 65536:
            head.append(0x80 | 126)
            head += struct.pack(">H", n)
        else:
            head.append(0x80 | 127)
            head += struct.pack(">Q", n)
        mask = os.urandom(4)
        head += mask
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(bytes(head) + masked)

    def send_text(self, text: str):
        self._frame(0x1, text.encode("utf-8"))

    # --- Receive ---
    def _read(self, n: int) -> bytes:
        out = b""
        while len(out) < n:
            chunk = self.sock.recv(n - len(out))
            if not chunk:
                raise ConnectionError("Connection closed")
            out += chunk
        return out

    def recv_text(self) -> str:
        while True:
            h = self._read(2)
            opcode = h[0] & 0x0F
            masked = h[1] & 0x80
            ln = h[1] & 0x7F
            if ln == 126:
                ln = struct.unpack(">H", self._read(2))[0]
            elif ln == 127:
                ln = struct.unpack(">Q", self._read(8))[0]
            mk = self._read(4) if masked else None
            data = self._read(ln) if ln else b""
            if mk:
                data = bytes(b ^ mk[i % 4] for i, b in enumerate(data))
            if opcode == 0x1:
                return data.decode("utf-8", "replace")
            if opcode == 0x9:                       # ping -> pong
                self._frame(0xA, data)
            elif opcode == 0x8:
                raise ConnectionError("Server closed the connection")
            # Anything else (pong / binary / fragments) is ignored

    def call(self, method: str, params: dict | None = None,
             timeout: float = 20.0) -> dict:
        self._id += 1
        mid = self._id
        self.send_text(json.dumps({"id": mid, "method": method,
                                   "params": params or {}}))
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                msg = json.loads(self.recv_text())
            except socket.timeout:
                continue
            except ConnectionError:
                raise
            if msg.get("id") == mid:
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error']}")
                return msg.get("result", {})
        raise TimeoutError(f"CDP {method} timed out")

    def close(self):
        try:
            self.sock.close()
        except Exception:                              # noqa: BLE001
            pass


# ---------------------------------------------------------------- Find the browser
CHROME_EXES = ("chrome.exe", "msedge.exe", "brave.exe", "chromium.exe")


def _win_registry_paths() -> list[str]:
    """Find browsers via the registry's App Paths.

    We can't rely on the %ProgramFiles% environment variable — it doesn't exist in some
    shells / stripped-down environments (verified locally: both ProgramFiles and PROGRAMFILES were None).
    """
    out: list[str] = []
    try:
        import winreg
    except ImportError:
        return out
    sub = (r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths")
    for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        for exe in CHROME_EXES:
            for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
                try:
                    with winreg.OpenKey(root, sub + "\\" + exe, 0,
                                        winreg.KEY_READ | view) as k:
                        val = winreg.QueryValueEx(k, "")[0]
                        if val:
                            out.append(os.path.expandvars(val))
                except OSError:
                    continue
    return out


def _win_common_paths() -> list[str]:
    roots = [os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)"),
             os.environ.get("LOCALAPPDATA"), r"C:\Program Files",
             r"C:\Program Files (x86)"]
    rel = [
        r"Google\Chrome\Application\chrome.exe",
        r"Microsoft\Edge\Application\msedge.exe",
        r"BraveSoftware\Brave-Browser\Application\brave.exe",
    ]
    return [os.path.join(r, p) for r in roots if r for p in rel]


def find_chrome() -> str | None:
    if sys.platform == "win32":
        cands = _win_registry_paths() + _win_common_paths()
    elif sys.platform == "darwin":
        cands = [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
            "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
        ]
    else:
        cands = ["google-chrome", "google-chrome-stable", "chromium",
                 "chromium-browser", "microsoft-edge", "brave-browser"]
    seen = set()
    for c in cands:
        if not c or c in seen:
            continue
        seen.add(c)
        if os.path.isfile(c):
            return c
        if not os.path.isabs(c):
            w = shutil.which(c)
            if w:
                return w
    return None


def wait_cdp(port: int, timeout: float = 30.0) -> str:
    """Wait for the DevTools port to come up and return the browser-level WebSocket URL."""
    deadline = time.time() + timeout
    url = f"http://127.0.0.1:{port}/json/version"
    last = None
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as r:
                return json.load(r)["webSocketDebuggerUrl"]
        except Exception as exc:                       # noqa: BLE001
            last = exc
            time.sleep(0.5)
    raise RuntimeError(f"Browser debugging port did not come up ({port}): {last}")


# ---------------------------------------------------------------- Read cookies
def read_cookies(ws: WS) -> dict[str, dict]:
    """Fetch all cookies via browser-level Storage.getCookies, then filter for muse.ai.

    This is the key point: CDP can read httpOnly cookies, page JS cannot.
    """
    res = ws.call("Storage.getCookies", {}, timeout=20)
    out: dict[str, dict] = {}
    for c in res.get("cookies", []):
        domain = (c.get("domain") or "").lstrip(".")
        if DOMAIN_HINT not in domain:
            continue
        name = c.get("name")
        if not name:
            continue
        try:
            exp = int(float(c.get("expires", -1)))
        except (TypeError, ValueError):
            exp = -1
        out[name] = {"value": c.get("value", ""), "expires": exp,
                     "httpOnly": bool(c.get("httpOnly"))}
    return out


# ---------------------------------------------------------------- Upload
def upload(base: str, key: str, label: str, cookies: dict[str, dict]) -> dict:
    payload = {
        "label": label,
        "cookies": {k: v["value"] for k, v in cookies.items()},
        "expires": {k: v["expires"] for k, v in cookies.items() if v["expires"] > 0},
    }
    req = urllib.request.Request(
        base.rstrip("/") + "/admin/accounts",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {key}",
                 "Content-Type": "application/json"},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:300]
        raise RuntimeError(f"Upload failed HTTP {e.code}: {detail}") from None
    except urllib.error.URLError as e:
        raise RuntimeError(f"Cannot connect to {base}: {e.reason}") from None


def fmt_ts(ts: int) -> str:
    if ts <= 0:
        return "session"
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


# ---------------------------------------------------------------- Main flow
def main() -> int:
    init_console()
    ap = argparse.ArgumentParser(
        description="Import your muse.ai login cookies into the muse2api account pool in one step")
    ap.add_argument("--base", default=os.environ.get("MUSE2API_BASE", ""),
                    help="muse2api URL, e.g. http://your-server-ip:18610")
    ap.add_argument("--key", default=os.environ.get("MUSE2API_KEY", ""),
                    help="API Key (starts with m2a_)")
    ap.add_argument("--label", default="", help="Account label, e.g. acc-01")
    ap.add_argument("--timeout", type=int, default=300,
                    help="Max seconds to wait for login, default 300")
    ap.add_argument("--keep-open", action="store_true",
                    help="Don't close the browser window after a successful upload")
    ap.add_argument("--port", type=int, default=CDP_PORT,
                    help=f"Debugging port, default {CDP_PORT}")
    ap.add_argument("--chrome", default="", help="Manually specify the browser executable path")
    args = ap.parse_args()

    say("=" * 66)
    say("  muse2api Cookie Helper")
    say("=" * 66)
    say()

    if not args.base or not args.key:
        say("✗ Missing arguments. Run it like this:")
        say("    python get_muse_cookie.py --base https://your-domain --key m2a_xxx")
        say()
        say("  (Both values can be copied from \"Connection info\" on the muse2api admin page)")
        return 2
    if not args.label:
        args.label = "muse-" + time.strftime("%m%d-%H%M")

    chrome = args.chrome or find_chrome()
    if not chrome:
        say("✗ Chrome / Edge browser not found.")
        say("  Please install Chrome first, or specify it manually with --chrome \"full path\".")
        return 3
    say(f"Browser: {chrome}")

    profile = os.path.join(tempfile.gettempdir(), "muse2api-cookie-profile")
    shutil.rmtree(profile, ignore_errors=True)
    os.makedirs(profile, exist_ok=True)

    args_cmd = [
        chrome,
        f"--remote-debugging-port={args.port}",
        f"--user-data-dir={profile}",
        "--no-first-run", "--no-default-browser-check",
        # In restricted environments (containers / sandboxes / CI) Chrome's built-in sandbox fails to initialise:
        #   "sandbox initialization failed: Operation not permitted"
        # → the GPU/network subprocesses crash → Chrome exits and the debugging port never listens,
        #   which shows up as "Connection closed / Broken pipe" right at the WebSocket handshake.
        # The two flags below work around it. No side effects on a normal desktop (only Chrome's own sandbox is disabled).
        "--no-sandbox", "--disable-gpu",
        "--new-window", SITE,
    ]
    say("Opening a separate browser window (your everyday browser is untouched)...")
    proc = subprocess.Popen(args_cmd, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)

    ws = None
    try:
        try:
            ws_url = wait_cdp(args.port, 30)
        except RuntimeError as exc:
            say(f"✗ {exc}")
            say("  Tip: if the browser is already open, quit it completely and try again.")
            say("  (While your everyday Chrome is running it takes over the new window, so the debugging port can't start.)")
            return 4
        ws = WS(ws_url)

        say()
        say("┌" + "─" * 64 + "┐")
        say("│  Log in to muse.ai in the browser window that just opened.     │")
        say("│  Once you see the chat screen, cookies are captured for you.   │")
        say("└" + "─" * 64 + "┘")
        say()

        deadline = time.time() + args.timeout
        got: dict[str, dict] = {}
        last_note = 0.0
        while time.time() < deadline:
            try:
                got = read_cookies(ws)
            except Exception as exc:                    # noqa: BLE001
                say(f"  Error reading cookies, retrying... ({exc})")
                time.sleep(2)
                continue

            have = [n for n in ESSENTIAL if n in got]
            if len(have) == len(ESSENTIAL):
                break

            now = time.time()
            if now - last_note > 5:
                last_note = now
                left = int(deadline - now)
                say(f"  Waiting for login... got {len(have)}/{len(ESSENTIAL)} core cookies"
                    f" ({', '.join(have) or 'none'}), {left}s left")
            time.sleep(2)
        else:
            say()
            say("✗ Timed out waiting; login not completed yet.")
            say("  Please rerun the script and finish logging in inside the window.")
            return 5

        say()
        say("✓ Got the full set of session cookies:")
        say()
        say(f"  {'cookie name':<30}{'httpOnly':<10}{'expires'}")
        say("  " + "-" * 62)
        for name in ESSENTIAL:
            c = got.get(name, {})
            say(f"  {name:<30}{('yes' if c.get('httpOnly') else 'no'):<10}"
                f"{fmt_ts(c.get('expires', -1))}")
        say()

        say("Uploading to muse2api...")
        try:
            r = upload(args.base, args.key, args.label, got)
        except RuntimeError as exc:
            say(f"✗ {exc}")
            say()
            say("  The cookies were captured but the upload failed. You can copy the line below")
            say("  and paste it into the \"Import account\" box on the admin page:")
            say()
            say("  " + "; ".join(f"{k}={v['value']}" for k, v in got.items()))
            return 6

        say()
        say("=" * 66)
        say(f"✓ Imported successfully! Account label: {r['added'][0]['label']}")
        say(f"  Account ID: {r['added'][0]['id']}")
        say(f"  {r['added'][0]['cookie_count']} cookies in total")
        if r.get("warning"):
            say(f"  ⚠ {r['warning']}")
        say()
        say(f"  Open {args.base.rstrip('/')}/ to see it in the account pool.")
        say("=" * 66)
        return 0

    finally:
        if ws:
            ws.close()
        if not args.keep_open:
            try:
                proc.terminate()
            except Exception:                           # noqa: BLE001
                pass
        shutil.rmtree(profile, ignore_errors=True)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        say()
        say("Cancelled.")
        sys.exit(130)
