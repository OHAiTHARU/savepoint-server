"""UUIDv7 (RFC 9562): 48-bit ms timestamp + random. Time-ordered, so IDs sort
in creation order, which the version list relies on for `before` cursors."""

from __future__ import annotations

import os
import threading
import time
import uuid

_lock = threading.Lock()
_last_ms = 0
_counter = 0


def uuid7() -> str:
    global _last_ms, _counter
    with _lock:
        ms = time.time_ns() // 1_000_000
        if ms <= _last_ms:
            # Same (or backwards) millisecond: keep monotonic with a 12-bit counter.
            ms = _last_ms
            _counter += 1
            if _counter > 0xFFF:
                ms += 1
                _counter = 0
        else:
            _counter = int.from_bytes(os.urandom(2), "big") & 0x3FF
        _last_ms = ms
        counter = _counter
    rand_b = int.from_bytes(os.urandom(8), "big") & ((1 << 62) - 1)
    value = (ms & ((1 << 48) - 1)) << 80
    value |= 0x7 << 76
    value |= (counter & 0xFFF) << 64
    value |= 0b10 << 62
    value |= rand_b
    return str(uuid.UUID(int=value))
