"""Bounded immutable evidence payload reuse across authorized batch reads.

The caller must first select the evidence id/hash through current permissions
and cutoff filters. This helper never selects or authorizes an evidence row.
"""
from collections import OrderedDict
from threading import RLock

MAX_BYTES = 64 * 1024 * 1024
MAX_PROTECTED_BYTES = 8 * 1024 * 1024
_payloads = OrderedDict()
_protected = OrderedDict()
_bytes = 0
_protected_bytes = 0
_lock = RLock()


def get(key):
    with _lock:
        pool = _protected if key in _protected else _payloads
        value = pool.get(key)
        if value is not None:
            pool.move_to_end(key)
        return value[0] if value else None


def put(key, payload, *, protected=False):
    import json
    global _bytes, _protected_bytes
    # Python dictionaries/strings exceed wire JSON size. Charge a conservative
    # expansion factor so a few full-market snapshots cannot fill the process.
    size = len(json.dumps(payload, ensure_ascii=False).encode()) * 4
    if size > MAX_BYTES:
        return
    with _lock:
        old = _payloads.pop(key, None)
        if old:
            _bytes -= old[1]
        old = _protected.pop(key, None)
        if old:
            _bytes -= old[1]
            _protected_bytes -= old[1]
        protect = protected and size <= min(MAX_PROTECTED_BYTES, MAX_BYTES)
        pool = _protected if protect else _payloads
        pool[key] = (payload, size)
        _bytes += size
        if protect:
            _protected_bytes += size
        # Small expensive indexes must survive a scan of large raw tables.
        # Both pools still share the original total process memory budget.
        while _protected_bytes > min(MAX_PROTECTED_BYTES, MAX_BYTES):
            _, (_, removed) = _protected.popitem(last=False)
            _protected_bytes -= removed
            _bytes -= removed
        while _bytes > MAX_BYTES:
            pool = _payloads if _payloads else _protected
            _, (_, removed) = pool.popitem(last=False)
            if pool is _protected:
                _protected_bytes -= removed
            _bytes -= removed


def clear():
    global _bytes, _protected_bytes
    with _lock:
        _payloads.clear()
        _protected.clear()
        _bytes = 0
        _protected_bytes = 0
