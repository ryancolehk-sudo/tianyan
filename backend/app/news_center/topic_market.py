"""Source-native board directories and separately dated ranking inputs."""
from collections import defaultdict
from datetime import date, time, timedelta
import re

from sqlalchemy import column, func, select, true
from sqlalchemy.dialects.postgresql import JSONB

from app.evidence.models import EvidenceVersion as E
from app.market import read_facets
from app.market.flash_news import INDUSTRIES
from app.market.intraday_funds import _samples, decimal
from app.market.kpl_atlas_source import DATASET as KPL_DATASET
from app.market.protocol import SHANGHAI
from app.market.review_protocol import DATASET_FAMILIES
from app.market.review_store import load_dataset, manifest_key, permitted_provider


def latest_table(db, family, dataset, cutoff):
    provider = permitted_provider(db, family)
    if not provider:
        return None
    observed = read_facets.observed_day()
    return db.scalar(read_facets.with_facets(select(E)).where(
        E.source_id == provider.id, E.source_key == 'reference:' + dataset, E.kind == 'reference',
        E.provenance == 'source_reported', E.visibility == 'shared', E.owner_id.is_(None),
        E.received_at <= cutoff, read_facets.value('dataset') == dataset,
        observed <= cutoff.astimezone(SHANGHAI).date().isoformat(),
    ).order_by(observed.desc(), E.received_at.desc(), E.version_number.desc()).limit(1))


def eastmoney_boards(db, category, cutoff):
    boards = {k: dict(code=k, name=n, latest=None) for k,n in INDUSTRIES.items()} if category == 'industry' else {}
    evidence = latest_table(db, 'eastmoney_funds', f'market.eastmoney.{category}_funds', cutoff)
    if not evidence:
        return None, boards
    day = date.fromisoformat(evidence.payload['observed_day'])
    rows = _samples(evidence, day, cutoff, category)
    for sample in rows.values():
        if category == 'industry' and sample.industry_level != 2:
            continue
        code = sample.code.removesuffix('.DC')
        entry = boards.setdefault(code, dict(code=code, name=sample.name, latest=None))
        if sample.pct is not None or sample.net is not None:
            entry['market'] = dict(pct_change=sample.pct, main_net_cny=sample.net, source_time=sample.stamp)
    return day if rows else None, boards


def kpl_boards(db, cutoff):
    evidence = latest_table(db, 'direction_kpl_strength', KPL_DATASET, cutoff)
    if not evidence:
        return None, {}
    day = date.fromisoformat(evidence.payload['observed_day'])
    rows = evidence.payload.get('rows') or []
    if not rows:
        return None, {}
    total = rows[0].get('total_count') or ''
    coverage = rows[0].get('coverage')
    if (not total.isdecimal() or not len(rows) <= int(total) <= 400
            or coverage not in {'complete_pages','partial_pages'} or coverage == 'complete_pages' and len(rows) != int(total)):
        return None, {}
    pct_unit = any(col.get('key') == 'pct_change' and col.get('unit') == '%' for col in evidence.payload.get('columns', []))
    boards = {}
    for index, r in enumerate(rows):
        code = r.get('code') or ''
        if (not re.fullmatch(r'\d{6}', code) or code in boards or not r.get('name') or r.get('rank') != str(index+1)
                or r.get('archive_kind') not in {'daily','intraday'} or r.get('source_day') != day.isoformat()
                or r.get('total_count') != total or r.get('coverage') != coverage):
            return None, {}
        pct = decimal(r.get('pct_change')) if pct_unit else None
        boards[code] = dict(code=code, name=r['name'], strength=decimal(r.get('strength')), latest=None,
            market=dict(pct_change=pct, main_net_cny=None, source_time=None, received_at=evidence.received_at) if pct is not None else None)
    return day, boards


_breadth = {}
_memberships = {}


def manifest_days(db, dataset, day, cutoff):
    provider = permitted_provider(db, DATASET_FAMILIES[dataset])
    if not provider:
        return {}
    prefix = 'review.manifest:' + dataset + ':'
    rows = db.execute(select(E.source_key, E.id).distinct(E.source_key).where(
        E.source_id == provider.id, E.kind == 'reference', E.visibility == 'shared', E.owner_id.is_(None),
        E.source_key.like(prefix+'%:all'), E.source_key <= manifest_key(dataset, day),
        E.received_at <= cutoff).order_by(E.source_key.desc(), E.received_at.desc(), E.id.desc()).limit(8))
    return {key.split(':')[2]: ident for key, ident in rows}


