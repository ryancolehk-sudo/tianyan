"""Bounded RFC 9110 Retry-After parsing; never a vendor quota or rate limit."""
from datetime import UTC,datetime
from email.utils import parsedate_to_datetime
from math import ceil
import re

from app.market.protocol import SourceFailure

MAX_RETRY_AFTER=315360000


def retry_after(value,clock=None):
    if not isinstance(value,str):return None
    value=value.strip()
    if re.fullmatch(r'[0-9]+',value):
        if len(value)>10 or int(value)>MAX_RETRY_AFTER:
            raise SourceFailure('retry_after_out_of_range')
        return int(value)
    if not value or len(value)>100:return None
    try:
        deadline=parsedate_to_datetime(value)
        # RFC 9110 also permits the obsolete asctime HTTP-date, in UTC.
        if deadline.tzinfo is None:
            if not re.fullmatch(r'[A-Za-z]{3} [A-Za-z]{3} [ 0-9][0-9] [0-9]{2}:[0-9]{2}:[0-9]{2} [0-9]{4}',value):return None
            deadline=deadline.replace(tzinfo=UTC)
        seconds=max(0,ceil((deadline-(clock or datetime.now(UTC))).total_seconds()))
    except (ValueError,TypeError,OverflowError):return None
    if seconds>MAX_RETRY_AFTER:raise SourceFailure('retry_after_out_of_range')
    return seconds
