"""Bounded shared price universes, after current evidence authorization.

Only owner-independent values belong here. Watchlists, selected stocks/boards,
member labels, histories and page rankings are applied to fresh copies later.
"""
from collections import OrderedDict
import json
from threading import RLock

MAX_BYTES = 16 * 1024 * 1024
MAX_ENTRIES = 2
_values = OrderedDict()
_bytes = 0
_lock = RLock()


def get(key):
    with _lock:
        entry = _values.get(key)
        if entry is not None:
            _values.move_to_end(key)
            return entry[0]
    return None


def put(key, value):
    global _bytes
    size = len(json.dumps(value, default=str, ensure_ascii=False).encode()) * 4
    if size > MAX_BYTES:
        return
    with _lock:
        previous = _values.pop(key, None)
        if previous:
            _bytes -= previous[1]
        _values[key] = (value, size)
        _bytes += size
        while _bytes > MAX_BYTES or len(_values) > MAX_ENTRIES:
            _, (_, removed) = _values.popitem(last=False)
            _bytes -= removed


def clear():
    global _bytes
    with _lock:
        _values.clear()
        _bytes = 0
