"""Tests for the multi-account concurrency scheduler and isolation."""
import os
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

# Make sure the muse2api in the repo root is imported
root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))

import app
from config import Config
from store import Store


def test_concurrency_lock():
    lock = app.ConcurrencyLock(limit=2)
    assert not lock.locked()

    # First slot
    assert lock.acquire(timeout=0.1)
    assert lock.locked()

    # Second slot (concurrency = 2)
    assert lock.acquire(timeout=0.1)
    assert lock.locked()

    # Third slot should time out
    assert not lock.acquire(blocking=True, timeout=0.05)

    # Release one slot
    lock.release()
    assert lock.locked()

    # Now the third one can be acquired
    assert lock.acquire(timeout=0.1)

    lock.release()
    lock.release()
    assert not lock.locked()
    print("PASS: test_concurrency_lock")


def test_account_pool_manager():
    dummy_accounts = [
        {"id": "acc-1", "label": "A1", "enabled": True, "cookies": {"k": "v1"}},
        {"id": "acc-2", "label": "A2", "enabled": True, "cookies": {"k": "v2"}},
    ]
    dummy_store = SimpleNamespace(
        accounts=dummy_accounts,
        get_account=lambda aid: next((a for a in dummy_accounts if a["id"] == aid), None),
        pick_account=lambda preferred_id=None, rotate=True, force_rotate=False, exclude_ids=None, record_use=True: next(
            (a for a in dummy_accounts if a["id"] not in (exclude_ids or set())), None
        )
    )

    pool = app.AccountPoolManager(dummy_store)

    # Thread 1 takes acc-1
    a1 = pool.acquire(timeout=0.5)
    assert a1["id"] == "acc-1"
    assert "acc-1" in pool.busy_accounts

    # Thread 2 concurrently takes acc-2 (automatically skips the busy acc-1)
    a2 = pool.acquire(timeout=0.5)
    assert a2["id"] == "acc-2"
    assert "acc-2" in pool.busy_accounts

    # Both accounts are busy now; thread 3 should wait and time out
    t0 = time.monotonic()
    try:
        pool.acquire(timeout=0.1)
        raise AssertionError("Expected a timeout")
    except app.MuseGenerationError:
        pass
    assert time.monotonic() - t0 >= 0.08

    # After acc-1 is released, thread 3 gets acc-1 immediately
    pool.release(a1["id"])
    assert "acc-1" not in pool.busy_accounts

    a3 = pool.acquire(timeout=0.5)
    assert a3["id"] == "acc-1"

    # Clean up
    pool.release(a2["id"])
    pool.release(a3["id"])
    assert len(pool.busy_accounts) == 0
    print("PASS: test_account_pool_manager")


def test_parallel_generation_simulation():
    """Simulate two requests generating concurrently on different accounts."""
    dummy_accounts = [
        {"id": "acc-1", "label": "A1", "enabled": True, "cookies": {"c": "1"}},
        {"id": "acc-2", "label": "A2", "enabled": True, "cookies": {"c": "2"}},
    ]
    orig_accounts = list(app.store.accounts)
    running_threads = set()
    active_accounts = []
    concurrency_observed = False
    lock = threading.Lock()

    def fake_generate(cookies, prompt, expect, timeout, expires=None, account_id=None, on_progress=None, reference_image=None):
        nonlocal concurrency_observed
        t_name = threading.current_thread().name
        with lock:
            running_threads.add(t_name)
            active_accounts.append(account_id)
            if len(running_threads) >= 2:
                concurrency_observed = True
        time.sleep(0.15)
        with lock:
            running_threads.discard(t_name)
        return {"filename": f"{account_id}.png", "size": 1234, "kind": expect}

    try:
        with patch("store._write", lambda *a, **k: None), \
             patch.object(app.store, "mark", lambda *a, **k: None), \
             patch.object(app.engine, "start", return_value=None), \
             patch.object(app.engine, "generate", side_effect=fake_generate), \
             patch.object(app, "_renew_and_persist", return_value=None), \
             patch.object(app, "_sync_cookies", return_value=None):

            app.store.accounts = dummy_accounts
            results = []
            def run_task(p):
                res = app._run_generation(p, "image", timeout=5)
                results.append(res)

            t1 = threading.Thread(target=run_task, args=("task 1",), name="Worker-1")
            t2 = threading.Thread(target=run_task, args=("task 2",), name="Worker-2")

            t1.start()
            t2.start()
            t1.join()
            t2.join()

            assert len(results) == 2
            assert concurrency_observed, "Both tasks should run concurrently in separate threads!"
            assert set(r[1] for r in results) == {"acc-1", "acc-2"}, "The two tasks should be scheduled on two separate accounts!"
            assert len(app.ACCOUNT_POOL.busy_accounts) == 0, "All accounts should be fully released after the tasks finish!"
    finally:
        app.store.accounts = orig_accounts
    print("PASS: test_parallel_generation_simulation")


if __name__ == "__main__":
    test_concurrency_lock()
    test_account_pool_manager()
    test_parallel_generation_simulation()
    print("ALL CONCURRENCY TESTS PASSED!")
