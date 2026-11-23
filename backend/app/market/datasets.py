"""Local cached views. Permission revocation is rechecked before every read."""
from datetime import datetime, time
import re

from sqlalchemy import case, func, or_, select

from app.evidence.models import EvidenceVersion
from app.market.read_models import market_identities
from app.market.protocol import SHANGHAI
from app.market.schemas import BarItem, BarSeries, BreadthPoint, BreadthSeries, DatasetItem, DatasetList, NewsItem, NewsList
from app.models import Provider
from app.market.auction_source import FAMILIES as AUCTION_TABLE_FAMILIES


def current_rows(db,kind,*,instrument_id=None,maximum=100,family=None,dataset=None,day=None,
                 bar_interval=None,bar_adjustment=None,source_id=None,families=None,preferred=None):
    from app.provider_access import cloud_share_sql
    daily_table = kind == 'reference' and family in AUCTION_TABLE_FAMILIES
    partition = ((EvidenceVersion.source_id, EvidenceVersion.payload['dataset'].astext,
                  EvidenceVersion.payload['observed_day'].astext) if daily_table else
                 (EvidenceVersion.source_id, EvidenceVersion.source_key))
    ranks=select(EvidenceVersion.id,func.row_number().over(
        partition_by=partition,order_by=(EvidenceVersion.received_at.desc(), EvidenceVersion.version_number.desc(), EvidenceVersion.id.desc()) if daily_table else EvidenceVersion.version_number.desc()).label('position'))
    ranks=ranks.where(EvidenceVersion.kind==kind,EvidenceVersion.visibility=='shared')
    if dataset is None or not dataset.startswith('review.'):
        ranks=ranks.where(~EvidenceVersion.source_key.startswith('review.'))
    if instrument_id is not None:ranks=ranks.where(EvidenceVersion.instrument_id==instrument_id)
    ranks=ranks.subquery()
    stmt=select(EvidenceVersion,Provider).join(Provider,Provider.id==EvidenceVersion.source_id).join(ranks,ranks.c.id==EvidenceVersion.id).where(
        ranks.c.position==1,Provider.enabled.is_(True),Provider.configured.is_(True),Provider.entitlement_status=='verified',
        Provider.rights['display'].astext=='allowed',Provider.rights['cache'].astext=='allowed',cloud_share_sql())
    if family is not None:stmt=stmt.where(Provider.family==family)
    if families is not None:stmt=stmt.where(Provider.family.in_(families))
    if source_id is not None:stmt=stmt.where(EvidenceVersion.source_id==source_id)
    # Apply period/price-basis isolation after current-version selection but
    # before the shared row limit. Minute slots must never crowd out daily bars,
    # and a filtered-out current revision must not resurrect an older revision.
    if bar_interval is not None:stmt=stmt.where(EvidenceVersion.payload['interval'].astext==bar_interval)
    if bar_adjustment is not None:
        basis=case((EvidenceVersion.payload.has_key('adjustment'), EvidenceVersion.payload['adjustment'].astext),
                   else_='unadjusted')
        stmt=stmt.where(basis==bar_adjustment)
    if dataset is not None:stmt=stmt.where(EvidenceVersion.payload['dataset'].astext==dataset)
    if day is not None:stmt=stmt.where(EvidenceVersion.payload['observed_day'].astext==day.isoformat())
    if preferred:
        stmt=stmt.order_by(case({value:index for index,value in enumerate(preferred)},value=Provider.family,else_=len(preferred)))
    if daily_table:
        # Latest state per source/dataset/day, then most recent source dates.
        # Repeated intraday revisions and unrelated sources cannot push the
        # three historical trading days out of this cached view.
        if day is None: maximum = 3 if dataset is not None else 6
        stmt=stmt.order_by(EvidenceVersion.payload['observed_day'].astext.desc())
    return list(db.execute(stmt.order_by(EvidenceVersion.received_at.desc(),EvidenceVersion.id.desc()).limit(maximum)))


def datasets(db,instrument_id=None,dataset=None,family=None,day=None):
    from app.integrations.routing import families_for, route_settings
    settings=route_settings(db)
    choices=settings.choices if settings else {}
    preferred=[] if family else list(dict.fromkeys(family for capability in ('financial','adjustment','depth5')
        for family in families_for(capability,choices.get(capability,{}).get('provider_id'))))
    records=current_rows(db,'reference',instrument_id=instrument_id,maximum=100,dataset=dataset,family=family,day=day,preferred=preferred)
    from app.market.tencent_market import FAMILY as QQ, saved_scope_allowed
    cutoff=datetime.now(SHANGHAI)
    return DatasetList(items=[DatasetItem(id=e.id,source_id=p.id,source_name=p.name,family=p.family,source_key=e.source_key,
        instrument_id=e.instrument_id,received_at=e.received_at,published_at=e.published_at,content_hash=e.content_hash,payload=e.payload)
        for e,p in records if p.family!=QQ or saved_scope_allowed(db,e.payload,cutoff,received_at=e.received_at)])


