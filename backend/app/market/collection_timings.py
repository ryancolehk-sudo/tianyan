"""Bounded stage durations only; no payload, identity, credential or data clock."""
from datetime import datetime
from functools import wraps
from hashlib import sha256
import json
import logging
import math
import time
from uuid import UUID

from app.market.collection_cadence import HISTORY_KEYS

log=logging.getLogger(__name__)
FAMILIES=frozenset({'tencent_web','tencent_market_rt','tushare_rt_k','tushare_market_rt',
    'tushare_rt_indices','fuyao_quotes','fuyao_indices','direction_kpl_top',
    'direction_kpl_strength','ths_industry_funds','ths_concept_funds'})


def _integer(value,maximum):
    return value if type(value) is int and 0<=value<=maximum else None


def _milliseconds(seconds,maximum=86400):
    return round(seconds*1000,3) if type(seconds) in (int,float) and math.isfinite(seconds) and 0<=seconds<=maximum else None


def _wall_span(start,end):
    if not all(isinstance(value,datetime) and value.utcoffset() is not None for value in (start,end)):
        return None
    return _milliseconds((end-start).total_seconds(),315360000)


class CollectionTiming:
    def __init__(self,job,entry_at,*,clock=None):
        self._clock=clock or time.monotonic
        self._entered=self._tick()
        payload=getattr(job,'payload',None)
        context=payload.get('integration_context') if isinstance(payload,dict) else None
        self._eligible=(getattr(job,'kind',None)=='source.collect' and isinstance(payload,dict)
            and payload.get('scheduled_realtime') is True and payload.get('schema_probe') is not True
            and not HISTORY_KEYS.intersection(payload)
            and not (isinstance(context,dict) and context.get('probe') is True))
        self.enabled=False
        self.family=None
        self.attempt=_integer(getattr(job,'attempts',None),255)
        created,available=getattr(job,'created_at',None),getattr(job,'available_at',None)
        self.creation_to_entry=_wall_span(created,entry_at)
        ready=max(created,available) if all(isinstance(value,datetime) and value.utcoffset() is not None
            for value in (created,available)) else None
        self.ready_to_entry=_wall_span(ready,entry_at)
        self._totals={'permit':0.0,'source_gate':0.0,'qt_gate':0.0}
        self._permit_calls=0
        self._first_permit=self._permit_before_first=self._storage=self._commit=None
        self._first_granted=False
        self._receipt_to_storage=None
        self._emitted=False

    def _tick(self):
        try:
            value=self._clock()
            return value if type(value) in (int,float) and math.isfinite(value) else None
        except Exception:
            return None

    def bind(self,family):
        self.enabled=self._eligible and type(family) is str and family in FAMILIES
        self.family=family if self.enabled else None

    @staticmethod
    def _seconds(start,end):
        return end-start if start is not None and end is not None and 0<=end-start<=86400 else None

    def _add(self,key,start,end):
        value=self._seconds(start,end)
        before=self._totals[key]
        self._totals[key]=before+value if before is not None and value is not None else None

    def call(self,key,callback,*args,**kwargs):
        if not self.enabled or key not in {'source_gate','qt_gate'}:
            return callback(*args,**kwargs)
        started=self._tick()
        try:
            return callback(*args,**kwargs)
        finally:
            self._add(key,started,self._tick())

    def permission(self,callback):
        @wraps(callback)
        def measured(*args,**kwargs):
            if not self.enabled:return callback(*args,**kwargs)
            started=self._tick();self._permit_calls+=1
            try:
                result=callback(*args,**kwargs)
            finally:
                ended=self._tick();self._add('permit',started,ended)
            if result is True and not self._first_granted:
                self._first_granted=True
                self._first_permit=ended
                self._permit_before_first=self._totals['permit']
            return result
        return measured

    def storage_started(self,received_at,at):
        if self.enabled:
            self._storage=self._tick()
            self._receipt_to_storage=_wall_span(received_at,at)

    def committed(self):
        if self.enabled:self._commit=self._tick()

    def finish(self,run_id,status,requests,*,health_returned):
        if not self.enabled or self._emitted:return
        self._emitted=True
        # A diagnostics/handler failure must preserve the collection result and
        # the exact exception from a post-commit health callback.
        try:
            ended=self._tick()
            first=self._seconds(self._entered,self._first_permit)
            outside=first-self._permit_before_first if first is not None and self._permit_before_first is not None else None
            value=dict(v=1,family=self.family,attempt=self.attempt,
                run_tag=sha256(run_id.bytes).hexdigest()[:16] if isinstance(run_id,UUID) else None,
                run_status=status if type(status) is str and status in {'complete','partial','error','unavailable'} else None,
                requests=_integer(requests,10000),creation_to_entry_ms=self.creation_to_entry,
                ready_to_entry_ms=self.ready_to_entry,to_first_permit_ms=_milliseconds(first),
                outside_permit_before_first_ms=_milliseconds(outside),permit_calls=_integer(self._permit_calls,10000),
                permit_ms=_milliseconds(self._totals['permit']),source_gate_ms=_milliseconds(self._totals['source_gate']),
                qt_gate_ms=_milliseconds(self._totals['qt_gate']),
                first_permit_to_storage_ms=_milliseconds(self._seconds(self._first_permit,self._storage)),
                receipt_to_storage_wall_ms=self._receipt_to_storage,
                storage_commit_ms=_milliseconds(self._seconds(self._storage,self._commit)),
                health_callback_ms=_milliseconds(self._seconds(self._commit,ended)),
                health_callback_returned=health_returned is True,
                collector_ms=_milliseconds(self._seconds(self._entered,ended)))
            encoded=json.dumps(value,separators=(',',':'),allow_nan=False)
            if len(encoded)<2048:log.info('source_collection_stages %s',encoded)
        except Exception:
            pass