def industry_members(db, day, manifest, sectors, cutoff):
    key = (manifest, tuple(s.code for s in sectors))
    cached = _memberships.get(key)
    if cached is not None:
        return cached
    table = load_dataset(db, 'members', day, as_of_time=cutoff, include_rows=False)
    groups = defaultdict(set)
    if table['state'] != 'complete':
        return {}
    from uuid import UUID
    parts = {UUID(r['id']):r['row_count'] for r in table['evidence_refs']}
    sizes = db.execute(select(E.id, func.jsonb_array_length(E.payload['rows'])).where(
        E.id.in_(parts), E.payload['dataset'].astext == 'review.members'))
    if dict(sizes.all()) != parts:
        return {}
    ds = day.strftime('%Y%m%d')
    row = func.jsonb_array_elements(E.payload['rows']).table_valued(column('value', JSONB)).lateral()
    code_col = row.c.value['ts_code'].astext
    wanted = [s.code for s in sectors]
    stmt = select(code_col, row.c.value['con_code'].astext).select_from(E).join(row,true()).where(
        E.id.in_(parts), code_col.in_(wanted), row.c.value['trade_date'].astext == ds)
    for code, stock in db.execute(stmt.execution_options(yield_per=1024)):
        if stock and re.fullmatch(r'\d{6}\.(SH|SZ|BJ)', stock):
            groups[code].add(stock)
    cached = {code:(day, members, True) for code,members in groups.items()}
    _memberships.clear()
    _memberships[key] = cached
    return cached


def industry_breadth(db, quote_day, cutoff, boards):
    from types import SimpleNamespace
    from app.market.sector_strength import count_strength
    from app.market.terminal_archives import closing_table
    from app.market.trading_datasets import source_day

    sectors = [SimpleNamespace(code=code+'.DC', name=b['name'], category='industry', industry_level=2)
               for code,b in boards.items()]
    local = cutoff.astimezone(SHANGHAI)
    today = local.date()
    membership_days = manifest_days(db, 'members', today, cutoff)
    if not membership_days:
        return None, None, {}
    if today.weekday() < 5 and time(9,30) <= local.time() < time(15):
        from app.market.session import market_session
        from app.market.ladder_observation import live_quotes
        session = market_session(db, cutoff)
        if session.is_trading_day and session.trade_day == today:
            ds = max(membership_days)
            members = industry_members(db, date.fromisoformat(ds), membership_days[ds], sectors, cutoff)
            stocks = live_quotes(db, today, session.previous_trade_day, cutoff)
            ranked = count_strength(sectors, members, stocks, today, cutoff)
            if any(r.quoted_count for r in ranked):
                return today, 'intraday', {r.code[:-3]:r for r in ranked}
    if not quote_day:
        return None, None, {}
    day = min(quote_day, today - timedelta(days=1) if local.time() < time(15) else today)
    prices = manifest_days(db, 'daily', day, cutoff)
    limits = manifest_days(db, 'limits', day, cutoff)
    shared = membership_days.keys() & prices.keys() & limits.keys()
    if not shared:
        return None, None, {}
    ds = max(shared)
    day = date.fromisoformat(ds)
    signature = (membership_days[ds], prices[ds], limits[ds], tuple(s.code for s in sectors))
    cached = _breadth.get(signature)
    if cached is None:
        members = industry_members(db, day, membership_days[ds], sectors, cutoff)
        reader = SimpleNamespace(db=db, cutoff=cutoff, tables={}, input_versions={}, day=source_day,
                                 permitted={DATASET_FAMILIES['daily'], DATASET_FAMILIES['limits']})
        daily = closing_table(reader, day, 'daily')
        upper = load_dataset(db, 'limits', day, as_of_time=cutoff)
        if not daily or upper['state'] != 'complete' or not upper['rows']:
            return None, None, {}
        by_code = {r['ts_code']: decimal(r.get('up_limit')) for r in upper['rows'] if r.get('ts_code')
                   and source_day(r.get('trade_date')) == day}
        if len(by_code) != len(upper['rows']):
            return None, None, {}
        stocks = {r['ts_code']: SimpleNamespace(price=decimal(r.get('close')), limit_up=by_code.get(r['ts_code']),
                    price_basis='daily_close', source_received_at=None) for r in daily['rows']}
        ranked = count_strength(sectors, members, stocks, day, cutoff, daily_close=True)
        cached = {r.code[:-3]:r for r in ranked}
        _breadth.clear()
        _breadth[signature] = cached
    return day, 'daily_close', cached
