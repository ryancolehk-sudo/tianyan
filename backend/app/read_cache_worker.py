"""Two bounded read lanes under one leader; never collects data or AI jobs."""
import logging
import re
import socket
import time
import uuid
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path
from traceback import walk_tb

from sqlalchemy.orm import Session

from app.config import settings
from app.db import engine
from app.read_cache import run_one, prepare_current_workbenches
from app.maintenance_control import paused
from app.worker_watchdog import ProgressWatchdog
from app.worker import heartbeat
from app.read_cache_wakeup import ReadCacheWakeup
from app.read_cache_leader import ReadCacheLeader

log = logging.getLogger(__name__)


def failure_location(error):
    """Bounded public source positions, never exception text or frame values."""
    root = Path(__file__).resolve().parent
    locations = []
    for frame, line in walk_tb(error.__traceback__):
        filename = frame.f_code.co_filename.replace('\\', '/')
        try:
            source = Path(filename)
            relative = source.relative_to(root).as_posix()
            if not re.fullmatch(r'[a-zA-Z0-9_/]+\.py', relative):
                continue
            source = source.resolve()
            if not source.is_relative_to(root) or not source.is_file():
                continue
        except (OSError, RuntimeError, ValueError):
            continue
        locations.append(f'{relative}:{line}')
    return '>'.join(locations[-6:]) or 'unavailable'


class Preparation:
    """Housekeeping runs in the ordinary lane, away from visible reads."""
    def __init__(self):
        self.prepare_after = 0
        self.public_funds_prepared = False
        self.public_funds_prepare_after = 0

    def __call__(self, db):
        if not self.public_funds_prepared and time.monotonic() >= self.public_funds_prepare_after:
            from app.market.fund_projection import warm_current_funds
            try:
                self.public_funds_prepared = warm_current_funds()
            except Exception as exc:
                log.warning('read_cache_public_prepare_failed kind=%s location=%s', type(exc).__name__, failure_location(exc))
                self.public_funds_prepare_after = time.monotonic() + 5
        if time.monotonic() >= self.prepare_after:
            self.prepare_after = time.monotonic() + 30
            try:
                prepare_current_workbenches(db)
            except Exception as exc:
                db.rollback()
                log.warning('read_cache_prepare_failed kind=%s location=%s', type(exc).__name__, failure_location(exc))


def refresh(lane, preparation=None):
    # Each execution owns its sessions. No ORM object or transaction is shared
    # with the coordinator or the other lane; build() retains its RR snapshot.
    with Session(engine(), expire_on_commit=False) as db:
        if not settings().read_cache_enabled or paused(db):
            return False
        if preparation is not None:
            preparation(db)
        if not settings().read_cache_enabled or paused(db):
            return False
        return run_one(db, lane=lane)


class ReadLane:
    def __init__(self, name, pool, work):
        self.name, self.pool, self.work = name, pool, work
        self.future = None
        self.retry_after = 0
        self.watchdog = ProgressWatchdog('read-cache')
        self.watchdog.start()

    def collect(self):
        if self.future is None or not self.future.done():
            return
        future, self.future = self.future, None
        try:
            refreshed = future.result()
        except Exception as exc:
            # Do not log saved payloads, SQL or request parameters.
            log.warning('read_cache_refresh_failed lane=%s kind=%s location=%s', self.name, type(exc).__name__, failure_location(exc))
            refreshed = False
        self.watchdog.advance()
        self.retry_after = 0 if refreshed else time.monotonic() + 1

    def launch(self, allowed):
        if self.future is None:
            self.watchdog.advance()
            if allowed and time.monotonic() >= self.retry_after:
                self.future = self.pool.submit(self.work)

    def close(self):
        self.watchdog.close()


def main():
    logging.basicConfig(level=logging.INFO, format='%(message)s')
    watchdog = ProgressWatchdog('read-cache')
    watchdog.start()
    wakeup = ReadCacheWakeup()
    leader = ReadCacheLeader()
    pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix='read-cache')
    preparation = Preparation()
    lanes = [ReadLane('focused', pool, lambda: refresh('focused')),
             ReadLane('other', pool, lambda: refresh('other', preparation))]
    heartbeat_after = 0
    identity = 'read-cache:' + socket.gethostname() + ':' + uuid.uuid4().hex[:8]
    try:
        while True:
            for lane in lanes:
                lane.collect()
            allowed = False
            try:
                if settings().read_cache_enabled and leader.acquire():
                    with Session(engine(), expire_on_commit=False) as db:
                        if not paused(db):
                            # A recovered connection must not reclaim this
                            # process's still-running attempts. Drain them first.
                            if not leader.reclaim_pending or all(lane.future is None for lane in lanes):
                                recovered = leader.reclaim()
                                if recovered:
                                    log.info('read_cache_worker_recovered count=%s', recovered)
                                allowed = True
            except Exception as exc:
                log.warning('read_cache_refresh_failed kind=%s location=%s', type(exc).__name__, failure_location(exc))
            for lane in lanes:
                lane.launch(allowed)
            # Coordinator progress never advances an occupied lane's watchdog.
            # A fast panel or heartbeat cannot hide a stuck ordinary build.
            watchdog.advance()
            if time.monotonic() >= heartbeat_after:
                try:
                    heartbeat(identity)
                except Exception as exc:
                    log.warning('read_cache_heartbeat_failed kind=%s location=%s', type(exc).__name__, failure_location(exc))
                heartbeat_after = time.monotonic() + 10
            running = [lane.future for lane in lanes if lane.future is not None]
            if running:
                delays = [max(0, lane.retry_after - time.monotonic()) for lane in lanes
                          if allowed and lane.future is None]
                wait(running, timeout=min([1, *delays]), return_when=FIRST_COMPLETED)
            else:
                # A notification can make an idle lane due before its fallback
                # second. This never advances a persisted retry/lease deadline.
                wakeup.wait()
                for lane in lanes:
                    lane.retry_after = 0
    finally:
        # Hold leadership until active attempts drain. Their independent
        # watchdogs remain armed during shutdown if a build stops progressing.
        try:
            pool.shutdown(wait=True, cancel_futures=True)
        finally:
            for lane in lanes:
                lane.close()
            leader.close()
            wakeup.close()
            watchdog.close()


if __name__ == '__main__':
    main()
