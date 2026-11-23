"""Small public native-funds projections; selection and health stay outside.

Callers check current source rights first. Exact source revisions, instance,
configuration and receipt bounds prevent reuse across evidence/time changes.
"""
import copy
from collections import OrderedDict
from threading import RLock

MAX_BYTES = 16 * 1024 * 1024
MAX_ENTRIES = 2
_values = OrderedDict()
_bytes = 0
_lock = RLock()


def get(key, cutoff):
    with _lock:
        item = _values.get(key)
        if not item:
            return None
        (model, encoded), facts, inputs, _ = item
        if any(at is not None and at > cutoff for at in (facts['max_receipt'], facts['max_source_time'])):
            return None
        _values.move_to_end(key)
        # Pydantic's JSON validator creates independent models without walking
        # thousands of Python model internals on every selected-object update.
        return model.model_validate_json(encoded), copy.deepcopy(facts), copy.deepcopy(inputs)


def put(key, result, facts, inputs):
    global _bytes
    if facts.get('future_seen') or not {'max_receipt','max_source_time','clocks','has_active','held'}.issubset(facts):
        return
    encoded = result.model_dump_json()
    size = len(encoded.encode()) * 4 + len(inputs) * 800
    if size > MAX_BYTES:
        return
    item = ((type(result), encoded), copy.deepcopy(facts), copy.deepcopy(inputs), size)
    with _lock:
        old = _values.pop(key, None)
        if old:
            _bytes -= old[3]
        _values[key] = item
        _bytes += size
        while _bytes > MAX_BYTES or len(_values) > MAX_ENTRIES:
            _, old = _values.popitem(last=False)
            _bytes -= old[3]


def clear():
    global _bytes
    with _lock:
        _values.clear()
        _bytes = 0
