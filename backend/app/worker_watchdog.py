"""Bound a stuck worker operation independently of lease-renewal heartbeats."""
import logging
import os
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar


_job_progress = ContextVar('job_progress', default=None)


@contextmanager
def progress_reporting(callback):
    """Progress belongs to this synchronous job, never its renewal thread."""
    token = _job_progress.set(callback)
    try:
        yield
    finally:
        _job_progress.reset(token)


def report_progress(detail):
    callback = _job_progress.get()
    if callback is not None:
        callback(detail)


DEADLINES = {'scheduler': 180, 'realtime': 300, 'recovery': 1800, 'general': 900, 'all': 1800, 'read-cache': 150}


class ProgressWatchdog:
    def __init__(self, role, *, clock=time.monotonic, exit_process=os._exit):
        self.timeout = DEADLINES[role]
        self.clock, self.exit_process = clock, exit_process
        self.updated = clock()
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._run, name='progress-watchdog', daemon=True)

    def advance(self):
        self.updated = self.clock()

    def expired(self):
        return self.clock() - self.updated > self.timeout

    def _run(self):
        while not self.stop_event.wait(5):
            if self.expired():
                logging.getLogger('jiulong.worker').error('worker progress deadline exceeded; restarting process')
                self.exit_process(70)
                return

    def start(self):
        self.thread.start()

    def close(self):
        self.stop_event.set()
        self.thread.join(timeout=2)
