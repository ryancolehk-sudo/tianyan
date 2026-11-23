"""Forward-only business clock for the disposable acceptance image, never runtime.

Auth, job leases, PostgreSQL and host clocks remain real. The runner alone writes
the separately mounted clock file; the application exposes no clock route.
"""
import json
import os
import re
import sys
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from time import monotonic

CLOCK = Path('/workspace/test-clock/clock.json')
MARKER = Path('/workspace/backend/.disposable-test-image')
last_seen = None
clock_anchor = None
clock_lock = threading.Lock()
WORKER_ROLES = {'worker': 'general', 'scheduler': 'scheduler',
                'realtime-worker': 'realtime', 'recovery-worker': 'recovery'}


def guard():
    if not MARKER.is_file() or MARKER.read_text().strip() != 'disposable-compose-test' or (
            os.getenv('JL_APP_MODE') != 'demo' or os.getenv('JL_ALLOW_ACCEPTANCE_CLOCK') != 'yes' or
            not re.fullmatch(r'jlpm-check-[a-f0-9]{10}-acceptance-(source|restore)', os.getenv('JL_TEST_PROJECT',''))):
        raise SystemExit('Acceptance clock requires the isolated disposable test image and demo project.')


def business_now(_db=None):
    with clock_lock:
        return _read_clock()


def _read_clock():
    global last_seen, clock_anchor
    value = json.loads(CLOCK.read_text())
    if value['project'] != os.environ['JL_TEST_PROJECT']:
        raise RuntimeError('Acceptance clock belongs to another disposable project')
    base = datetime.fromisoformat(value['business_at'])
    real = datetime.fromisoformat(value['real_at'])
    if base.tzinfo is None or real.tzinfo is None:
        raise RuntimeError('Acceptance clock must be timezone aware')
    wall_now, tick = datetime.now(UTC), monotonic()
    version = (base, real)
    # Anchor each explicit fixture once. Host/VM wall-clock corrections must
    # neither rewind business writes nor skip an auction/close checkpoint.
    if clock_anchor and clock_anchor[0] == version:
        current = clock_anchor[1] + timedelta(seconds=tick - clock_anchor[2])
    else:
        if clock_anchor and base < clock_anchor[0][0]:
            raise RuntimeError('Acceptance clock cannot rewind')
        current = base + (wall_now - real)
    if abs((current - wall_now).total_seconds()) > timedelta(days=14).total_seconds():
        raise RuntimeError('Acceptance clock is outside the bounded fixture range')
    if last_seen and current < last_seen:
        raise RuntimeError('Acceptance clock cannot rewind')
    if not clock_anchor or clock_anchor[0] != version:
        clock_anchor = (version, current, tick)
    last_seen = current
    return current.astimezone(UTC)


def install():
    guard()
    # Direct execution puts tests/, rather than backend/, on sys.path.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from sqlalchemy import text
    from app.db import engine
    with engine().connect() as db:
        if db.scalar(text('SELECT app_mode FROM instance_state WHERE id=1')) != 'demo':
            raise SystemExit('Acceptance clock refuses a non-demo database.')
    business_now()
    from app.evidence import common, service as evidence
    from app.research import service as research
    from app.observations import service as observations
    from app.validations import service as validations
    from app import http
    for module in [common, evidence, research, observations, validations]:
        module.now = business_now
    class BusinessDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            value = business_now()
            return value.astimezone(tz) if tz else value.replace(tzinfo=None)
    http.datetime = BusinessDateTime
    # Test-image-only diagnostics: frame locations and exception types expose
    # neither request bodies nor SQL parameters, passwords or report text.
    from app.main import log
    original_error = log.error
    def test_error(message, *args, **kwargs):
        exception = sys.exception()
        if exception is not None:
            frames=[];trace=exception.__traceback__
            while trace is not None:
                frames.append(f'{Path(trace.tb_frame.f_code.co_filename).name}:{trace.tb_lineno}:{trace.tb_frame.f_code.co_name}')
                trace=trace.tb_next
            original_error('acceptance-only exception_type=%s frames=%s',type(exception).__name__,' > '.join(frames))
        return original_error(message,*args,**kwargs)
    log.error=test_error


if __name__ == '__main__':
    install()
    if len(sys.argv) != 2 or sys.argv[1] not in {'api', *WORKER_ROLES}:
        raise SystemExit('Use api or a configured worker inside the disposable acceptance profile.')
    if sys.argv[1] == 'api':
        import uvicorn
        uvicorn.run('app.main:app', host='0.0.0.0', port=8000, access_log=False, proxy_headers=True, forwarded_allow_ips='*')
    else:
        from app.worker import main
        main(['--role', WORKER_ROLES[sys.argv[1]]])
