"""One bounded time-rejection receipt, never an upstream body or acceptance rule."""
from datetime import UTC, datetime
import hashlib
import json
import re

from app.market.protocol import SourceFailure


TABLES = {'stock_funds': 80, 'industry_funds': 50, 'concept_funds': 50}
FIELDS = {'version', 'stage', 'table', 'page', 'row_index', 'code', 'source_epoch',
    'source_time', 'page_received_at', 'capture_received_at', 'ahead_us', 'page_hash'}


def rejection(*, stage, table, page, row_index, code, source_time, page_received_at,
              body, capture_received_at=None):
    error = SourceFailure('future_source_time')
    difference = source_time - page_received_at
    error.funds_time_rejection = dict(version=1, stage=stage, table=table, page=page,
        row_index=row_index, code=code, source_epoch=int(source_time.timestamp()),
        source_time=source_time.astimezone(UTC).isoformat(),
        page_received_at=page_received_at.astimezone(UTC).isoformat(),
        capture_received_at=capture_received_at.astimezone(UTC).isoformat() if capture_received_at else None,
        ahead_us=(difference.days * 86400 + difference.seconds) * 1_000_000 + difference.microseconds,
        page_hash=hashlib.sha256(body).hexdigest())
    return error


def safe_diagnostic(error):
    """Fail closed on metadata shape; diagnostics never alter the original error."""
    value = getattr(error, 'funds_time_rejection', None)
    try:
        if error.reason != 'future_source_time' or not isinstance(value, dict) or set(value) != FIELDS:
            return None
        if type(value['version']) is not int or value['version'] != 1 or value['stage'] not in {'page_validation', 'capture_validation'}:
            return None
        table = value['table']
        if table not in TABLES or type(value['page']) is not int or not 1 <= value['page'] <= TABLES[table]:
            return None
        if type(value['row_index']) is not int or not 0 <= value['row_index'] < 200:
            return None
        if not isinstance(value['code'], str) or not re.fullmatch(r'\d{6}' if table == 'stock_funds' else r'BK\d{4}', value['code']):
            return None
        if type(value['source_epoch']) is not int or type(value['ahead_us']) is not int or not 0 < value['ahead_us'] < 10**18:
            return None
        if not isinstance(value['page_hash'], str) or not re.fullmatch(r'[a-f0-9]{64}', value['page_hash']):
            return None
        clocks = []
        for key in ('source_time', 'page_received_at', 'capture_received_at'):
            raw = value[key]
            if raw is None and key == 'capture_received_at':
                clocks.append(None)
                continue
            if not isinstance(raw, str) or len(raw) > 40:
                return None
            stamp = datetime.fromisoformat(raw)
            if stamp.utcoffset() is None or stamp.utcoffset().total_seconds() != 0:
                return None
            clocks.append(stamp)
        source, received, capture = clocks
        delta = source - received
        if source != datetime.fromtimestamp(value['source_epoch'], UTC) or value['ahead_us'] != (delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds:
            return None
        if value['stage'] == 'page_validation' and (capture is not None or value['ahead_us'] <= 60_000_000):
            return None
        if value['stage'] == 'capture_validation' and (capture is None or capture < received):
            return None
        if len(json.dumps(value)) > 1024:
            return None
        return dict(value)
    except (ValueError, TypeError, KeyError, OverflowError, OSError):
        return None
