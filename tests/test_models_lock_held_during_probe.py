"""Prove: get_available_models() holds _available_models_cache_lock across the
provider probe, so a slow probe blocks every other models-path caller.

This is the real defect behind "Slow WebUI request still running" on
/api/models and a stalled turn-start. It is NOT lock recursion: the RLock at
api/config.py:6706 exists precisely because slow work was moved inside this
lock, which is why a second acquire by the same thread succeeds while a second
*thread* blocks.

Run: ./scripts/test.sh tests/test_models_lock_held_during_probe.py -q
"""
from __future__ import annotations

import threading
import time

import api.config as cfg


def test_unrelated_caller_blocks_behind_slow_probe(monkeypatch):
    """An unrelated caller must not block behind slow work inside the lock.

    FAILS if the probe runs while _available_models_cache_lock is held.
    """
    probe_started = threading.Event()
    probe_release = threading.Event()
    acquired_after: list[float] = []

    def _slow_probe(*_a, **_k):
        # Stands in for the live per-provider HTTPS probe (Copilot token
        # exchange / provider catalog fetch) that hangs on slow networks.
        probe_started.set()
        probe_release.wait(timeout=10)
        return {}

    monkeypatch.setattr(cfg, "_invoke_models_rebuild", _slow_probe)

    def _foreground():
        try:
            cfg.get_available_models(force_refresh=True)
        except Exception:
            pass

    def _observer():
        probe_started.wait(timeout=10)
        t0 = time.monotonic()
        with cfg._available_models_cache_lock:
            acquired_after.append(time.monotonic() - t0)

    fg = threading.Thread(target=_foreground, daemon=True)
    ob = threading.Thread(target=_observer, daemon=True)
    fg.start()
    ob.start()

    assert probe_started.wait(timeout=10), "probe never started"
    time.sleep(0.5)  # let the observer contend while the probe is in flight
    blocked_while_probing = not acquired_after
    probe_release.set()

    fg.join(timeout=15)
    ob.join(timeout=15)

    assert acquired_after, "observer never acquired the lock"
    assert not blocked_while_probing, (
        "REAL DEFECT CONFIRMED: an unrelated models-path caller could not "
        "acquire _available_models_cache_lock while the provider probe ran; "
        f"it waited {acquired_after[0]:.2f}s behind network I/O held inside "
        "the lock (api/config.py:6706, 10177)."
    )
