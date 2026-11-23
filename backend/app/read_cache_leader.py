"""One live-read builder; its database session proves when a worker is gone."""
import time

import psycopg
from sqlalchemy.engine import make_url

from app.config import settings

LOCK = (0x5449414E, 0x52454144)


class ReadCacheLeader:
    def __init__(self):
        self.connection = None
        self.owned = False
        self.reclaim_pending = False
        self.retry_after = 0

    def acquire(self):
        if time.monotonic() < self.retry_after:
            return False
        try:
            if self.connection is not None and not self.connection.closed:
                self.connection.execute('SELECT 1')
                return self.owned
            self.close()
            url = make_url(settings().database_url).set(drivername='postgresql')
            self.connection = psycopg.connect(url.render_as_string(hide_password=False), autocommit=True,
                connect_timeout=2, application_name='tianyan_read_cache_leader')
            self.owned = bool(self.connection.execute('SELECT pg_try_advisory_lock(%s, %s)', LOCK).fetchone()[0])
            if not self.owned:
                self.close()
                return False
            self.reclaim_pending = True
            return True
        except Exception:
            self.close()
            self.retry_after = time.monotonic() + 5
            raise

    def reclaim(self):
        if not self.owned or self.connection is None or self.connection.closed:
            raise RuntimeError('read_cache_leader_required')
        if not self.reclaim_pending:
            return 0
        # This transaction uses the SAME session holding the leader lock.
        # A replacement cannot start while it is reclaiming; a lost session
        # rolls back before another leader can own the lock.
        with self.connection.transaction():
            result = self.connection.execute("""
                UPDATE read_cache_entries
                SET lease_token=NULL,lease_until=NULL,dirty=true,
                    last_error='worker_restarted',refresh_after=clock_timestamp()
                WHERE lease_token IS NOT NULL AND operation IN ('workbench','details')
                  AND params->>'as_of_time' IS NULL
                  AND (params->>'day' IS NULL OR params->>'day'=
                    to_char(clock_timestamp() AT TIME ZONE 'Asia/Shanghai','YYYY-MM-DD'))
                """)
            count = result.rowcount
        self.reclaim_pending = False
        return count

    def close(self):
        connection, self.connection = self.connection, None
        self.owned = self.reclaim_pending = False
        if connection is not None:
            connection.close()
