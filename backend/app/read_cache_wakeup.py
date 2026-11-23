"""A commit wakeup supplements the durable queue; it never holds market data."""
import logging
import time

import psycopg
from sqlalchemy.engine import make_url

from app.config import settings

log=logging.getLogger(__name__)


class ReadCacheWakeup:
    def __init__(self):
        self.connection=None
        self.retry_after=0

    def connect(self):
        if self.connection is not None and not self.connection.closed:return False
        url=make_url(settings().database_url).set(drivername='postgresql')
        self.connection=psycopg.connect(url.render_as_string(hide_password=False),autocommit=True,
            connect_timeout=2,application_name='tianyan_read_cache_notifications')
        self.connection.execute('LISTEN tianyan_read_dirty')
        return True

    def wait(self):
        if time.monotonic()<self.retry_after:
            time.sleep(1);return
        try:
            if self.connect():
                # Poll the durable queue again immediately after LISTEN, which
                # covers commits made just before this connection was ready.
                return
            for _ in self.connection.notifies(timeout=1,stop_after=1):break
        except Exception as exc:
            self.close()
            self.retry_after=time.monotonic()+5
            log.warning('read_cache_notifications_unavailable kind=%s',type(exc).__name__)
            time.sleep(1)

    def close(self):
        connection,self.connection=self.connection,None
        if connection is not None:
            try:connection.close()
            except Exception:pass
