"""Choose explicit calendar receipts after caller rights and receipt filtering."""
from datetime import date

SOURCES={
    'SSE':('tushare_calendar_sse','baostock_daily'),
    'SZSE':('tushare_calendar_szse','baostock_daily','szse_public'),
}
FAMILIES=frozenset(family for candidates in SOURCES.values() for family in candidates)
READ_VERSION='calendar-fallback-1'


def selected_days(rows,families):
    """Rows are newest-version first; an invalid correction stays unknown.

    A permitted paid receipt wins for its own day, including closed/unknown.
    Missing paid dates can use existing explicit free receipts. The source key
    identifies the date before payload/provenance checks, so a correction may
    invalidate a date without resurrecting an earlier apparently valid row.
    """
    latest=set();chosen={'SSE':{},'SZSE':{}}
    for row in rows:
        family=families.get(row.source_id)
        parts=row.source_key.split(':')
        if len(parts)!=3 or parts[0]!='calendar' or parts[1] not in SOURCES:
            continue
        exchange,value=parts[1:]
        if family not in SOURCES[exchange]:
            continue
        try:
            day=date.fromisoformat(value)
        except (TypeError,ValueError):
            continue
        if day.isoformat()!=value:
            continue
        identity=(row.source_id,row.source_key)
        if identity in latest:
            continue
        latest.add(identity)
        priority=SOURCES[exchange].index(family)
        old=chosen[exchange].get(day)
        if old is not None and old[0]<=priority:
            continue
        payload=row.payload
        opened=payload.get('is_open') if isinstance(payload,dict) else None
        valid=(row.provenance=='source_reported' and isinstance(payload,dict)
            and payload.get('exchange')==exchange and payload.get('trade_day')==value
            and type(opened) is bool)
        chosen[exchange][day]=(priority,opened if valid else None,row)
    values={exchange:{day:item[1] for day,item in rows.items()} for exchange,rows in chosen.items()}
    inputs=[item[2] for rows in chosen.values() for item in rows.values()]
    return values,inputs


def common_open_days(calendars):
    return sorted(day for day,opened in calendars['SSE'].items()
        if opened is True and calendars['SZSE'].get(day) is True)
