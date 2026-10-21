#!/usr/bin/env python3
"""Mail.tm disposable mailbox: create an address and poll for the verification code.

Purpose: when muse.ai login asks for an email verification code, run this script to get
      a disposable email address, enter it on the login page, and the script prints the code it receives.

Zero dependencies (standard library only). Usage:
    python3 mailtm_otp.py                # create a new address and start polling
    python3 mailtm_otp.py --addr a@b.c --pass xxx   # reuse an existing address
    python3 mailtm_otp.py --timeout 600  # polling duration (seconds), default 900
"""
import argparse, json, random, re, string, sys, time, urllib.request, urllib.error

API = "https://api.mail.tm"


def req(path, method="GET", token=None, body=None):
    """Send a request and return the parsed JSON."""
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(API + path, data=data, method=method)
    r.add_header("Content-Type", "application/json")
    r.add_header("Accept", "application/json")
    if token:
        r.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(r, timeout=20) as resp:
            raw = resp.read().decode()
            return json.loads(raw) if raw.strip() else {}
    except urllib.error.HTTPError as e:
        detail = e.read().decode()[:300]
        raise SystemExit(f"[!] {method} {path} -> HTTP {e.code}: {detail}")


def new_account():
    """Create a new address and return (address, password, token)."""
    d = req("/domains")
    domains = d["hydra:member"] if isinstance(d, dict) else d  # newer API returns an array, older wraps it in hydra
    domain = next(x["domain"] for x in domains if x.get("isActive", True))
    user = "".join(random.choices(string.ascii_lowercase + string.digits, k=12))
    addr, pwd = f"{user}@{domain}", "".join(random.choices(string.ascii_letters + string.digits, k=16))
    req("/accounts", "POST", body={"address": addr, "password": pwd})
    token = req("/token", "POST", body={"address": addr, "password": pwd})["token"]
    return addr, pwd, token


def login(addr, pwd):
    return req("/token", "POST", body={"address": addr, "password": pwd})["token"]


CODE_RE = re.compile(r"(?<!\d)(\d{4,8})(?!\d)")


def poll(token, timeout):
    """Poll for new mail and print the verification code when found. Returns the code or None."""
    seen, deadline = set(), time.time() + timeout
    print(f"[*] Polling inbox (up to {timeout}s, Ctrl-C to stop)\n")
    while time.time() < deadline:
        try:
            r = req("/messages?page=1", token=token)
            msgs = r["hydra:member"] if isinstance(r, dict) else r
        except SystemExit:
            time.sleep(3); continue
        for m in msgs:
            if m["id"] in seen:
                continue
            seen.add(m["id"])
            full = req(f"/messages/{m['id']}", token=token)
            frm = (full.get("from") or {}).get("address", "?")
            subj = full.get("subject", "")
            body = full.get("text") or full.get("intro") or ""
            print(f"[+] New mail  from {frm}\n    Subject: {subj}")
            codes = CODE_RE.findall(f"{subj}\n{body}")
            print(f"    Body: {body.strip()[:500]}")
            if codes:
                print(f"\n>>> Verification code: {codes[0]}\n")
                return codes[0]
            print("    (No verification code found, still waiting...)\n")
        left = int(deadline - time.time())
        print(f"    Waiting... {left}s left", end="\r", flush=True)
        time.sleep(3)
    print("\n[!] Timed out, no verification code received.")
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--addr"); ap.add_argument("--pass", dest="pwd")
    ap.add_argument("--timeout", type=int, default=900)
    a = ap.parse_args()
    if a.addr and a.pwd:
        addr, pwd, token = a.addr, a.pwd, login(a.addr, a.pwd)
        print(f"[*] Reusing existing address: {addr}")
    else:
        addr, pwd, token = new_account()
        print(f"[*] Created new address: {addr}\n    Password: {pwd} (needed to reuse it)")
    print(f"\n=== Enter this address on the muse.ai login page ===\n    {addr}\n")
    code = poll(token, a.timeout)
    sys.exit(0 if code else 1)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[*] Stopped.")
