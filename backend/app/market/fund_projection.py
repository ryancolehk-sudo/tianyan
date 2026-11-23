"""Refresh a selected funds overlay without rebuilding unchanged market tables."""
import json
from datetime import date, datetime

from app.evidence.common import now
from app.market.protocol import SHANGHAI


def warm_current_funds():
    """Prepare public saved inputs after a worker restart, without user demand."""
    from sqlalchemy.orm import Session
    from app.db import engine
    from app.market.intraday_funds import intraday_funds
    from app.market.session import market_session
    from app.maintenance_control import paused
    with Session(engine()) as db:
        db.connection(execution_options={'isolation_level':'REPEATABLE READ','postgresql_readonly':True})
        if paused(db):
            return False
        clock = now(db)
        intraday_funds(db, clock.astimezone(SHANGHAI).date(), clock, session=market_session(db, clock),
            owner_id=None, current=True)
        return True


def refresh_selected_funds(db, entry, manifest):
    from app.market.intraday_funds import intraday_funds
    from app.market.session import market_session
    from app.read_cache import fingerprint, quote_aged

    payload = entry.payload
    if entry.operation != 'workbench' or entry.params.get('view') not in {'overview', 'funds'} or not isinstance(payload, dict):
        return None
    previous = payload.get('_dependency_manifest')
    if not isinstance(previous, dict) or fingerprint(previous) != entry.revision:
        return None
    if {k:v for k,v in previous.items() if k != 'dependencies'} != {k:v for k,v in manifest.items() if k != 'dependencies'}:
        return None
    prior, current = previous['dependencies'], manifest['dependencies']
    key = 'funds-selection:' + str(entry.owner_id)
    if prior.keys() != current.keys() or key not in current or current[key] <= prior[key]:
        return None
    if any(prior[name] != current[name] for name in current if name != key):
        return None
    data = payload.get('data', {})
    clock = now(db)
    day = clock.astimezone(SHANGHAI).date()
    if entry.params.get('as_of_time') or entry.params.get('day') not in (None, day.isoformat()) or data.get('trade_day') != day.isoformat():
        return None
    try:
        observed = datetime.fromisoformat(data['market_session']['server_time'])
        if observed.utcoffset() is None or observed > clock or quote_aged(payload):
            return None
    except (KeyError, TypeError, ValueError):
        return None
    session = market_session(db, clock)
    if session.phase != data['market_session'].get('phase'):
        return None
    funds = intraday_funds(db, date.fromisoformat(data['trade_day']), clock,
        sector=entry.params.get('sector'), compare_codes=tuple(filter(None,(entry.params.get('fund_compare') or '').split(','))),
        session=session, owner_id=entry.owner_id, current=True)
    # These DTOs already own their JSON serializers. Avoid walking every
    # encoded chart point a second time through the generic FastAPI encoder.
    encoded = funds.model_dump(mode='json', by_alias=True)
    # A previously future native receipt may become visible without another
    # insert. Changing the native ranking can change the default board/members;
    # that case still needs the complete normal projection.
    if encoded['sectors'] != (data.get('intraday_funds') or {}).get('sectors'):
        return None
    # The stored projection contains JSON values only. A JSON round trip
    # creates an independent tree without deepcopy's per-node Python dispatch.
    # The old funds and session are being replaced, so copying their thousands
    # of points first would only allocate and immediately discard that tree.
    retained = {**payload, 'data': {key:value for key,value in data.items()
        if key not in {'intraday_funds', 'market_session'}}}
    result = json.loads(json.dumps(retained, ensure_ascii=False))
    result['data']['market_session'] = session.model_dump(mode='json', by_alias=True)
    result['data']['intraday_funds'] = encoded
    capability = dict(key='intraday_funds', label='盘中主力资金采样',
        status={'available':'partial','stale':'stale','missing':'missing','permission_denied':'permission_denied'}[funds.state],
        reason=funds.scope, source_family=funds.source_family, last_received_at=encoded['received_at'])
    capabilities = result['data']['capabilities']
    for index in range(len(capabilities) - 1, -1, -1):
        if capabilities[index]['key'] == 'intraday_funds':
            capabilities[index] = capability
            break
    else:
        return None
    return result