def breadth_history(db, day=None, *, latest_only=False, source='auto'):
    """Saved observations, never reconstructed source ticks or carried-forward counts."""
    now = datetime.now(SHANGHAI)
    day = day or now.date()
    from app.market.tushare_market import breadth_series
    if source not in {'auto','tushare_market_rt','tencent_market_rt','ashare_breadth'}:
        raise ValueError('Unsupported breadth history source')
    if source!='ashare_breadth':
        if source=='auto':
            from app.market.market_series import choose, FAMILIES
            providers={p.family:p for p in db.scalars(select(Provider).where(Provider.family.in_(FAMILIES)))}
            selected=choose(db,providers,day,now)
        else:selected=source
        if selected=='tencent_market_rt':
            from app.market.tencent_market import breadth_series as tencent_series
            series=tencent_series(db,day,now,latest_only=latest_only)
        else:series=breadth_series(db,day,now,latest_only=latest_only)
        if series is not None:return series
        if source!='auto':
            return BreadthSeries(observation_day=day, source_name=None,source_family=source,items=[])
    result = BreadthSeries(observation_day=day, source_name=None, source_family='ashare_breadth',items=[])
    # Resolve the permitted current series first: renaming its latest version
    # must not resurrect records under the previous dataset identity.
    current = current_rows(db, 'reference', family='ashare_breadth', dataset='market.ashare.breadth', maximum=1)
    if not current:
        return result
    latest, provider = current[0]
    result.source_name = provider.name
    clock = lambda hour, minute: datetime.combine(day, time(hour, minute), SHANGHAI)
    stmt = select(EvidenceVersion).where(
        EvidenceVersion.source_id == provider.id, EvidenceVersion.source_key == latest.source_key,
        EvidenceVersion.kind == 'reference', EvidenceVersion.visibility == 'shared',
        EvidenceVersion.payload['dataset'].astext == 'market.ashare.breadth',
        EvidenceVersion.received_at <= now,
        or_(EvidenceVersion.received_at.between(clock(9, 30), clock(11, 30)),
            EvidenceVersion.received_at.between(clock(13, 0), clock(15, 0))))
    rows = list(db.scalars(stmt.order_by(EvidenceVersion.received_at.desc(), EvidenceVersion.version_number.desc()).limit(2001)))
    result.truncated = len(rows) > 2000
    def known_count(value):
        return int(value) if isinstance(value, str) and re.fullmatch(r'(?:0|[1-9]\d{0,5})', value) else None
    for row in reversed(rows[:2000]):
        values = (row.payload.get('rows') or [{}])[0]
        result.items.append(BreadthPoint(id=row.id, received_at=row.received_at,
            limit_up=known_count(values.get('limit_up')), limit_down=known_count(values.get('limit_down'))))
    return result


def news(db,family=None):
    items=[NewsItem(id=e.id,source_id=p.id,source_name=p.name,family=p.family,received_at=e.received_at,
        published_at=e.published_at,content_hash=e.content_hash,payload=e.payload) for e,p in current_rows(db,'news',maximum=100,family=family)]
    def order(item):
        local = (item.published_at or item.received_at).astimezone(SHANGHAI)
        source_day = item.payload.published_day if item.published_at is None else None
        day = source_day or local.date()
        # A date-only item has no release clock. Timed entries come first on
        # that day, then date-only entries by receipt/id; no time is written back.
        clock = (0,) if source_day else (1,local.hour,local.minute,local.second,local.microsecond)
        return day.toordinal(), clock, item.received_at, str(item.id)
    items.sort(key=order,reverse=True)
    return NewsList(items=items)


def bars(db,instrument_id,adjustment,*,interval='1d',day=None,source_id=None,include_live=False,
         before=None,start_day=None,end_day=None,limit=400):
    if interval!='1d':
        from app.market.minute_read import minute_bars
        return minute_bars(db,instrument_id,adjustment,interval,day,source_id)
    instrument=market_identities(db,instrument_id)[0]
    from app.market.daily_history import read_daily
    rows,metadata=read_daily(db,instrument_id,adjustment,source_id=source_id,before=before,
        start_day=start_day,end_day=end_day,limit=limit)
    from app.market.minute_read import current_daily
    current=current_daily(db,instrument_id,adjustment) if include_live and not before and not end_day else None
    return BarSeries(instrument=instrument,**metadata,adjustment=adjustment,
        state='historical' if rows else 'empty',
        current_daily=current[0] if current else None,
        current_daily_source_name=current[1].source_name if current else None,
        current_daily_source_family=current[1].family if current else None,
        current_daily_state=current[2].state if current else None,
        current_daily_age_seconds=current[2].age_seconds if current else None,
        current_daily_reason=current[2].reason if current else None,
        data_mode='live_daily' if current else 'saved_daily',
        items=[BarItem(id=e.id,received_at=e.received_at,content_hash=e.content_hash,payload=e.payload) for e in rows])
